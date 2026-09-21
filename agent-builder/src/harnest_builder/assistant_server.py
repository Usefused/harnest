"""Supervise Studio's private compiled Harnest server and its transient requests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import re
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import httpx

from .assistant_build import PACKAGE, compile_assistant
from .processes import terminate_tree
from .assistant_errors import MESSAGES, RETRYABLE, failure_category

LOGGER = logging.getLogger(__name__)


def traceback_summary(text: str) -> dict:
    """Keep exception types and code locations, never exception messages or source lines."""
    frames = re.findall(r'File "([^"\n]+)", line ([0-9]+), in ([A-Za-z_][A-Za-z0-9_]*)', text)
    kinds = re.findall(r'^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):', text, re.MULTILINE)
    return {"exceptions": kinds[-4:], "frames": [f"{Path(path).name}:{line}:{function}" for path, line, function in frames[-12:]]}


def free_port() -> int:
    """Choose a loopback port; authenticated readiness prevents trusting another listener."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def source_reply(events: list[dict]) -> str | None:
    """Forward only the declared source tool's output to Studio's authorization boundary."""
    source_requests = [item for item in events if item.get("type") == "tool_result" and item.get("name") == "read_files"]
    if source_requests:
        # Preserve all requests from parallel tool calls. The proposal boundary
        # validates inventory membership, permission, count, and total bytes.
        paths = [path for item in source_requests for path in item["output"]["read_files"]]
        return json.dumps({"read_files": paths})
    return None


def final_reply(result: dict) -> str:
    """Prefer native source requests, otherwise keep only the final model turn's text."""
    events = result.get("output", [])
    requested = source_reply(events)
    if requested is not None:
        return requested
    managed = [item for item in events if item.get("type") == "tool_result" and item.get("name") in {"fused_discover", "plan_fused_mcp"}]
    if managed:
        if len(managed) != 1:
            raise ValueError("Request one Fused operation per turn")
        return json.dumps(managed[0]["output"])
    return final_model_text(result, events)


def final_model_text(result: dict, events: list[dict]) -> str:
    """Exclude skill narration and tool content while retaining final message chunks."""
    boundary = max((index + 1 for index, item in enumerate(events) if item.get("type") == "tool_result"), default=0)
    messages = [item for item in events[boundary:]
                if item.get("type") == "message" and item.get("role") == "assistant"]
    if messages:
        return "".join(part["text"] for item in messages for part in item.get("content", [])
                       if part.get("type") == "output_text")
    return result["outputText"]


class AssistantServer:
    """Own one model-bound Harnest process, serialized to prevent cross-request restarts."""

    def __init__(self, artifact: Path | None = None):
        """Defer compilation and process startup until the first Build with AI request."""
        configured = os.getenv("HARNEST_BUILDER_ARTIFACT")
        self.artifact = artifact or (Path(configured) if configured else PACKAGE / "_assistant")
        self.explicit_artifact = artifact is not None or bool(configured)
        self.process = None
        self.client = None
        self.model = None
        self.lock = asyncio.Lock()
        self.temporary = None
        self.log = None
        self.diagnostics = None
        self.session = None

    async def completion(self, *, model, messages, **_options):
        """Translate the existing proposal boundary into the native Harnest response protocol."""
        user = [item["content"] for item in messages if item["role"] == "user"]
        payload = json.dumps({"context": json.loads(user[-2]), "request": user[-1]})
        async with self.lock:
            content = await self.request(model, payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    async def request(self, model: str, payload: str) -> str:
        """Retry a read-only proposal once for transient failures, using a fresh owned server."""
        reference = uuid.uuid4().hex[:12]
        for attempt in range(2):
            await self.ensure_running(model)
            try:
                return await self.invoke(payload)
            except asyncio.CancelledError:
                await self.stop()
                raise
            except (httpx.HTTPError, ValueError, KeyError) as error:
                category = failure_category(error, self.read_diagnostic(), self.session)
                retry = attempt == 0 and category in RETRYABLE
                LOGGER.warning("builder_request_failed reference=%s category=%s attempt=%s retry=%s", reference, category, attempt + 1, retry)
                if category == "runtime_error":
                    LOGGER.warning("builder_runtime_trace reference=%s trace=%s", reference, self.runtime_trace())
                # Failed native invocations may still own their session and reject
                # deletion. Discard the private process to clear that state too.
                await self.stop()
                if not retry:
                    raise HTTPException(502, MESSAGES[category] + f" Reference: {reference}.") from error
                # A proposal cannot execute project code or apply edits, so retrying
                # this private request cannot duplicate user-visible mutations.
                await asyncio.sleep(1)

    def runtime_trace(self) -> dict:
        """Read a bounded tail from the private server log for sanitized debugging metadata."""
        if self.log is None:
            return {}
        # pread avoids moving the shared file offset used by the child process.
        size = os.fstat(self.log.fileno()).st_size
        if hasattr(os, "pread"):
            data = os.pread(self.log.fileno(), min(size, 131072), max(0, size - 131072))
        else:
            # Windows exposes a named temporary file; a separate reader leaves
            # the writer's offset intact, just as pread does on POSIX.
            with open(self.log.name, "rb") as stream:
                stream.seek(max(0, size - 131072))
                data = stream.read(131072)
        return traceback_summary(data.decode(errors="replace"))

    def read_diagnostic(self) -> dict:
        """Read bounded, category-only model evidence without surfacing raw runtime logs."""
        if self.diagnostics is None:
            return {}
        try:
            with (Path(self.diagnostics.name) / "failure.json").open() as stream:
                result = json.loads(stream.read(2048))
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    async def prepare(self) -> None:
        """Use the packaged artifact in production; compile from a checkout only in development."""
        if (self.artifact / "harnest-manifest.json").is_file():
            return
        if self.explicit_artifact or not (PACKAGE / "assistant_source").is_dir():
            raise HTTPException(503, "The compiled Studio agent is missing. Rebuild the Studio distribution.")
        self.temporary = tempfile.TemporaryDirectory(prefix="harnest-studio-agent-")
        self.artifact = Path(self.temporary.name) / "compiled"
        try:
            await self.compile_development()
        except Exception as error:
            self.temporary.cleanup()
            self.temporary = None
            raise HTTPException(503, "The Studio agent could not compile. Build it with the matching Harnest runtime and authoring skills.") from error

    async def compile_development(self) -> None:
        """Let the bounded compiler finish before cancellation can delete its output directory."""
        task = asyncio.create_task(asyncio.to_thread(compile_assistant, self.artifact))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise

    async def ensure_running(self, model: str) -> None:
        """Restart after model changes or process exits, never changing global provider settings."""
        if self.process is not None and self.process.returncode is None and self.model == model:
            return
        await self.stop()
        await self.prepare()
        token, port = secrets.token_urlsafe(32), free_port()
        self.diagnostics = tempfile.TemporaryDirectory(prefix="harnest-builder-diagnostics-")
        environment = {**os.environ, "HARNEST_BUILDER_MODEL": model, "HARNEST_BUILDER_SERVICE_TOKEN": token,
                       "HARNEST_BUILDER_DIAGNOSTICS": str(Path(self.diagnostics.name) / "failure.json")}
        self.log = tempfile.TemporaryFile()
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, str(self.artifact), "serve", "--host", "127.0.0.1", "--port", str(port),
                cwd=str(self.artifact), env=environment, stdout=self.log, stderr=self.log, start_new_session=True,
            )
            self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}, timeout=185, trust_env=False)
            await self.wait_ready()
            self.model = model
        except BaseException:
            await self.stop()
            raise

    async def wait_ready(self) -> None:
        """Wait for an authenticated native endpoint, failing cleanly on startup errors."""
        for _ in range(150):
            if self.process.returncode is not None:
                raise HTTPException(503, "The compiled Studio agent could not start. Check its runtime dependencies and model configuration.")
            try:
                response = await self.client.get("/sessions", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
        raise HTTPException(503, "The compiled Studio agent did not become ready in time.")

    async def invoke(self, prompt: str) -> str:
        """Use a fresh session and delete it after success, failure, or cancellation."""
        session = uuid.uuid4().hex
        self.session = session
        created = await self.client.post("/sessions", json={"id": session})
        created.raise_for_status()
        try:
            response = await self.client.post("/responses", json={"input": prompt, "sessionId": session, "stream": False})
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "completed":
                raise HTTPException(502, "The Harnest builder agent did not complete its proposal.")
            return final_reply(result)
        finally:
            with suppress(httpx.HTTPError):
                await self.client.delete(f"/sessions/{session}", timeout=5)

    async def stop(self) -> None:
        """Release sockets and the full subprocess group before replacing its model or exiting."""
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        if self.process is not None and self.process.returncode is None:
            with suppress(ProcessLookupError):
                await asyncio.to_thread(terminate_tree, self.process.pid)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    await asyncio.to_thread(terminate_tree, self.process.pid, force=True)
                await self.process.wait()
        self.process = None
        self.model = None
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.diagnostics is not None:
            self.diagnostics.cleanup()
            self.diagnostics = None
        self.session = None

    async def close(self) -> None:
        """Stop owned runtime work and discard development-only compiled files on shutdown."""
        async with self.lock:
            await self.stop()
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None

"""Run Studio previews through the public CLI and bounded native HTTP contracts."""

import asyncio
import json
import time
import uuid

from fastapi import HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field

from .assistant_server import free_port
from .commands import Command, arguments


class PreviewProject(BaseModel):
    """Select a registered project, never an upstream address or executable."""

    model_config = ConfigDict(extra="forbid", strict=True)
    project: str


class PreviewMessage(PreviewProject):
    """Keep test conversations independent from the source-writing assistant."""

    conversation: str = Field(pattern=r"^[a-f0-9-]{36}$")
    input: str = Field(min_length=1, max_length=32000)


class Preview:
    """Own one reload server and its test sessions for the lifetime of Studio."""

    def __init__(self, workspace, jobs):
        """Reuse CLI supervision, project credentials, and shutdown ownership."""
        self.workspace, self.jobs = workspace, jobs
        self.job = None
        self.port = None
        self.bound = False
        self.sessions = {}
        self.invoking = asyncio.Lock()

    def start(self, project):
        """Reuse the active preview; require an explicit stop before switching agents."""
        with self.workspace.lock, self.jobs.lock:
            if self.invoking.locked():
                raise HTTPException(409, "Wait for the current agent response before restarting the preview.")
            self.workspace.project(project)
            active = self.current()
            if active and active["status"] == "running":
                if active["project"] != project:
                    raise HTTPException(409, "Stop the running preview before running another agent.")
                return active
            self.jobs.ensure_idle()
            port = free_port()
            args, identity = arguments(self.workspace, Command(action="serve", project=project, port=port))
            job = self.jobs.start(args, identity, serving=True)
            self.job, self.port = job["id"], port
            self.bound = False
            self.sessions.clear()
            return job

    def current(self):
        """Read a supervision snapshot rather than treating a port as process ownership."""
        return next((job for job in self.jobs.list() if job["id"] == self.job), None)

    async def status(self, project):
        """Distinguish compilation, readiness, failure, and a different active agent."""
        self.workspace.project(project)
        job = self.current()
        if not job or job["project"] != project:
            return {"status": "idle", "active_job": job}
        result = {"status": job["status"], "job": job}
        if job["status"] != "running":
            return result
        result["status"] = "starting"
        # Do not contact the selected port until the owned server reports binding it.
        if not self.bound:
            if "Uvicorn running on" not in job["output"]:
                return result
            # Job logs roll over; binding evidence must survive a verbose conversation.
            self.bound = True
        try:
            await self.request("GET", "/agent", timeout=2)
            result.update(status="ready", url=f"http://127.0.0.1:{self.port}/")
        except HTTPException as error:
            result.update(status="unavailable", detail=error.detail)
        if self.job != job["id"]:
            return {"status": "idle", "active_job": self.current()}
        return result

    async def request(self, method, path, *, body=None, timeout=180):
        """Contact only the owned loopback server; bound responses and never retry actions."""
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                async with client.stream(method, f"http://127.0.0.1:{self.port}{path}", json=body) as response:
                    if response.status_code in {401, 403}:
                        raise HTTPException(409, "This agent requires authentication. Open its Playground to sign in and test it.")
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise HTTPException(502, "The agent response exceeded the preview limit. Inspect it in Playground.")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Native responses must be objects")
            return result
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(502, "The agent did not return a usable response. Inspect server output before retrying; an action may already have run.") from error

    async def invoke(self, body):
        """Retain native conversation state and serialize turns without replaying side effects."""
        if self.invoking.locked():
            raise HTTPException(409, "Wait for the current agent response before sending another message.")
        async with self.invoking:
            current = await self.status(body.project)
            if current["status"] != "ready":
                raise HTTPException(409, current.get("detail", "Run the agent and wait for it to become ready."))
            if body.conversation not in self.sessions:
                if len(self.sessions) >= 64:
                    raise HTTPException(409, "Restart the preview to begin more conversations.")
                session = "studio-" + uuid.uuid4().hex
                await self.request("POST", "/sessions", body={"id": session})
                self.sessions[body.conversation] = session
            started = time.monotonic()
            result = await self.request("POST", "/responses", body={"input": body.input, "sessionId": self.sessions[body.conversation], "stream": False})
            return {"response": result, "session_id": self.sessions[body.conversation], "duration_ms": round((time.monotonic() - started) * 1000)}


def install_routes(app, workspace, jobs):
    """Keep preview operations inside Studio's existing authenticated local boundary."""
    preview = Preview(workspace, jobs)
    app.state.preview = preview

    @app.post("/api/preview/start")
    def start(body: PreviewProject):
        """Start compilation and serving without blocking the browser on preparation."""
        return preview.start(body.project)

    @app.get("/api/preview")
    async def status(project: str):
        """Report actual server readiness separately from a running compiler process."""
        return await preview.status(project)

    @app.post("/api/preview/message")
    async def message(body: PreviewMessage):
        """Send an explicit user message to the agent, never to the builder assistant."""
        return await preview.invoke(body)

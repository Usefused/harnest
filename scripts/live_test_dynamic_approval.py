"""Live HTTP probe for dynamic approval in a compiled Harnest server."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from harnest.bundle import compile_artifact


def _write_agent(root: Path) -> None:
    """Create a deterministic graph that fails if approval replays evaluation."""

    files = {
        "agent.py": _AGENT,
        "instructions.md": "Evaluate risk before executing the operation.\n",
        "agent-card.yaml": (
            "name: Dynamic approval live\n"
            "description: Dynamic approval live probe\n"
            "version: 0.1.0\n"
        ),
        "lib/storage.py": _STORAGE,
        "extensions/storage.py": _STORAGE_EXTENSION,
    }
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


def _free_port() -> int:
    """Reserve an ephemeral loopback port for the standalone probe."""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    base_url: str, path: str, *, method: str = "GET", payload: Any = None
) -> dict[str, Any]:
    """Send one JSON request to the real server and decode its response."""

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback only
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{method} {path} returned {exc.code}: {detail}") from exc


def _wait_until_ready(base_url: str, process: subprocess.Popen[bytes]) -> None:
    """Wait for the child server or report an early startup failure."""

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = b"" if process.stdout is None else process.stdout.read()
            detail = output.decode("utf-8", errors="replace").strip()
            raise AssertionError(
                f"server exited with status {process.returncode}: {detail}"
            )
        try:
            _request(base_url, "/healthz")
            return
        except (AssertionError, URLError):
            time.sleep(0.05)
    raise AssertionError("server did not become ready")


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    """Stop only the standalone server created by this probe."""

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run() -> None:
    """Compile, serve, suspend, approve, and resume one dynamic operation."""

    with tempfile.TemporaryDirectory(prefix="harnest-dynamic-live-") as directory:
        root = Path(directory) / "source"
        artifact = Path(directory) / "artifact"
        root.mkdir()
        _write_agent(root)
        compile_artifact(root, artifact, framework="langgraph", mode="managed")

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [
                sys.executable,
                str(artifact),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_ready(base_url, process)
            session = _request(
                base_url,
                "/sessions",
                method="POST",
                payload={"id": "dynamic-live"},
            )
            required = _request(
                base_url,
                "/responses",
                method="POST",
                payload={"input": "risky", "sessionId": session["id"]},
            )
            assert required["status"] == "requires_action", required
            action = required["requiredAction"]
            assert action["action"] == "dynamic:typescript.execute", action
            completed = _request(
                base_url,
                f"/approvals/{action['id']}",
                method="POST",
                payload={"decision": "approve"},
            )
            assert completed["status"] == "completed", completed
            assert completed["outputText"] == "executed:risky", completed
        finally:
            _stop_server(process)


def main() -> int:
    """Run the standalone dynamic-approval probe."""

    run()
    print("live dynamic approval passed for langgraph")
    return 0


_AGENT = '''"""Deterministic graph for the dynamic approval live probe."""
from harnest import Edge, Event, Graph, START, request_human_approval

evaluations = 0

async def execute(value):
    global evaluations
    evaluations += 1
    if evaluations != 1:
        raise RuntimeError("risk evaluation was replayed")
    risk = {"capabilities": ["network"], "sourceHash": "sha256:fixture"}
    async with request_human_approval(
        action="typescript.execute",
        message="Execute TypeScript with network access?",
        arguments=risk,
    ):
        return Event(output=f"executed:{value}", message=f"executed:{value}")

root_agent = Graph(
    name="dynamic_approval_live",
    nodes={"execute": execute},
    edges=(Edge(START, "execute"),),
)
'''

_STORAGE = """from harnest import MemoryStore\nstore = MemoryStore()\n"""

_STORAGE_EXTENSION = '''from harnest import lifecycle
from harnest.lib.storage import store

@lifecycle.session_store
def sessions():
    return store

@lifecycle.checkpointer
def checkpoints():
    return store
'''


if __name__ == "__main__":
    raise SystemExit(main())

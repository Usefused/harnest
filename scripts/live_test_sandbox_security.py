"""Opt-in bounded security regressions against an already available Docker image.

Run with PYTHONPATH=src .venv/bin/python scripts/live_test_sandbox_security.py
--image python:3.12-slim --base-url unix:///path/to/docker.sock. No image is
pulled, no host directory is mounted, and only recorded test containers are
removed. The parent bounds the entire probe independently of the sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

import docker
from docker.models.containers import ContainerCollection

from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.sandbox import Sandbox, SandboxRequest


OUTPUT_LIMIT = 64 * 1024
OWNED_PREFIX = "HARNEST_SECURITY_CONTAINER="
REPORT_PREFIX = "HARNEST_SECURITY_REPORT="


def _backend(args: argparse.Namespace) -> Any:
    """Select explicit network-denied Docker configuration without host mounts."""
    return Sandbox.container(
        image=args.image, base_url=args.base_url, network=False,
        timeout_seconds=4, max_output_bytes=OUTPUT_LIMIT,
        metadata={"purpose": "bounded-security-regression", "network": False},
        options={"error_retry_attempts": 2, "stateful": False,
                 "optimize_data_file": False},
    ).build()


def _warm(backend: Any) -> str:
    """Create the native container before measuring execution-only deadlines."""
    result = backend.execute(SandboxRequest("print('ready')"))
    assert result.stdout == "ready\n", (result.stdout, result.stderr)
    return backend._executor._container.id


def _removed(client: Any, container_id: str) -> bool:
    """Check a known owned container without inspecting unrelated resources."""
    try:
        client.containers.get(container_id)
    except docker.errors.NotFound:
        return True
    return False


def _recovery(backend: Any, client: Any, previous_id: str) -> str:
    """Prove abnormal execution discards its container before a successful retry."""
    assert _removed(client, previous_id), "aborted container was not removed"
    replacement = _warm(backend)
    assert replacement != previous_id, "aborted executor was reused"
    return replacement


def _deadline_probe(backend: Any, client: Any) -> dict[str, Any]:
    """Suspend the in-container supervisor while retaining a bounded child program."""
    original = _warm(backend)
    code = (
        "import os, signal, time\n"
        "os.kill(os.getppid(), signal.SIGSTOP)\n"
        "time.sleep(2)\n"
        "print('DEADLINE_BYPASSED', flush=True)\n"
    )
    started = time.monotonic()
    result = backend.execute(SandboxRequest(code, timeout_seconds=1))
    elapsed = time.monotonic() - started
    assert elapsed < 1.9, f"host deadline took {elapsed:.3f}s"
    assert "DEADLINE_BYPASSED" not in result.stdout
    assert "tim" in result.stderr.lower(), result.stderr
    replacement = _recovery(backend, client, original)
    return {"elapsed_seconds": round(elapsed, 3), "deadline_seconds": 1,
            "supervisor_suspended": True, "post_deadline_statement_blocked": True,
            "discarded_container": original, "recovery_container": replacement}


def _output_probe(backend: Any, client: Any) -> dict[str, Any]:
    """Exceed a deliberately small output cap without attempting memory exhaustion."""
    original = _warm(backend)
    result = backend.execute(SandboxRequest(
        "import sys; sys.stdout.write('x' * (128 * 1024)); sys.stdout.flush()",
    ))
    captured = len(result.stdout.encode("utf-8"))
    assert captured <= OUTPUT_LIMIT
    assert "output" in result.stderr.lower(), result.stderr
    assert len(result.stderr.encode("utf-8")) < 1024
    replacement = _recovery(backend, client, original)
    return {"attempted_bytes": 128 * 1024, "max_output_bytes": OUTPUT_LIMIT,
            "captured_stdout_bytes": captured, "discarded_container": original,
            "recovery_container": replacement}


async def _wait_marker(container: Any, path: str) -> None:
    """Observe a test-owned marker to remove admission timing guesses."""
    # Observe through the raw owned Docker object; using the execution guard
    # would create a second managed operation and disturb the admitted call.
    container = getattr(container, "container", container)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_marker_exists, container, path):
            return
        await asyncio.sleep(0.025)
    raise AssertionError("test code did not start before its bounded wait")


def _marker_exists(container: Any, path: str) -> bool:
    """Treat restart-in-progress as no marker without ignoring other Docker errors."""
    try:
        return container.exec_run(["test", "-f", path]).exit_code == 0
    except docker.errors.APIError as error:
        if error.status_code == 409:
            return False
        raise


async def _cancel(task: asyncio.Task[Any]) -> None:
    """Require the caller-visible cancellation contract without masking failures."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("sandbox task did not preserve caller cancellation")


async def _queued_probe(backend: Any) -> dict[str, Any]:
    """Cancel a call waiting behind native execution and verify it never writes."""
    tool = Sandbox.provider(lambda: backend, timeout_seconds=4).to_langchain_tool()
    container_id = _warm(backend)
    container = backend._executor._container
    first = asyncio.create_task(tool.ainvoke({"code":
        "from pathlib import Path; import time; "
        "Path('/tmp/security-running').touch(); time.sleep(0.8); print('finished')"}))
    await _wait_marker(container, "/tmp/security-running")
    queued = asyncio.create_task(tool.ainvoke({"code":
        "from pathlib import Path; Path('/tmp/security-cancelled-write').touch()"}))
    await asyncio.sleep(0.1)
    await _cancel(queued)
    assert (await first)["stdout"] == "finished\n"
    check = await tool.ainvoke({"code":
        "from pathlib import Path; print(Path('/tmp/security-cancelled-write').exists())"})
    assert check["stdout"] == "False\n", "cancelled queued call wrote to the container"
    assert backend._executor._container.id == container_id
    return {"cancelled_write_absent": True, "running_call_undisturbed": True,
            "container_id": container_id}


async def _active_probe(backend: Any, client: Any) -> dict[str, Any]:
    """Cancel admitted native work and require its entire container to disappear."""
    tool = Sandbox.provider(lambda: backend, timeout_seconds=4).to_langchain_tool()
    original = _warm(backend)
    container = backend._executor._container
    running = asyncio.create_task(tool.ainvoke({"code":
        "from pathlib import Path; import time; "
        "Path('/tmp/security-active').touch(); time.sleep(2); "
        "Path('/tmp/security-active-cancelled-write').touch()"}))
    await _wait_marker(container, "/tmp/security-active")
    await _cancel(running)
    deadline = time.monotonic() + 1.5
    while not await asyncio.to_thread(_removed, client, original):
        assert time.monotonic() < deadline, "active cancellation did not remove its container"
        await asyncio.sleep(0.025)
    replacement = await asyncio.to_thread(_recovery, backend, client, original)
    return {"active_container_removed": True, "discarded_container": original,
            "recovery_container": replacement}


def _revoked_probe(args: argparse.Namespace) -> dict[str, Any]:
    """Ensure a revoked managed identity cannot become an anonymous provider call."""
    backend = _backend(args)
    tool = Sandbox.provider(lambda: backend).to_langchain_tool()
    active = create_agent_context(
        framework="langgraph", agent_name="security-test", invocation_id="test-call",
        user_id="test-user", session_id="test-session", metadata={}, resources={},
    )
    with activate_context(active):
        revoke_context(active)
        try:
            tool.invoke({"code": "print('must not execute')"})
        except RuntimeError as error:
            assert backend._executor is None
            return {"rejected_before_docker": True, "exception_type": type(error).__name__}
    raise AssertionError("revoked context was accepted")


def _detached_probe(backend: Any) -> dict[str, Any]:
    """Ensure successful execution cannot leave detached children running afterward."""
    original = _warm(backend)
    result = backend.execute(SandboxRequest(
        "import os, time\n"
        "from pathlib import Path\n"
        "Path('/tmp/security-kept-file').write_text('persisted')\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    fd = os.open('/dev/null', os.O_RDWR)\n"
        "    for target in (0, 1, 2):\n"
        "        os.dup2(fd, target)\n"
        "    time.sleep(0.6)\n"
        "    Path('/tmp/security-detached-write').touch()\n"
        "    os._exit(0)\n"
        "print('parent finished')\n"
    ))
    assert result.stdout == "parent finished\n"
    time.sleep(0.8)
    result = backend.execute(SandboxRequest(
        "from pathlib import Path\n"
        "print(Path('/tmp/security-detached-write').exists())\n"
        "print(Path('/tmp/security-kept-file').read_text())\n"
    ))
    assert result.stdout == "False\npersisted\n", result.stdout
    assert backend._executor._container.id == original
    return {"detached_write_absent": True, "filesystem_preserved": True,
            "container_id": original}


def _child(args: argparse.Namespace) -> None:
    """Run only pre-existing images and emit ownership immediately after creation."""
    client = docker.DockerClient(base_url=args.base_url)
    client.images.get(args.image)
    native_run = ContainerCollection.run

    def record_run(collection: Any, *values: Any, **options: Any) -> Any:
        """Record exact resources so the parent can recover after any probe failure."""
        container = native_run(collection, *values, **options)
        print(OWNED_PREFIX + container.id, flush=True)
        return container

    try:
        with patch.object(ContainerCollection, "run", record_run):
            backend = _backend(args)
            report = {"status": "passed", "mocked": False, "image": args.image,
                      "deadline": _deadline_probe(backend, client),
                      "output_limit": _output_probe(backend, client),
                      "queued_cancellation": asyncio.run(_queued_probe(backend)),
                      "active_cancellation": asyncio.run(_active_probe(backend, client)),
                      "revoked_context": _revoked_probe(args),
                      "detached_process": _detached_probe(backend)}
            print(REPORT_PREFIX + json.dumps(report), flush=True)
    finally:
        client.close()


def _cleanup(client: Any, output: str) -> list[str]:
    """Remove only exact IDs emitted by this probe, including failed child runs."""
    owned = [line.removeprefix(OWNED_PREFIX) for line in output.splitlines()
             if line.startswith(OWNED_PREFIX)]
    for container_id in owned:
        try:
            client.containers.get(container_id).remove(force=True, v=True)
        except docker.errors.NotFound:
            pass
    assert all(_removed(client, container_id) for container_id in owned)
    return owned


def _run(args: argparse.Namespace) -> None:
    """Bound the whole security probe and recover all recorded owned resources."""
    command = [sys.executable, str(Path(__file__).resolve()), "--child",
               "--image", args.image, "--base-url", args.base_url]
    client = docker.DockerClient(base_url=args.base_url)
    try:
        child = subprocess.run(command, capture_output=True, text=True, timeout=45)
        owned = _cleanup(client, child.stdout)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        _cleanup(client, output.decode() if isinstance(output, bytes) else output)
        raise
    finally:
        client.close()
    if child.returncode:
        raise RuntimeError(f"Security probe failed:\n{child.stdout}\n{child.stderr}")
    line = next(line for line in child.stdout.splitlines() if line.startswith(REPORT_PREFIX))
    report = json.loads(line.removeprefix(REPORT_PREFIX))
    report.update(owned_containers=owned, exact_owned_container_cleanup="passed")
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> None:
    """Require explicit Docker access and never discover or transmit credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", default=".cache/sandbox-security-fixed.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        _child(args)
    else:
        _run(args)


if __name__ == "__main__":
    main()

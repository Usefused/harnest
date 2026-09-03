"""Opt-in real Docker sandbox probe; never substitutes a mocked executor.

Run with PYTHONPATH=src .venv/bin/python scripts/live_test_sandbox_container.py
--image python:3.12-slim --base-url unix:///path/to/docker.sock. The image must
already exist. A child owns all executors so native atexit cleanup is observable.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

import docker
from docker.models.containers import ContainerCollection
from google.adk.code_executors.code_execution_utils import CodeExecutionInput

from harnest.context import activate_context, create_agent_context
from harnest.sandbox import Sandbox, SandboxContext, SandboxFile, SandboxRequest
from harnest.sandbox_types import sandbox_metadata_to_dict


OPTIONS = {
    "error_retry_attempts": 4,
    "code_block_delimiters": [("<live_python>\n", "\n</live_python>")],
    "execution_result_delimiters": ("<live_result>\n", "\n</live_result>"),
    "stateful": False,
    "optimize_data_file": False,
}
METADATA = {
    "provider": "docker-native-adk",
    "region": "local",
    "policy": {"network": False, "labels": ["live", "portable"], "priority": 7},
    "optional": None,
    "sample_rate": 0.25,
}
TIMEOUT_SECONDS = 10
OWNED_PREFIX = "HARNEST_LIVE_CONTAINER="


def _context(framework: str, session: str = "session-a") -> Any:
    """Supply test-owned invocation identity without external application data."""
    return create_agent_context(
        framework=framework, agent_name="live-sandbox", invocation_id="live-call",
        user_id="live-user", session_id=session, metadata={}, resources={},
    )


def _container_details(backend: Any) -> dict[str, Any]:
    """Read only selected non-secret properties from the actual Docker object."""
    executor = backend._executor
    container = executor._container
    container.reload()
    host = container.attrs["HostConfig"]
    assert container.attrs["Config"]["NetworkDisabled"] is True
    assert "ALL" in host["CapDrop"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert host["Memory"] == 0 and host["NanoCpus"] == 0
    assert executor.error_retry_attempts == OPTIONS["error_retry_attempts"]
    assert executor.code_block_delimiters == OPTIONS["code_block_delimiters"]
    assert executor.execution_result_delimiters == OPTIONS["execution_result_delimiters"]
    assert executor.stateful is False and executor.optimize_data_file is False
    return {
        "container_id": container.id,
        "image_id": container.image.id,
        "image": executor.image,
        "network_disabled": True,
        "cap_drop": host["CapDrop"],
        "security_opt": host["SecurityOpt"],
        "memory_bytes": host["Memory"],
        "nano_cpus": host["NanoCpus"],
        "timeout_seconds": executor.timeout_seconds,
        "native_options": OPTIONS,
    }


class RecordingProvider:
    """Attach real Docker properties to genuine execution results and record requests."""

    def __init__(self, backend: Any) -> None:
        """Wrap a real native-backed provider without replacing code execution."""
        self.backend = backend
        self.requests: list[SandboxRequest] = []

    def execute(self, request: SandboxRequest) -> Any:
        """Run in Docker before collecting actual provider result metadata."""
        self.requests.append(request)
        result = self.backend.execute(request)
        metadata = _container_details(self.backend)
        metadata["request_metadata"] = sandbox_metadata_to_dict(request.metadata)
        metadata["execution_id"] = request.execution_id
        return replace(result, metadata=metadata)


def _adk_probe(definition: Sandbox) -> dict[str, Any]:
    """Execute real Python via ADK and confirm every native option was preserved."""
    executor = definition.to_adk_executor()
    assert executor.error_retry_attempts == 4
    assert executor.code_block_delimiters == OPTIONS["code_block_delimiters"]
    assert executor.execution_result_delimiters == OPTIONS["execution_result_delimiters"]
    with activate_context(_context("adk")):
        result = executor.execute_code(None, CodeExecutionInput(
            code="import sys; print('ADK café ✓'); print('diagnostic λ', file=sys.stderr)",
            execution_id="live-adk-execution",
        ))
    assert result.stdout == "ADK café ✓\n"
    assert result.stderr == "diagnostic λ\n"
    return _container_details(executor._runtime._backend)


def _langgraph_probe(definition: Sandbox) -> None:
    """Exercise real sync and async LangChain tool entrypoints with both output streams."""
    tool = definition.to_langchain_tool()
    with activate_context(_context("langgraph")):
        sync = tool.invoke({"code": "print('LG café ✓')"})
        async_result = asyncio.run(tool.ainvoke({"code": "print('async λ')"}))
    assert sync["stdout"] == "LG café ✓\n" and sync["stderr"] == ""
    assert async_result["stdout"] == "async λ\n"
    assert sync["metadata"]["request_metadata"] == METADATA
    assert async_result["metadata"]["request_metadata"] == METADATA


def _metadata_probe(definition: Sandbox) -> RecordingProvider:
    """Verify detailed provider metadata and identity through both native adapters."""
    provider = RecordingProvider(definition.build())
    portable = Sandbox.provider(
        lambda: provider, name="docker-live-details", timeout_seconds=TIMEOUT_SECONDS, metadata=METADATA,
    )
    adk = portable.to_adk_executor()
    with activate_context(_context("adk")):
        result = adk.execute_code(None, CodeExecutionInput(
            code="print(6 * 7)", execution_id="metadata-execution",
        ))
    envelope = json.loads(result.stdout)
    assert envelope["stdout"] == "42\n"
    assert envelope["metadata"]["request_metadata"] == METADATA
    assert result.metadata["execution_id"] == "metadata-execution"
    _langgraph_probe(portable)
    _verify_requests(provider)
    return provider


def _verify_requests(provider: RecordingProvider) -> None:
    """Check static policy and managed identity survived both native adapters."""
    for request in provider.requests:
        assert sandbox_metadata_to_dict(request.metadata) == METADATA
        assert request.context.user_id == "live-user"
        assert request.context.session_id == "session-a"
        assert request.context.agent_name == "live-sandbox"
        assert request.context.invocation_id == "live-call"
        assert request.timeout_seconds == TIMEOUT_SECONDS


def _timeout_probe(backend: Any) -> dict[str, Any]:
    """Check host-enforced timeout, discarded state, and native recovery."""
    previous_id = backend._executor._container.id
    started = time.monotonic()
    timed = backend.execute(SandboxRequest(
        code="import time; time.sleep(5)", timeout_seconds=1,
    ))
    elapsed = time.monotonic() - started
    assert "timed out after 1 seconds" in timed.stderr
    assert elapsed < 4
    assert backend._executor is None
    recovered = backend.execute(SandboxRequest(code="print('recovered')"))
    assert recovered.stdout == "recovered\n" and recovered.stderr == ""
    assert backend._executor.timeout_seconds == TIMEOUT_SECONDS
    assert backend._executor._container.id != previous_id
    return {"elapsed_seconds": round(elapsed, 3), "recovered": True,
            "aborted_container_discarded": previous_id}


def _filesystem_probe(backend: Any) -> dict[str, Any]:
    """Make the native cross-session filesystem behavior explicit, not an isolation claim."""
    backend.execute(SandboxRequest(
        code="from pathlib import Path; Path('/tmp/harnest-live-marker').write_text('session-a')",
        context=SandboxContext(user_id="user-a", session_id="session-a"),
    ))
    result = backend.execute(SandboxRequest(
        code="from pathlib import Path; print(Path('/tmp/harnest-live-marker').read_text())",
        context=SandboxContext(user_id="user-b", session_id="session-b"),
    ))
    assert result.stdout == "session-a\n"
    return {"cross_session_files_persist": True, "harnest_session_isolation": False}


def _parallel_probe(backend: Any) -> None:
    """Verify concurrent real calls are safely serialized by the shared backend."""
    requests = [SandboxRequest(code=f"print({value})") for value in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(backend.execute, requests))
    assert [result.stdout for result in results] == ["0\n", "1\n", "2\n"]


def _files_probe(backend: Any) -> None:
    """Confirm unsupported inputs fail before implying files reached the container."""
    try:
        backend.execute(SandboxRequest(
            code="print('must not execute')",
            input_files=(SandboxFile("input.txt", b"harmless"),),
        ))
    except ValueError as exc:
        assert "does not support input_files" in str(exc)
        return
    raise AssertionError("native Docker unexpectedly accepted unsupported input files")


def _execute_probes(args: argparse.Namespace) -> dict[str, Any]:
    """Leave room for shared admission while independently testing a one-second deadline."""
    definition = Sandbox.container(
        image=args.image, base_url=args.base_url, network=False,
        timeout_seconds=TIMEOUT_SECONDS, options=OPTIONS, metadata=METADATA,
    )
    adk_details = _adk_probe(definition)
    provider = _metadata_probe(definition)
    backend = provider.backend
    timeout = _timeout_probe(backend)
    filesystem = _filesystem_probe(backend)
    _parallel_probe(backend)
    _files_probe(backend)
    return {
        "status": "passed", "mocked": False,
        "containers": [adk_details, _container_details(backend)],
        "metadata": METADATA, "metadata_requests": len(provider.requests),
        "timeout": timeout, "filesystem": filesystem,
        "checks": ["adk", "langgraph_sync", "langgraph_async", "provider_metadata",
                   "identity", "unicode_stdout_stderr", "native_options", "timeout",
                   "recovery", "concurrency", "input_file_rejection", "host_config"],
    }


def _child(args: argparse.Namespace) -> None:
    """Emit ownership immediately so failed probes can clean up exact resources."""
    native_run = ContainerCollection.run

    def record_run(collection: Any, *values: Any, **options: Any) -> Any:
        """Record only containers actually returned by this probe's native constructor."""
        container = native_run(collection, *values, **options)
        print(OWNED_PREFIX + container.id, flush=True)
        return container

    with patch.object(ContainerCollection, "run", record_run):
        print("HARNEST_LIVE_REPORT=" + json.dumps(_execute_probes(args)), flush=True)


def _run(args: argparse.Namespace) -> None:
    """Observe cleanup from a separate process without removing unrelated containers."""
    command = [sys.executable, str(Path(__file__).resolve()), "--child",
               "--image", args.image, "--base-url", args.base_url]
    client = docker.DockerClient(base_url=args.base_url)
    output = ""
    try:
        child = subprocess.run(command, capture_output=True, text=True, timeout=90)
        output = child.stdout
        if child.returncode:
            raise RuntimeError(f"Live child failed:\n{child.stdout}\n{child.stderr}")
        line = next(item for item in output.splitlines() if item.startswith("HARNEST_LIVE_REPORT="))
        report = json.loads(line.partition("=")[2])
        report["owned_containers"] = _owned_ids(output)
        _verify_cleanup(client, report)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b""
        output = output.decode() if isinstance(output, bytes) else output
        raise
    finally:
        _remove_owned(client, _owned_ids(output))
        client.close()
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def _verify_cleanup(client: Any, report: dict[str, Any]) -> None:
    """Assert native atexit removed exactly the observed test-owned containers."""
    for container_id in report["owned_containers"]:
        try:
            client.containers.get(container_id)
        except docker.errors.NotFound:
            continue
        raise AssertionError("native atexit left a test container behind")
    report["native_atexit_cleanup"] = "passed"


def _owned_ids(output: str) -> list[str]:
    """Read only ownership records emitted by this probe's child process."""
    return [line.removeprefix(OWNED_PREFIX) for line in output.splitlines()
            if line.startswith(OWNED_PREFIX)]


def _remove_owned(client: Any, identifiers: list[str]) -> None:
    """Recover failed probe resources without touching unrelated Docker containers."""
    for container_id in identifiers:
        try:
            client.containers.get(container_id).remove(force=True, v=True)
        except docker.errors.NotFound:
            pass


def main() -> None:
    """Require explicit Docker configuration for this network-capable live probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", default=".cache/sandbox-live-container.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        _child(args)
    else:
        _run(args)


if __name__ == "__main__":
    main()

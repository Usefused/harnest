"""Opt-in real Docker verification for extension-owned isolation policy."""

from __future__ import annotations

import os

import pytest

from harnest.sandbox import (
    SandboxBudget,
    SandboxContext,
    SandboxNetworkPolicy,
    SandboxRequest,
    SandboxStatus,
)
from harnest_extension_docker.extension import docker_sandbox


pytestmark = pytest.mark.skipif(
    os.environ.get("HARNEST_TEST_DOCKER") != "1",
    reason="set HARNEST_TEST_DOCKER=1 for real Docker isolation checks",
)


def _backend(**settings: object):
    """Build the checked-in extension provider against a real Docker daemon."""

    return docker_sandbox(image="python:3.12-slim", **settings).build()


def _request(
    code: str,
    *,
    context: SandboxContext | None = None,
    timeout_seconds: int | None = None,
) -> SandboxRequest:
    """Mirror the managed adapter's immutable no-network request policy."""

    return SandboxRequest(
        code,
        context=SandboxContext() if context is None else context,
        timeout_seconds=timeout_seconds,
        network_policy=SandboxNetworkPolicy.none(),
    )


def test_kernel_policy_and_scoped_scratch() -> None:
    """Verify kernel settings, read-only root, and per-call process cleanup."""

    backend = _backend(
        scope="session",
        budget=SandboxBudget(
            cpu=0.5,
            memory_bytes=64 * 1024 * 1024,
            pids=16,
            scratch_bytes=1024 * 1024,
        ),
    )
    identity = SandboxContext("worker", "turn", "alice", "session")
    try:
        result = backend.execute(
            _request(
                "import os; print(os.getuid()); open('/tmp/owned', 'w').write('ok')",
                context=identity,
            )
        )
        assert result.stdout.strip() == "65534"
        owner = next(iter(backend._backend._scopes.values()))
        owner._guard.container.reload()
        _assert_host_policy(owner._guard.container.attrs["HostConfig"])
        denied = backend.execute(
            _request(
                "open('/escape', 'w').write('bad')", context=identity
            )
        )
        assert denied.status == SandboxStatus.FAILED
        assert "Read-only file system" in denied.stderr
        cleared = backend.execute(
            _request(
                "import os; print(os.path.exists('/tmp/owned'))", context=identity
            )
        )
        assert cleared.stdout.strip() == "False"
    finally:
        backend.close()


def _assert_host_policy(host: dict[str, object]) -> None:
    """Check the Docker-inspected host policy in one focused assertion helper."""

    expected = {
        "NanoCpus": 500_000_000,
        "Memory": 64 * 1024 * 1024,
        "PidsLimit": 16,
        "ReadonlyRootfs": True,
        "NetworkMode": "none",
    }
    assert {name: host[name] for name in expected} == expected
    assert host["MemorySwap"] == host["Memory"]


def test_deadline_output_limit_and_fresh_isolation() -> None:
    """Verify execution deadlines separately from fresh-container cold starts."""

    backend = _backend(timeout_seconds=15, max_output_bytes=1024)
    try:
        failed = backend.execute(_request("raise SystemExit(7)"))
        assert (failed.status, failed.exit_code) == (SandboxStatus.FAILED, 7)
        overflow = backend.execute(_request("print('x' * 100000)"))
        assert overflow.status == SandboxStatus.OUTPUT_LIMIT_EXCEEDED
        backend.execute(
            _request("open('/tmp/private', 'w').write('secret')")
        )
        result = backend.execute(
            _request(
                "import os; print(os.path.exists('/tmp/private'))"
            )
        )
        assert result.stdout.strip() == "False"
    finally:
        backend.close()

    # Reuse one already-started container so this assertion measures the host
    # execution watchdog rather than Docker Desktop image/container cold start.
    identity = SandboxContext("worker", "turn", "alice", "deadline-session")
    deadline_backend = _backend(timeout_seconds=15, scope="session")
    try:
        ready = deadline_backend.execute(
            _request("print('ready')", context=identity)
        )
        assert ready.stdout.strip() == "ready"
        timed_out = deadline_backend.execute(
            _request("while True: pass", context=identity, timeout_seconds=2)
        )
        assert timed_out.status == SandboxStatus.TIMED_OUT
    finally:
        deadline_backend.close()

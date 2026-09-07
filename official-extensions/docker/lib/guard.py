"""Host-owned Docker transport, output, deadline, and cleanup bounds."""

from __future__ import annotations

import atexit
import socket
import threading
import time
from typing import Any, Callable

from harnest.sandbox import SandboxCancelledError, SandboxStatus, control

from .socket_stream import collect_output
from .telemetry import docker_operation


class OutputLimitError(RuntimeError):
    """The native execution exceeded its combined output budget."""


class SandboxCleanupError(RuntimeError):
    """An owned Docker resource may remain available for cleanup retry."""

    def __init__(self, executor: Any) -> None:
        """Attach only the cleanup handle, never private provider details."""

        super().__init__("sandbox Docker resource cleanup could not be confirmed")
        self.failed_executor = executor


class GuardedContainer:
    """Preserve container inspection while bounding its execution transport."""

    def __init__(self, owner: Any, container: Any, limit: int) -> None:
        """Keep ownership explicit so aborts cannot target other containers."""

        self.owner = owner
        self.container = container
        self.limit = limit
        self.failed = False
        self.status = SandboxStatus.SUCCEEDED
        self.exit_code: int | None = None
        self.removed = False
        self.stopped = False
        self._cleanup_lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        """Delegate native inspection properties without widening execution APIs."""

        return getattr(self.container, name)

    def exec_run(self, cmd: Any, *, demux: bool = False) -> Any:
        """Run the native command under an independent host deadline."""

        from docker.models.containers import ExecResult

        if self.failed:
            raise RuntimeError(
                "sandbox executor cannot be reused after an aborted execution"
            )
        current = control.current()
        deadline = time.monotonic() + self.owner.timeout_seconds
        done = threading.Event()
        aborted = threading.Event()
        state: dict[str, Any] = {}

        def check() -> None:
            """Apply the captured execution authority to worker operations."""

            _check_execution(current, deadline, aborted)

        worker = threading.Thread(
            target=_run_worker,
            args=(self, cmd, demux, check, state, done),
            daemon=True,
        )
        check()
        worker.start()
        try:
            while not done.wait(0.025):
                check()
            check()
            if "error" in state:
                raise state["error"]
            return self._finish(state["result"])
        except BaseException as error:
            return self._abort(error, aborted, state, demux, ExecResult)

    def _abort(
        self,
        error: BaseException,
        aborted: threading.Event,
        state: dict[str, Any],
        demux: bool,
        result_type: Any,
    ) -> Any:
        """Terminate owned work and translate bounded terminal conditions."""

        aborted.set()
        self.failed = True
        _close_socket(state.get("socket"))
        self.close()
        if isinstance(error, TimeoutError):
            self.status = SandboxStatus.TIMED_OUT
            self.exit_code = 124
            return result_type(124, (b"", b"") if demux else b"")
        if isinstance(error, OutputLimitError):
            self.status = SandboxStatus.OUTPUT_LIMIT_EXCEEDED
            self.exit_code = 1
            message = (
                b"Sandbox output exceeded max_output_bytes; execution terminated."
            )
            return result_type(1, (b"", message) if demux else message)
        self.status = (
            SandboxStatus.CANCELLED
            if isinstance(error, SandboxCancelledError)
            else SandboxStatus.FAILED
        )
        raise error

    def _finish(self, result: Any) -> Any:
        """Remove work after native timeout while preserving authored exit codes."""

        self.exit_code = result.exit_code
        self.status = (
            SandboxStatus.SUCCEEDED
            if result.exit_code == 0
            else SandboxStatus.FAILED
        )
        if result.exit_code == getattr(self.owner, "native_timeout_exit_code", 124):
            self.status = SandboxStatus.TIMED_OUT
            self.close()
        return result

    def resume(self) -> None:
        """Restart the same writable layer only after its prior work was stopped."""

        if self.stopped:
            _bounded_container_call(self.container, self.container.start)
            self.stopped = False

    def quiesce(self) -> None:
        """Stop detached children after success without discarding the filesystem."""

        _bounded_container_call(
            self.container, lambda: self.container.stop(timeout=0)
        )
        self.stopped = True

    def close(self) -> None:
        """Remove only this container and retain poisoned state on uncertainty."""

        self.failed = True
        with self._cleanup_lock:
            if self.removed:
                return
            try:
                _remove_owned_container(self.container)
            except Exception as error:
                from docker.errors import NotFound

                if not isinstance(error, NotFound):
                    raise SandboxCleanupError(self.owner) from None
            self.removed = True
            self.owner._container = None
            forget = getattr(self.owner, "_forget_container", None)
            if callable(forget):
                forget(self.container)


def _check_execution(
    current: Any, deadline: float, aborted: threading.Event
) -> None:
    """Recheck admission before every Docker operation and stream read."""

    if aborted.is_set():
        raise RuntimeError("sandbox execution was aborted")
    if current is not None:
        current.check()
    if time.monotonic() >= deadline:
        raise TimeoutError("sandbox execution deadline exceeded")


def _run_worker(
    guard: GuardedContainer,
    cmd: Any,
    demux: bool,
    check: Callable[[], None],
    state: dict[str, Any],
    done: threading.Event,
) -> None:
    """Isolate blocking Docker calls without renewing execution authority."""

    try:
        state["result"] = _exec_transport(guard, cmd, demux, check, state)
    except BaseException as error:
        state["error"] = error
    finally:
        _close_socket(state.get("socket"))
        done.set()


def _exec_transport(
    guard: GuardedContainer,
    cmd: Any,
    demux: bool,
    check: Callable[[], None],
    state: dict[str, Any],
) -> Any:
    """Read raw multiplexed sockets so output cannot be joined unboundedly."""

    from docker.models.containers import ExecResult

    api = guard.container.client.api
    check()
    guard.resume()
    check()
    created = api.exec_create(
        guard.container.id, cmd, stdout=True, stderr=True, tty=False
    )
    check()
    raw = api.exec_start(created["Id"], socket=True, tty=False)
    state["socket"] = raw
    check()
    output = collect_output(raw, guard.limit, check, OutputLimitError)
    check()
    status = api.exec_inspect(created["Id"])
    check()
    if demux:
        guard.quiesce()
        check()
    return ExecResult(status["ExitCode"], output if demux else b"".join(output))


def _close_socket(raw: Any) -> None:
    """Release a Docker stream without hiding the execution's primary failure."""

    if raw is None:
        return
    underlying = getattr(raw, "_sock", raw)
    try:
        underlying.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    try:
        raw.close()
    except Exception:
        pass
    response = getattr(raw, "_response", None)
    if response is not None:
        try:
            response.close()
        except Exception:
            pass


def _remove_owned_container(container: Any) -> None:
    """Bound cleanup I/O so daemon loss cannot hold admission indefinitely."""

    with control.cleanup(5) as cleanup:
        _bounded_container_call(
            container,
            lambda: container.remove(force=True, v=True),
            cleanup.remaining(),
        )


def _bounded_container_call(
    container: Any, operation: Callable[[], None], timeout: float = 2.0
) -> None:
    """Give lifecycle requests a finite transport bound and restore client state."""

    api = container.client.api
    previous = api.timeout
    api.timeout = min(previous or timeout, timeout)
    try:
        operation()
    finally:
        api.timeout = previous


def _remove_owned_network(network: Any) -> None:
    """Remove an exact owned network after every attached container is gone."""

    with control.cleanup(5) as cleanup:
        _bounded_container_call(
            network,
            network.remove,
            cleanup.remaining(),
        )


def close_guarded_executor(executor: Any) -> None:
    """Release all exact resources in reverse order and retain failed ownership."""

    topology_id = getattr(executor, "_topology_id", "unknown")
    with docker_operation(
        "sandbox.cleanup",
        topology_id=topology_id,
        attributes={
            "harnest.docker.container_count": len(
                getattr(executor, "_owned_containers", ())
            ),
            "harnest.docker.network_count": len(
                getattr(executor, "_owned_networks", ())
            ),
        },
    ):
        executor._guard_poisoned = True
        try:
            cleanup_failed = _cleanup_executor_resources(executor)
        except Exception:
            cleanup_failed = True
        if cleanup_failed or getattr(executor, "_startup_uncertain", False):
            raise SandboxCleanupError(executor)
        atexit.unregister(executor.close)


def _cleanup_executor_resources(executor: Any) -> bool:
    """Share one five-second cleanup deadline across the complete topology."""

    with control.cleanup(5):
        return _cleanup_executor_resources_with_control(executor)


def _cleanup_executor_resources_with_control(executor: Any) -> bool:
    """Attempt every owned removal while preserving any failed exact handles."""

    cleanup_failed = _close_primary_container(executor)
    cleanup_failed = _close_tracked_containers(executor) or cleanup_failed
    cleanup_failed = _close_tracked_networks(executor) or cleanup_failed
    client = getattr(executor, "_client", None)
    if client is not None and not cleanup_failed:
        client.close()
        executor._client = None
    return cleanup_failed


def _close_primary_container(executor: Any) -> bool:
    """Close the guarded primary or an older partial single-container owner."""

    guard = getattr(executor, "_guard", None)
    if guard is not None:
        try:
            guard.close()
        except SandboxCleanupError:
            return True
        return False
    if getattr(executor, "_owned_containers", None):
        return False
    container = getattr(executor, "_container", None)
    if container is None:
        return False
    if not _removed_or_absent(lambda: _remove_owned_container(container)):
        return True
    executor._container = None
    return False


def _close_tracked_containers(executor: Any) -> bool:
    """Remove every remaining exact container handle in reverse creation order."""

    cleanup_failed = False
    for container in reversed(tuple(getattr(executor, "_owned_containers", ()))):
        if _removed_or_absent(lambda: _remove_owned_container(container)):
            executor._forget_container(container)
        else:
            cleanup_failed = True
    return cleanup_failed


def _close_tracked_networks(executor: Any) -> bool:
    """Remove owned networks after every removable container has been handled."""

    cleanup_failed = False
    for network in reversed(tuple(getattr(executor, "_owned_networks", ()))):
        if _removed_or_absent(lambda: _remove_owned_network(network)):
            executor._forget_network(network)
        else:
            cleanup_failed = True
    return cleanup_failed


def _removed_or_absent(operation: Callable[[], None]) -> bool:
    """Treat provider-confirmed absence as successful exact-resource cleanup."""

    try:
        operation()
    except Exception as error:
        from docker.errors import NotFound

        return isinstance(error, NotFound)
    return True


def guard_failed(executor: Any) -> bool:
    """Report whether an executor can safely accept another command."""

    guard = getattr(executor, "_guard", None)
    return bool(getattr(executor, "_guard_poisoned", False)) or (
        guard is not None and guard.failed
    )


__all__ = [
    "GuardedContainer",
    "OutputLimitError",
    "SandboxCleanupError",
    "close_guarded_executor",
    "guard_failed",
]

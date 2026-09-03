"""Host-owned bounds around the native ADK Docker execution transport."""

from __future__ import annotations

import atexit
import socket
import threading
import time
from typing import Any, Callable

from .sandbox_control import current_control
from .sandbox_socket import collect_output
from .sandbox_startup import OwnedStartupClient, check_startup


class OutputLimitError(RuntimeError):
    """The native execution exceeded its combined output budget."""


class SandboxCleanupError(RuntimeError):
    """An owned container may remain; keep its executor available for cleanup."""

    def __init__(self, executor: Any) -> None:
        """Attach only an internal handle, never provider details to the message."""
        super().__init__("sandbox container cleanup could not be confirmed")
        self.failed_executor = executor


class GuardedContainer:
    """Preserve native container behavior while bounding its exec transport."""

    def __init__(self, owner: Any, container: Any, limit: int) -> None:
        """Keep ownership explicit so aborts never target unrelated containers."""
        self.owner, self.container, self.limit = owner, container, limit
        self.failed = False
        self.removed = False
        self.stopped = False
        self._cleanup_lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        """Delegate native inspection properties without widening execution APIs."""
        return getattr(self.container, name)

    def exec_run(self, cmd: Any, *, demux: bool = False) -> Any:
        """Run the unchanged native command under an independent host deadline."""
        from docker.models.containers import ExecResult

        if self.failed:
            raise RuntimeError("sandbox executor cannot be reused after an aborted execution")
        control = current_control()
        deadline = time.monotonic() + self.owner.timeout_seconds
        done, aborted = threading.Event(), threading.Event()
        state: dict[str, Any] = {}
        check = lambda: _check_execution(control, deadline, aborted)
        worker = threading.Thread(
            target=_run_worker, args=(self, cmd, demux, check, state, done), daemon=True,
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
        except BaseException as exc:
            aborted.set()
            self.failed = True
            _close_socket(state.get("socket"))
            self.close()
            if isinstance(exc, TimeoutError):
                return ExecResult(124, (b"", b"") if demux else b"")
            if isinstance(exc, OutputLimitError):
                message = b"Sandbox output exceeded max_output_bytes; execution terminated."
                return ExecResult(1, (b"", message) if demux else message)
            raise

    def _finish(self, result: Any) -> Any:
        """Native timeouts also require container removal to stop detached children."""
        if result.exit_code == 124:
            self.close()
        return result

    def resume(self) -> None:
        """Restart the same writable layer only after the prior run was quiesced."""
        if self.stopped:
            _bounded_container_call(self.container, self.container.start)
            self.stopped = False

    def quiesce(self) -> None:
        """Kill detached children on success without discarding the filesystem."""
        # A native supervisor can be killed or escaped by code with its UID.
        # Only the host/container boundary can establish that no work remains.
        _bounded_container_call(self.container, lambda: self.container.stop(timeout=0))
        self.stopped = True

    def close(self) -> None:
        """Remove only this owned container; retain poisoned state on uncertainty."""
        self.failed = True
        with self._cleanup_lock:
            if self.removed:
                return
            # Docker requests retain finite transport timeouts. A failed removal
            # must leave the owner poisoned instead of allowing another command.
            try:
                _remove_owned_container(self.container)
            except Exception as exc:
                from docker.errors import NotFound

                if not isinstance(exc, NotFound):
                    raise SandboxCleanupError(self.owner) from None
            self.removed = True
            self.owner._container = None


def _check_execution(control: Any, deadline: float, aborted: threading.Event) -> None:
    """Recheck admission before every operation, including a delayed SDK response."""
    if aborted.is_set():
        raise RuntimeError("sandbox execution was aborted")
    if control is not None:
        control.check()
    if time.monotonic() >= deadline:
        raise TimeoutError("sandbox execution deadline exceeded")


def _run_worker(
    guard: GuardedContainer, cmd: Any, demux: bool, check: Callable[[], None],
    state: dict[str, Any], done: threading.Event,
) -> None:
    """Isolate blocking Docker control calls without giving them renewed admission."""
    try:
        state["result"] = _exec_transport(guard, cmd, demux, check, state)
    except BaseException as exc:
        state["error"] = exc
    finally:
        _close_socket(state.get("socket"))
        done.set()


def _exec_transport(
    guard: GuardedContainer, cmd: Any, demux: bool, check: Callable[[], None],
    state: dict[str, Any],
) -> Any:
    """Use raw Docker sockets so no SDK frame or joined output is unbounded."""
    from docker.models.containers import ExecResult

    api = guard.container.client.api
    check()
    guard.resume()
    check()
    created = api.exec_create(guard.container.id, cmd, stdout=True, stderr=True, tty=False)
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
    """Best-effort stream release does not hide the execution's primary failure."""
    if raw is not None:
        underlying = getattr(raw, "_sock", raw)
        try:
            # Closing a SocketIO wrapper alone need not interrupt a read already
            # blocked in another thread; shut down the actual socket first.
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
    """Bound cleanup I/O so an unavailable daemon cannot hold admission forever."""
    _bounded_container_call(container, lambda: container.remove(force=True, v=True))


def _bounded_container_call(container: Any, operation: Callable[[], None]) -> None:
    """Give lifecycle requests a finite I/O bound without changing provider options."""
    api = container.client.api
    previous = api.timeout
    api.timeout = min(previous or 2.0, 2.0)
    try:
        operation()
    finally:
        api.timeout = previous


def create_guarded_executor(config: dict[str, Any], max_output_bytes: int) -> Any:
    """Construct native ADK with scoped transport and failure-safe startup cleanup."""
    from google.adk.code_executors import ContainerCodeExecutor
    from pydantic import PrivateAttr

    class GuardedExecutor(ContainerCodeExecutor):
        """Native configuration and Python wrapper with host-owned transport guards."""

        _guard: Any = PrivateAttr(default=None)
        _guard_poisoned: bool = PrivateAttr(default=False)
        _startup_uncertain: bool = PrivateAttr(default=False)

        def __init__(self, **values: Any) -> None:
            """Catch native verification failures before its cleanup registration."""
            try:
                super().__init__(**values)
            except BaseException:
                # Native validation precedes resource acquisition; Pydantic
                # private attributes do not yet exist when validation fails.
                if getattr(self, "__pydantic_private__", None) is not None:
                    self._guard_poisoned = True
                    close_guarded_executor(self)
                raise

        def _verify_python_installation(self) -> None:
            """Protect even startup probes from image-controlled output and hangs."""
            self._guard = GuardedContainer(self, self._container, max_output_bytes)
            self._container = self._guard
            super()._verify_python_installation()

        def _ContainerCodeExecutor__init_container(self) -> None:
            """Capture native create ownership before an image's start can fail."""
            check_startup()
            self._client = OwnedStartupClient(self, self._client)
            super()._ContainerCodeExecutor__init_container()
            check_startup()

        def _ContainerCodeExecutor__cleanup_container(self) -> None:
            """Make native atexit cleanup safe after deadline or output termination."""
            close_guarded_executor(self)

    return GuardedExecutor(**config)


def close_guarded_executor(executor: Any) -> None:
    """Release native resources idempotently, including partially constructed ones."""
    executor._guard_poisoned = True
    guard = getattr(executor, "_guard", None)
    if guard is not None:
        guard.close()
    else:
        container = getattr(executor, "_container", None)
        if container is not None:
            try:
                _remove_owned_container(container)
            except Exception:
                raise SandboxCleanupError(executor) from None
            executor._container = None
    client = getattr(executor, "_client", None)
    if client is not None:
        client.close()
        executor._client = None
    if getattr(executor, "_startup_uncertain", False):
        raise SandboxCleanupError(executor)
    atexit.unregister(executor._ContainerCodeExecutor__cleanup_container)


def guard_failed(executor: Any) -> bool:
    """Report when an executor cannot safely accept another native command."""
    guard = getattr(executor, "_guard", None)
    return bool(getattr(executor, "_guard_poisoned", False)) or (guard is not None and guard.failed)

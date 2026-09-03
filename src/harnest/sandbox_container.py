"""Portable access to ADK's native Docker execution and lifecycle policy."""

from __future__ import annotations

import copy
import threading
from typing import Any, Mapping

from .sandbox_control import execution_control
from .sandbox_runtime import SandboxInputFilesUnsupportedError
from .sandbox_guard import close_guarded_executor, create_guarded_executor, guard_failed
from .sandbox_types import SandboxRequest, SandboxResult, validate_timeout


_EXECUTOR_OPTIONS = frozenset({
    "error_retry_attempts", "code_block_delimiters", "execution_result_delimiters",
    "stateful", "optimize_data_file",
})


def _validate_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Preserve supported native properties without silently accepting ignored fields."""
    extra = copy.deepcopy(dict(options or {}))
    unknown = extra.keys() - _EXECUTOR_OPTIONS
    if unknown:
        raise ValueError(f"unsupported container sandbox options: {', '.join(sorted(unknown))}")
    for name in ("stateful", "optimize_data_file"):
        if name in extra and extra[name] is not False:
            raise ValueError(f"container sandbox {name} must remain False")
    return extra


def create_container_backend(
    *, image: str | None = None, docker_path: str | None = None,
    base_url: str | None = None, network: bool = False,
    timeout_seconds: int = 300, options: Mapping[str, Any] | None = None,
    max_output_bytes: int = 1_048_576,
) -> ContainerSandboxBackend:
    """Validate configuration without contacting Docker or constructing its executor."""
    validate_timeout(timeout_seconds)
    if timeout_seconds is None:
        raise ValueError("container sandbox timeout_seconds must be a positive integer")
    if bool(image) == bool(docker_path):
        raise ValueError("container sandbox requires exactly one of image or docker_path")
    for name, value in (("image", image), ("docker_path", docker_path), ("base_url", base_url)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"container sandbox {name} must be non-empty text")
    if not isinstance(network, bool):
        raise TypeError("container sandbox network must be a boolean")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("container sandbox max_output_bytes must be a positive integer")
    config = _validate_options(options)
    config.update(
        image=image, docker_path=docker_path, base_url=base_url,
        network_enabled=network, timeout_seconds=timeout_seconds,
    )
    return ContainerSandboxBackend(config, max_output_bytes)


class ContainerSandboxBackend:
    """Reuse one native executor whose Docker lifecycle remains framework-owned.

    Harnest does not add per-session filesystem isolation or CPU/memory limits.
    Native ADK owns execution and networking; the host-side transport guard
    enforces output/deadline bounds and cleans up aborted or failed starts.
    """

    def __init__(self, config: dict[str, Any], max_output_bytes: int = 1_048_576) -> None:
        """Snapshot provider options and defer native construction until execution."""
        self._config = copy.deepcopy(config)
        self._executor: Any = None
        self._lock = threading.Lock()
        self._max_output_bytes = max_output_bytes
        self._closed = False

    def new_backend(self) -> ContainerSandboxBackend:
        """Copy validated configuration without sharing a native executor across adapters."""
        return ContainerSandboxBackend(self._config, self._max_output_bytes)

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Adapt requests while serializing shared native executor configuration."""
        if request.input_files:
            # Native Docker execution ignores these; fail rather than imply
            # that model-supplied files were made available in the container.
            raise SandboxInputFilesUnsupportedError("container sandbox does not support input_files")
        timeout = min(self._config["timeout_seconds"], request.timeout_seconds or self._config["timeout_seconds"])
        # Admission is part of the deadline. A revoked/cancelled invocation
        # must not start merely because an earlier execution released the lock.
        with execution_control(timeout) as control, control.acquire(self._lock):
            if self._closed:
                raise RuntimeError("container sandbox is closed")
            self._retire_failed_executor()
            control.check()
            self._ensure_executor()
            control.check()
            try:
                return self._execute(request)
            finally:
                self._retire_failed_executor()

    def close(self) -> None:
        """Revoke new execution and remove this backend's exact owned container."""
        with self._lock:
            self._closed = True
            if self._executor is not None:
                close_guarded_executor(self._executor)
                self._executor = None

    def _ensure_executor(self) -> None:
        """Remember failed-start cleanup ownership until termination is confirmed."""
        if self._executor is not None:
            return
        try:
            self._executor = _create_executor(copy.deepcopy(self._config), self._max_output_bytes)
        except Exception as error:
            self._executor = getattr(error, "failed_executor", None)
            raise

    def _retire_failed_executor(self) -> None:
        """Never replace a poisoned container until its cleanup has succeeded."""
        if self._executor is not None and guard_failed(self._executor):
            close_guarded_executor(self._executor)
            self._executor = None

    def _execute(self, request: SandboxRequest) -> SandboxResult:
        """Allow shorter request deadlines without extending the configured maximum."""
        from google.adk.code_executors.code_execution_utils import CodeExecutionInput

        timeout = self._config["timeout_seconds"]
        self._executor.timeout_seconds = min(timeout, request.timeout_seconds or timeout)
        try:
            result = self._executor.execute_code(
                None, CodeExecutionInput(code=request.code, execution_id=request.execution_id),
            )
            return SandboxResult(stdout=result.stdout, stderr=result.stderr)
        finally:
            # The native executor is shared, so a previous shorter deadline
            # must not leak into another invocation after success or failure.
            self._executor.timeout_seconds = timeout


def _create_executor(config: dict[str, Any], max_output_bytes: int) -> Any:
    """Use native ADK with bounded host-side transport and failure cleanup."""
    if config["image"] is None:
        # A Dockerfile build uses the native executor's default image tag.
        config.pop("image")
    return create_guarded_executor(config, max_output_bytes)

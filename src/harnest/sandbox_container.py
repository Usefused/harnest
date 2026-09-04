"""Identity-scoped Docker execution shared by ADK and LangGraph."""

from __future__ import annotations

import copy
from collections import OrderedDict
import threading
from typing import Any, Mapping

from .sandbox_control import execution_control
from .sandbox_policy import SandboxBudget, scope_key, validate_scope
from .sandbox_runtime import SandboxInputFilesUnsupportedError
from .sandbox_guard import close_guarded_executor, guard_failed
from .sandbox_docker import create_docker_executor
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
    scope: str = "execution", budget: SandboxBudget | None = None, max_scopes: int = 8,
) -> ContainerSandboxBackend:
    """Validate identity scopes and hard budgets without contacting Docker."""
    _validate_container_settings(image, docker_path, base_url, network, timeout_seconds, max_output_bytes)
    validate_scope(scope, max_scopes)
    budget = SandboxBudget() if budget is None else budget
    if not isinstance(budget, SandboxBudget):
        raise TypeError("sandbox budget must be SandboxBudget")
    config = _validate_options(options)
    config.update(
        image=image, docker_path=docker_path, base_url=base_url,
        network_enabled=network, timeout_seconds=timeout_seconds,
        harnest_resource_limits=budget.docker_options(),
    )
    return ContainerSandboxBackend(config, max_output_bytes, scope=scope, max_scopes=max_scopes)


def _validate_container_settings(image: Any, docker_path: Any, base_url: Any,
                                 network: Any, timeout_seconds: Any, max_output_bytes: Any) -> None:
    """Keep provider policy validation separate from acquiring Docker resources."""
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


class ContainerSandboxBackend:
    """Own bounded, identity-scoped containers behind the portable Docker provider.

    The default execution scope never retains files between calls. Retained
    scopes are a bounded LRU cache, not durable session storage.
    """

    def __init__(self, config: dict[str, Any], max_output_bytes: int = 1_048_576, *,
                 scope: str = "execution", max_scopes: int = 8) -> None:
        """Snapshot provider options and defer Docker construction until execution."""
        self._config = copy.deepcopy(config)
        self._executor: Any = None
        self._lock = threading.Lock()
        self._max_output_bytes = max_output_bytes
        self._closed = False
        self._scope, self._max_scopes = scope, max_scopes
        self._scopes: OrderedDict[tuple[str, ...], Any] = OrderedDict()

    def new_backend(self) -> ContainerSandboxBackend:
        """Copy validated policy without sharing container ownership across applications."""
        return ContainerSandboxBackend(self._config, self._max_output_bytes,
                                       scope=self._scope, max_scopes=self._max_scopes)

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Authorize identity-scoped reuse and enforce cancellation-aware admission."""
        if request.input_files:
            # File transfer is unsupported; fail rather than imply
            # that model-supplied files were made available in the container.
            raise SandboxInputFilesUnsupportedError("container sandbox does not support input_files")
        key = scope_key(self._scope, request.context)
        timeout = min(self._config["timeout_seconds"], request.timeout_seconds or self._config["timeout_seconds"])
        with execution_control(timeout) as control, control.acquire(self._lock):
            if self._closed:
                raise RuntimeError("container sandbox is closed")
            self._retire_failed_executor()
            self._select_scope(key)
            try:
                control.check()
                self._ensure_executor()
                control.check()
                return self._execute(request)
            finally:
                self._release_scope(key)

    def _select_scope(self, key: tuple[str, ...] | None) -> None:
        """Evict only quiescent owned containers before admitting another identity."""
        if key is not None and key in self._scopes:
            self._executor = self._scopes.pop(key)
            return
        if len(self._scopes) >= self._max_scopes:
            oldest = next(iter(self._scopes))
            # Keep the entry if cleanup fails, so ownership is never lost.
            close_guarded_executor(self._scopes[oldest])
            del self._scopes[oldest]

    def _release_scope(self, key: tuple[str, ...] | None) -> None:
        """Never retain default-scope files or recycle an aborted container."""
        if self._executor is None:
            return
        if key is None or guard_failed(self._executor):
            # A cleanup failure keeps _executor poisoned for the next admission.
            close_guarded_executor(self._executor)
        else:
            self._scopes[key] = self._executor
        self._executor = None

    def close(self) -> None:
        """Revoke admission and preserve every failed cleanup handle for retry."""
        with self._lock:
            self._closed = True
            if self._executor is not None:
                close_guarded_executor(self._executor)
                self._executor = None
            for key in tuple(self._scopes):
                close_guarded_executor(self._scopes[key])
                del self._scopes[key]

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
        timeout = self._config["timeout_seconds"]
        self._executor.timeout_seconds = min(timeout, request.timeout_seconds or timeout)
        try:
            return self._executor.execute(request)
        finally:
            # Retained scope executors are reused, so a previous shorter deadline
            # must not leak into another invocation after success or failure.
            self._executor.timeout_seconds = timeout


def _create_executor(config: dict[str, Any], max_output_bytes: int) -> Any:
    """Construct the shared Docker provider without importing either framework."""
    return create_docker_executor(config, max_output_bytes)

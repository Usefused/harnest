"""Identity-scoped Docker backend for the public Harnest sandbox contract."""

from __future__ import annotations

import copy
from collections import OrderedDict
from collections.abc import Mapping
import threading
from typing import Any

from harnest.sandbox import (
    SandboxBudget,
    SandboxInputFilesUnsupportedError,
    SandboxRequest,
    SandboxResult,
    control,
)

from .docker_runtime import create_docker_executor
from .guard import close_guarded_executor, guard_failed


_ADAPTER_OPTIONS = frozenset(
    {
        "error_retry_attempts",
        "code_block_delimiters",
        "execution_result_delimiters",
        "stateful",
        "optimize_data_file",
    }
)


def validate_adapter_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retain supported adapter fields and reject silently ignored settings."""

    extra = copy.deepcopy(dict(options or {}))
    unknown = extra.keys() - _ADAPTER_OPTIONS
    if unknown:
        raise ValueError(
            "unsupported Docker sandbox options: "
            + ", ".join(sorted(unknown))
        )
    for name in ("stateful", "optimize_data_file"):
        if name in extra and extra[name] is not False:
            raise ValueError(f"Docker sandbox {name} must remain False")
    return extra


def create_docker_backend(
    *,
    image: str | None = None,
    docker_path: str | None = None,
    base_url: str | None = None,
    network: bool = False,
    timeout_seconds: int = 300,
    max_output_bytes: int = 1_048_576,
    scope: str = "execution",
    budget: SandboxBudget | None = None,
    max_scopes: int = 8,
) -> "DockerSandboxBackend":
    """Validate provider settings without connecting to Docker."""

    _validate_settings(
        image,
        docker_path,
        base_url,
        network,
        timeout_seconds,
        max_output_bytes,
    )
    _validate_scope(scope, max_scopes)
    effective_budget = SandboxBudget() if budget is None else budget
    if not isinstance(effective_budget, SandboxBudget):
        raise TypeError("sandbox budget must be SandboxBudget")
    config = {
        "image": image,
        "docker_path": docker_path,
        "base_url": base_url,
        "network_enabled": network,
        "timeout_seconds": timeout_seconds,
        "harnest_resource_limits": _docker_budget_options(effective_budget),
    }
    return DockerSandboxBackend(
        config,
        max_output_bytes,
        scope=scope,
        max_scopes=max_scopes,
    )


def _docker_budget_options(budget: SandboxBudget) -> dict[str, Any]:
    """Translate provider-neutral budgets into enforced Docker create options."""

    return {
        "nano_cpus": int(budget.cpu * 1_000_000_000),
        "mem_limit": budget.memory_bytes,
        "memswap_limit": budget.memory_bytes,
        "pids_limit": budget.pids,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "user": "65534:65534",
        "working_dir": "/tmp",
        "tmpfs": {
            "/tmp": (
                f"rw,nosuid,nodev,size={budget.scratch_bytes},mode=1777"
            )
        },
    }


def _validate_settings(
    image: Any,
    docker_path: Any,
    base_url: Any,
    network: Any,
    timeout_seconds: Any,
    max_output_bytes: Any,
) -> None:
    """Reject ambiguous authority and unbounded provider configuration."""

    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("Docker sandbox timeout_seconds must be a positive integer")
    if bool(image) == bool(docker_path):
        raise ValueError(
            "Docker sandbox requires exactly one of image or docker_path"
        )
    _validate_optional_text("image", image)
    _validate_optional_text("docker_path", docker_path)
    _validate_optional_text("base_url", base_url)
    if not isinstance(network, bool):
        raise TypeError("Docker sandbox network must be a boolean")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError(
            "Docker sandbox max_output_bytes must be a positive integer"
        )


def _validate_optional_text(name: str, value: Any) -> None:
    """Reject empty or non-text values for optional Docker identifiers."""

    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"Docker sandbox {name} must be non-empty text")


def _validate_scope(scope: str, max_scopes: int) -> None:
    """Require a known retention scope and a finite identity-cache bound."""

    if scope not in ("execution", "invocation", "session"):
        raise ValueError(
            "Docker sandbox scope must be execution, invocation, or session"
        )
    if type(max_scopes) is not int or max_scopes <= 0:
        raise ValueError("Docker sandbox max_scopes must be a positive integer")


def _scope_key(scope: str, context: Any) -> tuple[str, ...] | None:
    """Bind retained containers to authenticated owner and invocation identity."""

    if scope == "execution":
        return None
    fields = [context.agent_name, context.user_id, context.session_id]
    if scope == "invocation":
        fields.append(context.invocation_id)
    if any(not isinstance(value, str) or not value for value in fields):
        required = ", and invocation identity" if scope == "invocation" else " identity"
        raise ValueError(
            f"Docker sandbox {scope} scope requires agent, user, session" + required
        )
    return tuple(fields)


class DockerSandboxBackend:
    """Own bounded identity-scoped containers behind a portable provider."""

    def __init__(
        self,
        config: dict[str, Any],
        max_output_bytes: int,
        *,
        scope: str,
        max_scopes: int,
    ) -> None:
        """Snapshot policy and defer Docker construction until execution."""

        self._config = copy.deepcopy(config)
        self._executor: Any = None
        self._lock = threading.Lock()
        self._max_output_bytes = max_output_bytes
        self._closed = False
        self._scope = scope
        self._max_scopes = max_scopes
        self._scopes: OrderedDict[tuple[str, ...], Any] = OrderedDict()

    def new_backend(self) -> "DockerSandboxBackend":
        """Copy policy without sharing containers across native adapters."""

        return DockerSandboxBackend(
            self._config,
            self._max_output_bytes,
            scope=self._scope,
            max_scopes=self._max_scopes,
        )

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """Authorize identity-scoped reuse under cancellation-aware admission."""

        if request.input_files:
            raise SandboxInputFilesUnsupportedError(
                "Docker sandbox does not support input_files"
            )
        key = _scope_key(self._scope, request.context)
        maximum = self._config["timeout_seconds"]
        timeout = min(maximum, request.timeout_seconds or maximum)
        with control.execute(timeout) as current, current.acquire(self._lock):
            if self._closed:
                raise RuntimeError("Docker sandbox is closed")
            self._retire_failed_executor()
            self._select_scope(key)
            try:
                current.check()
                self._ensure_executor()
                current.check()
                return self._execute(request)
            finally:
                self._release_scope(key)

    def _select_scope(self, key: tuple[str, ...] | None) -> None:
        """Evict only quiescent containers before admitting another identity."""

        if key is not None and key in self._scopes:
            self._executor = self._scopes.pop(key)
            return
        if len(self._scopes) >= self._max_scopes:
            oldest = next(iter(self._scopes))
            close_guarded_executor(self._scopes[oldest])
            del self._scopes[oldest]

    def _release_scope(self, key: tuple[str, ...] | None) -> None:
        """Destroy fresh scopes and never recycle an aborted container."""

        if self._executor is None:
            return
        if key is None or guard_failed(self._executor):
            close_guarded_executor(self._executor)
        else:
            self._scopes[key] = self._executor
        self._executor = None

    def close(self) -> None:
        """Revoke admission and clean every container before forgetting it."""

        with self._lock:
            self._closed = True
            if self._executor is not None:
                close_guarded_executor(self._executor)
                self._executor = None
            for key in tuple(self._scopes):
                close_guarded_executor(self._scopes[key])
                del self._scopes[key]

    def _ensure_executor(self) -> None:
        """Retain failed-start cleanup ownership until termination is proven."""

        if self._executor is not None:
            return
        try:
            self._executor = create_docker_executor(
                copy.deepcopy(self._config), self._max_output_bytes
            )
        except Exception as error:
            self._executor = getattr(error, "failed_executor", None)
            raise

    def _retire_failed_executor(self) -> None:
        """Clean a poisoned container before allowing provider replacement."""

        if self._executor is not None and guard_failed(self._executor):
            close_guarded_executor(self._executor)
            self._executor = None

    def _execute(self, request: SandboxRequest) -> SandboxResult:
        """Allow shorter request deadlines without extending the configured cap."""

        maximum = self._config["timeout_seconds"]
        self._executor.timeout_seconds = min(
            maximum, request.timeout_seconds or maximum
        )
        try:
            return self._executor.execute(request)
        finally:
            # Retained executors cannot leak a prior request's shorter deadline.
            self._executor.timeout_seconds = maximum


__all__ = [
    "DockerSandboxBackend",
    "create_docker_backend",
    "validate_adapter_options",
]

"""Portable per-connection lifecycle for remote MCP HTTP clients."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence, TypeAlias

MCPFramework = Literal["adk", "langgraph"]
MCPLifecycleValue: TypeAlias = Any | Awaitable[Any]

_RESOURCE_ATTRIBUTE = "__harnest_mcp_lifecycle_resources__"
_CONTROLLER_ATTRIBUTE = "__harnest_mcp_lifecycle_controller__"


@dataclass(frozen=True, slots=True)
class MCPClientContext:
    """Privacy-safe identity supplied to an MCP client lifecycle."""

    name: str
    transport: Literal["sse", "streamable-http"]
    framework: MCPFramework
    url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MCPHTTPClientOptions:
    """Adapter-provided inputs for one MCP HTTP client instance."""

    headers: Mapping[str, str] = field(repr=False)
    timeout: Any = field(default=None, repr=False)
    auth: Any = field(default=None, repr=False)


class MCPClientLifecycle:
    """Customize one remote MCP connection's HTTP client lifecycle.

    ``start`` and ``close`` run once for the compiled application. The
    synchronous ``create_http_client`` hook runs for every MCP session because
    ADK and LangGraph own and close those session-scoped HTTP clients.
    """

    def start(self, context: MCPClientContext) -> MCPLifecycleValue:
        """Initialize credential, certificate, or gateway support resources."""

        return None

    def create_http_client(
        self, options: MCPHTTPClientOptions, context: MCPClientContext
    ) -> Any:
        """Return a proxy-aware ``httpx.AsyncClient`` with safe diagnostics."""

        from mcp.shared._httpx_utils import create_mcp_http_client

        try:
            # HTTPX's default trust_env=True is intentional: deployments may
            # require HTTP(S) or SOCKS egress proxies configured by operators.
            return create_mcp_http_client(
                headers=dict(options.headers),
                timeout=options.timeout,
                auth=options.auth,
            )
        except Exception as error:
            failure = _http_client_construction_failure(error)
        # Raise outside the active handler so secret-bearing provider details
        # are not retained through the exception context chain.
        raise failure

    def close(self, context: MCPClientContext) -> MCPLifecycleValue:
        """Release application-level resources created by ``start``."""

        return None


class _MCPClientLifecycleController:
    """Start, validate, and close one authored lifecycle exactly once."""

    def __init__(self, lifecycle: MCPClientLifecycle) -> None:
        self.lifecycle = lifecycle
        self._context: MCPClientContext | None = None
        self._state: Literal["new", "starting", "started", "closed"] = "new"
        self._lock = asyncio.Lock()

    async def start(self, context: MCPClientContext) -> bool:
        """Start once and report whether this call acquired ownership."""

        async with self._lock:
            if self._state == "started":
                self._require_context(context)
                return False
            if self._state == "closed":
                raise RuntimeError("MCP client lifecycle is closed")
            self._context = context
            self._state = "starting"
            failure = await _invoke_lifecycle_hook(
                "start", lambda: self.lifecycle.start(context)
            )
            if failure is not None:
                cleanup_failure = await _invoke_lifecycle_hook(
                    "close", lambda: self.lifecycle.close(context)
                )
                self._context = None
                # Failed cleanup may leave live credential or watcher state, so
                # retrying start on this controller would be unsafe.
                self._state = "closed" if cleanup_failure is not None else "new"
                _note_sanitized_failure(
                    failure,
                    cleanup_failure,
                    "MCP lifecycle failed-start cleanup",
                )
                raise failure
            self._context = context
            self._state = "started"
            return True

    async def close(self, context: MCPClientContext, *, reset: bool = False) -> None:
        """Close a started lifecycle, optionally allowing startup retry."""

        async with self._lock:
            if self._state in {"new", "closed"}:
                return
            self._require_context(context)
            failure = await _invoke_lifecycle_hook(
                "close", lambda: self.lifecycle.close(context)
            )
            if failure is not None:
                raise failure
            self._context = None
            self._state = "new" if reset else "closed"

    def client_factory(self, context: MCPClientContext) -> Any:
        """Return the structural factory expected by both MCP adapters."""

        def create_client(headers=None, timeout=None, auth=None):
            if self._state != "started":
                raise RuntimeError("MCP client lifecycle has not started")
            self._require_context(context)
            options = MCPHTTPClientOptions(
                headers=MappingProxyType(dict(headers or {})),
                timeout=timeout,
                auth=auth,
            )
            client, failure = _invoke_http_client_hook(
                self.lifecycle, options, context
            )
            if failure is not None:
                raise failure
            if inspect.isawaitable(client):
                _close_awaitable(client)
                raise TypeError("MCP lifecycle create_http_client must be synchronous")
            _validate_http_client(client)
            return client

        return create_client

    def _require_context(self, context: MCPClientContext) -> None:
        """Prevent one stateful lifecycle from crossing connection boundaries."""

        if self._context is not None and self._context != context:
            raise RuntimeError(
                "MCP client lifecycle cannot be shared across connections or frameworks"
            )


@dataclass(frozen=True, slots=True)
class _MCPClientLifecycleBinding:
    """Bind one lifecycle controller to its resolved connection identity."""

    controller: _MCPClientLifecycleController
    context: MCPClientContext

    async def start(self) -> bool:
        """Start the bound lifecycle once."""

        return await self.controller.start(self.context)

    async def close(self, *, reset: bool = False) -> None:
        """Close the bound lifecycle once."""

        await self.controller.close(self.context, reset=reset)

    def client_factory(self) -> Any:
        """Return a framework-compatible session HTTP client factory."""

        return self.controller.client_factory(self.context)


def _mcp_lifecycle_controller(
    lifecycle: MCPClientLifecycle,
) -> _MCPClientLifecycleController:
    """Return the one controller owned by a lifecycle instance."""

    existing = getattr(lifecycle, _CONTROLLER_ATTRIBUTE, None)
    if isinstance(existing, _MCPClientLifecycleController):
        return existing
    controller = _MCPClientLifecycleController(lifecycle)
    # Lifecycle identity is the ownership boundary. Sharing one instance across
    # incompatible connections must reach the controller's context guard.
    object.__setattr__(lifecycle, _CONTROLLER_ATTRIBUTE, controller)
    return controller


def attach_mcp_lifecycle(value: Any, binding: _MCPClientLifecycleBinding) -> Any:
    """Make a framework object own an MCP lifecycle binding."""

    current = mcp_lifecycle_bindings(value)
    object.__setattr__(value, _RESOURCE_ATTRIBUTE, (*current, binding))
    return value


def propagate_mcp_lifecycles(source: Any, target: Any) -> Any:
    """Propagate MCP lifecycle ownership through framework wrapper objects."""

    values = source if isinstance(source, (tuple, list)) else (source,)
    for value in values:
        for binding in mcp_lifecycle_bindings(value):
            attach_mcp_lifecycle(target, binding)
    return target


def mcp_lifecycle_bindings(value: Any) -> tuple[_MCPClientLifecycleBinding, ...]:
    """Return lifecycle bindings directly attached to a framework object."""

    resources = getattr(value, _RESOURCE_ATTRIBUTE, ())
    return tuple(resources) if isinstance(resources, (tuple, list)) else ()


async def start_mcp_lifecycles(bindings: Sequence[_MCPClientLifecycleBinding]) -> None:
    """Start unique lifecycle bindings and unwind partial startup failures."""

    unique = _unique_bindings(bindings)
    started: list[_MCPClientLifecycleBinding] = []
    try:
        for binding in unique:
            if await binding.start():
                started.append(binding)
    except BaseException as error:
        for binding in reversed(started):
            try:
                await binding.close(reset=True)
            except BaseException as cleanup_error:
                # Preserve the startup error and expose only the cleanup type;
                # lifecycle messages may include gateway credentials.
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "MCP lifecycle startup cleanup failed with "
                        f"{type(cleanup_error).__name__}"
                    )
        raise


async def close_mcp_lifecycles(
    bindings: Sequence[_MCPClientLifecycleBinding], *, reset: bool = False
) -> None:
    """Close unique bindings in reverse ownership order."""

    failures: list[BaseException] = []
    for binding in reversed(_unique_bindings(bindings)):
        try:
            await binding.close(reset=reset)
        except BaseException as error:
            failures.append(error)
    if failures:
        # Cancellation must retain task-control semantics even when another
        # independent cleanup hook also fails during the same shutdown.
        primary = next(
            (
                error
                for error in failures
                if isinstance(error, asyncio.CancelledError)
            ),
            failures[0],
        )
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            for error in failures:
                if error is primary:
                    continue
                add_note(
                    "additional MCP lifecycle cleanup failure: "
                    f"{type(error).__name__}"
                )
        raise primary


def _unique_bindings(
    bindings: Sequence[_MCPClientLifecycleBinding],
) -> tuple[_MCPClientLifecycleBinding, ...]:
    """Deduplicate propagated wrappers by their underlying controller."""

    unique: list[_MCPClientLifecycleBinding] = []
    seen: dict[int, MCPClientContext] = {}
    for binding in bindings:
        identity = id(binding.controller)
        previous = seen.get(identity)
        if previous is not None:
            if previous != binding.context:
                raise RuntimeError(
                    "MCP client lifecycle cannot be shared across connections or frameworks"
                )
            continue
        seen[identity] = binding.context
        unique.append(binding)
    return tuple(unique)


async def _await_lifecycle(value: MCPLifecycleValue) -> Any:
    """Await lifecycle setup and cleanup only when authored asynchronously."""

    return await value if inspect.isawaitable(value) else value


async def _invoke_lifecycle_hook(hook: str, operation: Any) -> BaseException | None:
    """Return a sanitized hook failure without retaining its exception context."""

    try:
        await _await_lifecycle(operation())
    except BaseException as error:
        failure = _sanitized_hook_failure(hook, error)
    else:
        return None
    # Raising this value later, outside the active handler, prevents the
    # original secret-bearing exception from becoming ``__context__``.
    return failure


def _invoke_http_client_hook(
    lifecycle: MCPClientLifecycle,
    options: MCPHTTPClientOptions,
    context: MCPClientContext,
) -> tuple[Any, BaseException | None]:
    """Call the synchronous client hook without retaining unsafe failures."""

    try:
        client = lifecycle.create_http_client(options, context)
    except BaseException as error:
        failure = _sanitized_hook_failure("create_http_client", error)
    else:
        return client, None
    return None, failure


def _validate_http_client(client: Any) -> None:
    """Fail before transport I/O when the lifecycle returns the wrong object."""

    try:
        from httpx import AsyncClient
    except ImportError as error:  # pragma: no cover - MCP installs httpx
        raise RuntimeError("MCP HTTP lifecycle requires httpx") from error
    if not isinstance(client, AsyncClient):
        raise TypeError(
            "MCP lifecycle create_http_client must return httpx.AsyncClient"
        )


def _close_awaitable(value: Any) -> None:
    """Close rejected coroutine objects without executing user code."""

    closer = getattr(value, "close", None)
    if callable(closer):
        closer()


def _sanitized_hook_failure(hook: str, error: BaseException) -> BaseException:
    """Redact hook failures while preserving task cancellation semantics."""

    if isinstance(error, asyncio.CancelledError):
        return asyncio.CancelledError()
    return RuntimeError(
        f"MCP lifecycle {hook} failed with {type(error).__name__}"
    )


def _http_client_construction_failure(error: Exception) -> RuntimeError:
    """Describe client construction without exposing proxy URLs or credentials."""

    return RuntimeError(
        f"MCP HTTP client construction failed with {type(error).__name__}"
    )


def _note_sanitized_failure(
    primary: BaseException,
    secondary: BaseException | None,
    label: str,
) -> None:
    """Attach only the safe exception type for an additional failure."""

    if secondary is None:
        return
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(f"{label} failed with {type(secondary).__name__}")


__all__ = ["MCPClientContext", "MCPClientLifecycle", "MCPHTTPClientOptions"]

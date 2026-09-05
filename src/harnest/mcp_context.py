"""Invocation-scoped MCP access that can dispatch only governed tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import inspect
import re
from types import MappingProxyType
from typing import Any

from .approval import ApprovalError, ApprovalPolicy, ApprovalRequired
from .lifecycle import LifecycleListener
from .lifecycle_transition import Finish, Next, TransitionContext, UNCHANGED
from .logging import get_logger
from .tool_lifecycle import wrap_lifecycle_tool


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,127}$")
_FRAMEWORKS = frozenset({"adk", "langgraph"})
_PHASES = frozenset({"before_mcp", "after_mcp", "on_mcp_error"})
_AUDIT = get_logger("mcp.audit")
_MCPCall = Callable[[Mapping[str, Any]], Awaitable[Any]]
_GOVERNED_OPERATION = "__harnest_mcp_governed__"


class MCPContextUnavailableError(RuntimeError):
    """Raised when managed MCP access has no active invocation binding."""


class MCPClientUnavailableError(LookupError):
    """Raised when an invocation cannot access a named managed MCP client."""


class MCPToolUnavailableError(LookupError):
    """Raised when a managed MCP client does not expose a requested tool."""


class MCPToolCallError(RuntimeError):
    """A provider-neutral failure from a discovered managed MCP tool."""


class MCPLifecycleError(TypeError):
    """An MCP interceptor returned invalid or ambiguous control flow."""


@dataclass(frozen=True, slots=True, repr=False)
class MCPToolCallRequest:
    """Opaque remote arguments that a before hook may explicitly replace."""

    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Freeze only the container while leaving tool-owned values opaque."""

        object.__setattr__(self, "arguments", _immutable_arguments(self.arguments))


@dataclass(frozen=True, slots=True, repr=False)
class MCPToolLifecycleContext(TransitionContext):
    """Privacy-safe identity for one governed remote-tool execution."""

    framework: str
    agent_name: str
    invocation_id: str = field(repr=False)
    user_id: str = field(repr=False)
    session_id: str = field(repr=False)
    client_name: str
    tool_name: str

    def __post_init__(self) -> None:
        """Validate only stable identity and never accept transport metadata."""

        if self.framework not in _FRAMEWORKS:
            raise ValueError(f"unsupported MCP lifecycle framework {self.framework!r}")
        for field_name in (
            "agent_name",
            "invocation_id",
            "user_id",
            "session_id",
        ):
            _require_identity(getattr(self, field_name), field_name)
        _require_name(self.client_name, "MCP client name")
        _require_name(self.tool_name, "MCP tool name")


@dataclass(frozen=True, slots=True)
class _MCPExecution:
    """Carry audit-relevant flow without exposing it as an author result."""

    result: Any = field(repr=False)
    outcome: str


class MCPLifecyclePipeline:
    """Apply explicit portable interceptors around one managed MCP call."""

    def __init__(self, listeners: Sequence[LifecycleListener]) -> None:
        selected = (item for item in listeners if item.phase in _PHASES)
        self._listeners = tuple(sorted(selected, key=_listener_order))

    async def run(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[Any]],
        context: MCPToolLifecycleContext,
    ) -> _MCPExecution:
        """Run one call, allowing explicit short-circuit and safe recovery."""

        try:
            current, finished = await self._before(context, request)
            if finished is not None:
                return _MCPExecution(finished.result, "finished")
            result = await handler(current)
            outcome = "failed" if _mcp_result_failed(result) else "committed"
            return _MCPExecution(await self._after(context, result), outcome)
        except asyncio.CancelledError as error:
            await self._notify(context, error, recoverable=False)
            raise
        except BaseException as error:
            recovered = await self._notify(
                context, _observer_error(error), recoverable=_recoverable(error)
            )
            if recovered is not None:
                return _MCPExecution(recovered.result, "recovered")
            raise

    async def _before(
        self, context: MCPToolLifecycleContext, request: MCPToolCallRequest
    ) -> tuple[MCPToolCallRequest, Finish[Any] | None]:
        """Advance through before hooks until one explicitly finishes."""

        current = request
        for listener in self._phase("before_mcp"):
            transition = await _resolve(listener.callback(context, current))
            current, finished = _before_transition(listener, current, transition)
            if finished is not None:
                return current, finished
        return current, None

    async def _after(self, context: MCPToolLifecycleContext, result: Any) -> Any:
        """Apply explicit result replacement in deterministic hook order."""

        current = result
        for listener in self._phase("after_mcp"):
            transition = await _resolve(listener.callback(context, current))
            current, finished = _after_transition(listener, current, transition)
            if finished:
                break
        return current

    async def _notify(
        self,
        context: MCPToolLifecycleContext,
        error: BaseException,
        *,
        recoverable: bool,
    ) -> Finish[Any] | None:
        """Notify error hooks while forbidding approval/cancellation recovery."""

        for listener in self._phase("on_mcp_error"):
            try:
                transition = await _resolve(listener.callback(context, error))
            except BaseException:
                # Error observation must not replace the governed call failure.
                continue
            recovered = _error_transition(listener, transition, recoverable)
            if recovered is not None:
                return recovered
        return None

    def _phase(self, phase: str) -> tuple[LifecycleListener, ...]:
        """Select one phase from the already ordered immutable listener set."""

        return tuple(item for item in self._listeners if item.phase == phase)


@dataclass(frozen=True, slots=True, repr=False)
class _GovernedMCPTool:
    """The only runtime adapter value accepted by an invocation binding."""

    client_name: str
    name: str
    _operation: _MCPCall = field(repr=False, compare=False)
    required_permissions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _require_name(self.client_name, "MCP client name")
        _require_name(self.name, "MCP tool name")
        if not callable(self._operation):
            raise TypeError("managed MCP tool operation must be callable")
        from .agent_principal import validate_permission

        for permission in self.required_permissions:
            validate_permission(permission)

    async def invoke(self, arguments: Mapping[str, Any]) -> Any:
        """Run the single path shared by framework and context-originated calls."""

        from .agent_principal import (
            AgentRuntimePermissionError,
            permissions_are_available,
        )

        if not permissions_are_available(self.required_permissions):
            raise AgentRuntimePermissionError(
                f"capability {self.name!r} is unavailable to the "
                "Agent Runtime Principal"
            )

        binding = _active_binding()
        public_name = binding.public_names.get(id(self))
        if public_name is None:
            raise MCPContextUnavailableError(
                "managed MCP tool is not registered for this invocation"
            )
        request = MCPToolCallRequest(arguments)
        lifecycle_context = _call_context(public_name, self.name)
        dispatch_token = _NATIVE_DISPATCHED.set(False)
        try:
            execution = await binding.pipeline.run(
                request,
                lambda current: self._operation(current.arguments),
                lifecycle_context,
            )
        except ApprovalRequired:
            _audit(self.client_name, self.name, "suspended")
            raise
        except asyncio.CancelledError:
            _audit(self.client_name, self.name, "cancelled")
            raise
        except BaseException:
            _audit(self.client_name, self.name, "failed")
            raise
        else:
            outcome = execution.outcome
            if outcome == "committed" and not _NATIVE_DISPATCHED.get():
                outcome = "finished"
            _audit(self.client_name, self.name, outcome)
            return execution.result
        finally:
            _NATIVE_DISPATCHED.reset(dispatch_token)


@dataclass(slots=True)
class _MCPContextLifetime:
    """Revoke bindings inherited by child tasks after an invocation ends."""

    active: bool = True


@dataclass(frozen=True, slots=True, repr=False)
class ManagedMCPClient:
    """Named MCP facade with no access to credentials or raw transports."""

    name: str
    _tools: Mapping[str, _GovernedMCPTool] = field(repr=False)
    _invocation_id: str = field(repr=False)
    _lifetime: _MCPContextLifetime = field(repr=False, compare=False)

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> Any:
        """Dispatch one discovered tool through its governed shared path."""

        self._require_active()
        _require_name(name, "MCP tool name")
        tool = self._tools.get(name)
        if tool is None:
            raise MCPToolUnavailableError("managed MCP tool is not available")
        return await tool.invoke(_immutable_arguments(arguments))

    def _require_active(self) -> None:
        """Require both the binding lifetime and its exact invocation identity."""

        from .context import context

        try:
            active = context.current()
        except RuntimeError:
            raise MCPContextUnavailableError(
                "managed MCP access is available only during an invocation"
            ) from None
        if not self._lifetime.active or active.invocation_id != self._invocation_id:
            raise MCPContextUnavailableError("managed MCP context is no longer active")


@dataclass(frozen=True, slots=True, repr=False)
class _MCPBinding:
    """Private client and lifecycle registry bound to exactly one invocation."""

    clients: Mapping[str, ManagedMCPClient] = field(repr=False)
    public_names: Mapping[int, str] = field(repr=False)
    pipeline: MCPLifecyclePipeline = field(repr=False)
    lifetime: _MCPContextLifetime = field(repr=False)


_ACTIVE_MCP: ContextVar[_MCPBinding | None] = ContextVar(
    "harnest_mcp_context", default=None
)
_NATIVE_DISPATCHED: ContextVar[bool] = ContextVar(
    "harnest_mcp_native_dispatched", default=False
)


class MCPContext:
    """Resolve managed MCP clients without exposing the client registry."""

    def __call__(self, name: str) -> ManagedMCPClient:
        """Return one named managed client for concise context access."""

        return self.client(name)

    def client(self, name: str) -> ManagedMCPClient:
        """Return one named client or fail without enumerating capabilities."""

        _require_name(name, "MCP client name")
        binding = _active_binding()
        client = binding.clients.get(name)
        if client is None:
            raise MCPClientUnavailableError("managed MCP client is not available")
        client._require_active()
        return client


mcp = MCPContext()


def _managed_mcp_tool(
    client_name: str,
    name: str,
    operation: _MCPCall,
    *,
    approval: ApprovalPolicy | None = None,
    required_permissions: Sequence[str] = (),
) -> _GovernedMCPTool:
    """Create the one governed wrapper for an actually discovered framework tool."""

    _require_name(client_name, "MCP client name")
    _require_name(name, "MCP tool name")
    if not callable(operation):
        raise TypeError("managed MCP tool operation must be callable")

    async def approved(arguments: Mapping[str, Any]) -> Any:
        from .mcp import _invoke_governed_mcp_call

        return await _invoke_governed_mcp_call(
            lambda: _safe_framework_call(operation, arguments),
            client_name=client_name,
            tool_name=name,
            arguments=arguments,
            policy=approval,
        )

    # ToolLifecycle derives the public tool identity from __name__. Assigning the
    # discovered stable name prevents an internal helper name entering policy.
    approved.__name__ = name
    governed = wrap_lifecycle_tool(approved)
    if required_permissions:
        from .agent_principal import attach_required_permissions

        # The lifecycle wrapper performs its own execution-time principal check,
        # so it must retain the same requirements as the outer MCP marker.
        attach_required_permissions(governed, tuple(required_permissions))
    return _GovernedMCPTool(
        client_name,
        name,
        governed,
        frozenset(required_permissions),
    )


def _mark_governed_mcp_operation(operation: Any) -> Any:
    """Mark a framework adapter closure that delegates only to marker.invoke."""

    if not callable(operation):
        raise TypeError("governed MCP framework operation must be callable")
    setattr(operation, _GOVERNED_OPERATION, True)
    return operation


def _is_governed_mcp_operation(operation: Any) -> bool:
    """Let framework-wide tool adapters avoid a second lifecycle boundary."""

    return getattr(operation, _GOVERNED_OPERATION, False) is True


@contextmanager
def _activate_mcp_context(
    clients: Mapping[str, Mapping[str, _GovernedMCPTool]],
    listeners: Sequence[LifecycleListener] = (),
) -> Iterator[None]:
    """Bind governed clients and revoke framework and child-task references."""

    from .context import context

    from .agent_principal import permissions_are_available

    active = context.current()
    lifetime = _MCPContextLifetime()
    projected = {
        name: {
            tool_name: tool
            for tool_name, tool in tools.items()
            if permissions_are_available(
                getattr(tool, "required_permissions", ())
            )
        }
        for name, tools in clients.items()
    }
    projected = {name: tools for name, tools in projected.items() if tools}
    managed = {
        name: _managed_client(name, tools, active.invocation_id, lifetime)
        for name, tools in projected.items()
    }
    public_names = _tool_public_names(projected)
    binding = _MCPBinding(
        MappingProxyType(managed),
        MappingProxyType(public_names),
        MCPLifecyclePipeline(listeners),
        lifetime,
    )
    token = _ACTIVE_MCP.set(binding)
    try:
        yield
    finally:
        # Resetting the current task is insufficient because child tasks copy
        # ContextVars; the shared lifetime closes that authority as well.
        lifetime.active = False
        _ACTIVE_MCP.reset(token)


def _managed_client(
    name: str,
    tools: Mapping[str, _GovernedMCPTool],
    invocation_id: str,
    lifetime: _MCPContextLifetime,
) -> ManagedMCPClient:
    """Validate one runtime-owned tool index before it becomes accessible."""

    _require_name(name, "MCP client name")
    if not isinstance(tools, Mapping):
        raise TypeError("managed MCP client tools must be a mapping")
    copied: dict[str, _GovernedMCPTool] = {}
    for tool_name, tool in tools.items():
        _require_name(tool_name, "MCP tool name")
        if not isinstance(tool, _GovernedMCPTool) or tool.name != tool_name:
            # Accepting arbitrary or cross-client callables would route around
            # the approval identity recorded for the discovered capability.
            raise TypeError("managed MCP tools must use Harnest governed adapters")
        copied[tool_name] = tool
    if len({tool.client_name for tool in copied.values()}) > 1:
        raise TypeError("one public MCP client cannot combine capabilities")
    return ManagedMCPClient(name, MappingProxyType(copied), invocation_id, lifetime)


def _tool_public_names(
    clients: Mapping[str, Mapping[str, _GovernedMCPTool]],
) -> dict[int, str]:
    """Bind each marker to exactly one collision-checked public client name."""

    public_names: dict[int, str] = {}
    for public_name, tools in clients.items():
        for marker in tools.values():
            previous = public_names.setdefault(id(marker), public_name)
            if previous != public_name:
                # A second alias makes policy behavior depend on lookup spelling.
                raise ValueError("managed MCP tools cannot have public aliases")
    return public_names


def _active_binding() -> _MCPBinding:
    """Return the live MCP binding or fail closed outside its invocation."""

    binding = _ACTIVE_MCP.get()
    if binding is None or not binding.lifetime.active:
        raise MCPContextUnavailableError(
            "managed MCP access is available only during an invocation"
        )
    return binding


async def _safe_framework_call(
    operation: _MCPCall, arguments: Mapping[str, Any]
) -> Any:
    """Detach provider errors before any authored lifecycle observer sees them."""

    failure: MCPToolCallError | None = None
    _NATIVE_DISPATCHED.set(True)
    try:
        return await operation(arguments)
    except asyncio.CancelledError:
        raise
    except ApprovalError:
        # An adapter may already own approval. These errors are control flow and
        # must reach the suspension runtime unchanged rather than being hidden.
        raise
    except MCPToolCallError:
        # Framework adapters create this error only after detaching provider
        # result objects, so preserving it does not reintroduce private state.
        raise
    except Exception as error:
        failure = MCPToolCallError(
            f"managed MCP tool call failed with {type(error).__name__}"
        )
    # Raising after the provider exception scope prevents __context__ from
    # retaining transports, URLs, response bodies, or credential-bearing state.
    raise failure


def _call_context(client_name: str, tool_name: str) -> MCPToolLifecycleContext:
    """Derive private call identity only from the active Harnest invocation."""

    from .context import context

    active = context.current()
    return MCPToolLifecycleContext(
        framework=active.framework,
        agent_name=active.agent_name,
        invocation_id=active.invocation_id,
        user_id=active.user_id,
        session_id=active.session_id,
        client_name=client_name,
        tool_name=tool_name,
    )


def _before_transition(
    listener: LifecycleListener, current: MCPToolCallRequest, value: Any
) -> tuple[MCPToolCallRequest, Finish[Any] | None]:
    """Require explicit continuation without permitting identity replacement."""

    if isinstance(value, Finish):
        return current, value
    if not isinstance(value, Next):
        raise _transition_error(listener, "context.next(...) or context.finish(...)")
    if value.value is UNCHANGED:
        return current, None
    if not isinstance(value.value, MCPToolCallRequest):
        raise _transition_error(listener, "context.next(MCPToolCallRequest)")
    return value.value, None


def _after_transition(
    listener: LifecycleListener, current: Any, value: Any
) -> tuple[Any, bool]:
    """Apply an explicit result replacement or stop remaining after hooks."""

    if isinstance(value, Finish):
        return value.result, True
    if not isinstance(value, Next):
        raise _transition_error(listener, "context.next(...) or context.finish(...)")
    return (current if value.value is UNCHANGED else value.value), False


def _error_transition(
    listener: LifecycleListener, value: Any, recoverable: bool
) -> Finish[Any] | None:
    """Allow explicit fallback without swallowing approval or cancellation."""

    if isinstance(value, Next) and value.value is UNCHANGED:
        return None
    if isinstance(value, Finish) and recoverable:
        return value
    if isinstance(value, Finish):
        # Approval and cancellation are runtime control flow. A fallback is
        # intentionally ignored so an extension cannot acknowledge then bypass it.
        return None
    expected = "context.next()"
    if recoverable:
        expected += " or context.finish(result)"
    raise _transition_error(listener, expected)


def _transition_error(listener: LifecycleListener, expected: str) -> MCPLifecycleError:
    """Create a source-addressed error without reflecting callback values."""

    return MCPLifecycleError(f"MCP listener {listener.identity} must return {expected}")


def _observer_error(error: BaseException) -> BaseException:
    """Hide approval payloads from error hooks while preserving outer control flow."""

    if isinstance(error, ApprovalError):
        return MCPToolCallError("managed MCP approval did not complete")
    return error


def _recoverable(error: BaseException) -> bool:
    """Prevent error hooks from bypassing approval and process cancellation."""

    return not isinstance(error, (ApprovalError, asyncio.CancelledError))


def _mcp_result_failed(result: Any) -> bool:
    """Use the shared adapter-level error classification without reading payloads."""

    from .mcp import _mcp_result_failed as classify

    return classify(result)


def _listener_order(listener: LifecycleListener) -> tuple[Any, ...]:
    """Match deterministic lifecycle ordering used by other portable stages."""

    return (
        listener.order,
        listener.relative_path,
        listener.line,
        listener.function_name,
    )


async def _resolve(value: Any) -> Any:
    """Accept synchronous or asynchronous lifecycle callbacks uniformly."""

    return await value if inspect.isawaitable(value) else value


def _immutable_arguments(
    arguments: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Protect dispatcher policy from top-level argument mutation by callbacks."""

    if arguments is None:
        return MappingProxyType({})
    if not isinstance(arguments, Mapping):
        raise TypeError("MCP tool arguments must be a mapping")
    return MappingProxyType(dict(arguments))


def _require_name(value: Any, label: str) -> None:
    """Validate stable declared identifiers without reflecting unsafe input."""

    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{label} must be a valid identifier")


def _require_identity(value: Any, label: str) -> None:
    """Validate private invocation identity without including it in failures."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _audit(client: str, tool: str, outcome: str) -> None:
    """Emit one payload-free signal for every governed remote execution."""

    # Arguments and results may contain credentials or customer content; only
    # compiler-owned capability and discovered tool names are safe dimensions.
    _AUDIT.info(
        "mcp.call",
        operation="call",
        trigger="agent",
        outcome=outcome,
        client=client,
        tool=tool,
    )


__all__ = [
    "MCPClientUnavailableError",
    "MCPContext",
    "MCPContextUnavailableError",
    "MCPLifecycleError",
    "MCPLifecyclePipeline",
    "MCPToolCallError",
    "MCPToolCallRequest",
    "MCPToolLifecycleContext",
    "MCPToolUnavailableError",
    "ManagedMCPClient",
    "mcp",
]

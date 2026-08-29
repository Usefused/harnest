"""Bind managed subagent identity across native Google ADK execution."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from google.adk.plugins.base_plugin import BasePlugin

from .context import (
    AgentContext,
    _ACTIVE_CONTEXT,
    context,
    create_agent_context,
    derive_agent_context,
    revoke_context,
)
from .plugin_runtime_context import (
    PluginInvocationBinding,
    bind_plugin_bindings,
    reset_plugin_bindings,
)

if TYPE_CHECKING:
    from .plugin_runtime_manager import PluginRuntimeManager


@dataclass(slots=True)
class _AgentScope:
    """Balance one callback even when no derived activation was needed."""

    invocation_id: str
    token: Token[AgentContext | None] | None
    owner_task: asyncio.Task[Any]


@dataclass(slots=True)
class _RunScope:
    """Track whether direct native execution needed a fallback root context."""

    token: Token[AgentContext | None] | None
    owned: AgentContext | None
    plugin_tokens: tuple[tuple[Any, Any], ...]
    owner_task: asyncio.Task[Any]


class _ADKAgentContextCoordinator:
    """Pair ADK's split callbacks around one task-local derived context."""

    def __init__(
        self,
        root_name: str | None,
        plugin_manager: "PluginRuntimeManager | None",
    ) -> None:
        # ADK parallel branches run in copied async Contexts. A ContextVar stack
        # keeps their callback pairing isolated without mutable global keys.
        self._scopes: ContextVar[tuple[_AgentScope, ...]] = ContextVar(
            "harnest_adk_agent_context_scopes", default=()
        )
        self._runs: ContextVar[tuple[_RunScope, ...]] = ContextVar(
            "harnest_adk_agent_run_scopes", default=()
        )
        self._root_name = root_name
        self._plugin_manager = plugin_manager

    def enter_run(self, invocation_context: Any) -> None:
        """Reuse host authority or establish a minimal direct-driver fallback."""

        try:
            context.current()
        except RuntimeError:
            bindings = (
                {}
                if self._plugin_manager is None
                else self._plugin_manager.invocation_bindings()
            )
            scope = _open_root_scope(
                _root_context(invocation_context, self._root_name, bindings)
            )
        else:
            scope = _RunScope(None, None, (), _current_task())
        self._runs.set((*self._runs.get(), scope))

    def exit_run(self) -> None:
        """Release only fallback authority created by this native run."""

        scopes = self._runs.get()
        if not scopes:
            return
        scope = scopes[-1]
        _require_scope_owner(scope.owner_task)
        self._runs.set(scopes[:-1])
        _close_root_scope(scope)

    def enter(self, agent: Any, callback_context: Any) -> None:
        """Derive identity only when ADK is entering a nested managed agent."""

        active = context.current()
        agent_name = _required_text(getattr(agent, "name", None), "agent name")
        token = None
        if active.agent_name != agent_name:
            derived = derive_agent_context(active, agent_name=agent_name)
            token = _ACTIVE_CONTEXT.set(derived)
        invocation_id = _required_text(
            getattr(callback_context, "invocation_id", None), "invocation id"
        )
        scope = _AgentScope(invocation_id, token, _current_task())
        self._scopes.set((*self._scopes.get(), scope))

    def exit(self, callback_context: Any) -> None:
        """Restore the parent context after authored after/error callbacks run."""

        scopes = self._scopes.get()
        if not scopes:
            return
        invocation_id = _required_text(
            getattr(callback_context, "invocation_id", None), "invocation id"
        )
        scope = scopes[-1]
        if scope.invocation_id != invocation_id:
            raise RuntimeError("ADK agent context callback order is invalid")
        _require_scope_owner(scope.owner_task)
        self._scopes.set(scopes[:-1])
        if scope.token is not None:
            _ACTIVE_CONTEXT.reset(scope.token)


class _ADKAgentContextEnterPlugin(BasePlugin):
    """Run first so every authored native callback observes child identity."""

    def __init__(self, coordinator: _ADKAgentContextCoordinator) -> None:
        super().__init__(name="_harnest_agent_context_enter")
        self._coordinator = coordinator

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        """Ensure direct ADKRuntimeDriver calls still receive managed identity."""

        self._coordinator.enter_run(invocation_context)

    async def before_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> None:
        """Open the child view before ADK or authored callbacks execute."""

        self._coordinator.enter(agent, callback_context)


class _ADKAgentContextExitPlugin(BasePlugin):
    """Run last so authored after/error callbacks retain child identity."""

    def __init__(self, coordinator: _ADKAgentContextCoordinator) -> None:
        super().__init__(name="_harnest_agent_context_exit")
        self._coordinator = coordinator

    async def after_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> None:
        """Restore the parent after successful child execution."""

        del agent
        self._coordinator.exit(callback_context)

    async def on_agent_error_callback(
        self, *, agent: Any, callback_context: Any, error: Exception
    ) -> None:
        """Restore the parent while preserving ADK's original failure."""

        del agent, error
        self._coordinator.exit(callback_context)

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        """Release a successful run's direct-driver fallback context."""

        del invocation_context
        self._coordinator.exit_run()

    async def on_run_error_callback(
        self, *, invocation_context: Any, error: Exception
    ) -> None:
        """Release fallback identity without replacing the native failure."""

        del invocation_context, error
        self._coordinator.exit_run()


def adk_agent_context_plugins(
    root_name: str | None = None,
    plugin_manager: "PluginRuntimeManager | None" = None,
) -> tuple[BasePlugin, BasePlugin]:
    """Create ordered entry and exit adapters sharing one scope coordinator."""

    coordinator = _ADKAgentContextCoordinator(root_name, plugin_manager)
    return (
        _ADKAgentContextEnterPlugin(coordinator),
        _ADKAgentContextExitPlugin(coordinator),
    )


def _required_text(value: Any, label: str) -> str:
    """Require framework-owned stable identifiers without string coercion."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"ADK {label} must be a non-empty string")
    return value


def _root_context(
    invocation_context: Any,
    fallback_name: str | None,
    plugin_bindings: Mapping[str, PluginInvocationBinding],
) -> AgentContext:
    """Translate only stable native identity for direct driver execution."""

    session = getattr(invocation_context, "session", None)
    agent = getattr(invocation_context, "agent", None)
    root = getattr(agent, "root_agent", None) or agent
    native_name = getattr(root, "name", None)
    agent_name = native_name if isinstance(native_name, str) else fallback_name
    return create_agent_context(
        framework="adk",
        agent_name=_required_text(agent_name, "agent name"),
        invocation_id=_required_text(
            getattr(invocation_context, "invocation_id", None), "invocation id"
        ),
        user_id=_required_text(getattr(session, "user_id", None), "user id"),
        session_id=_required_text(getattr(session, "id", None), "session id"),
        metadata={},
        resources={},
        plugin_bindings=plugin_bindings,
    )


def _open_root_scope(active: AgentContext) -> _RunScope:
    """Bind direct-driver authority using the same task-local plugin contract."""

    token = _ACTIVE_CONTEXT.set(active)
    try:
        plugin_tokens = bind_plugin_bindings(active._plugin_bindings)
    except BaseException:
        # The native adapter owns this newly-created lifetime, so an incomplete
        # plugin bind must not leave a retained usable view behind.
        try:
            revoke_context(active)
        finally:
            _ACTIVE_CONTEXT.reset(token)
        raise
    return _RunScope(token, active, plugin_tokens, _current_task())


def _close_root_scope(scope: _RunScope) -> None:
    """Revoke copied views before resetting tokens in their owning task."""

    try:
        if scope.owned is not None:
            revoke_context(scope.owned)
    finally:
        try:
            reset_plugin_bindings(scope.plugin_tokens)
        finally:
            if scope.token is not None:
                _ACTIVE_CONTEXT.reset(scope.token)


def _current_task() -> asyncio.Task[Any]:
    """Require async callback ownership because ContextVar tokens are task-local."""

    task = asyncio.current_task()
    if task is None:  # pragma: no cover - ADK callbacks are asynchronous
        raise RuntimeError("ADK context callbacks require an active async task")
    return task


def _require_scope_owner(owner: asyncio.Task[Any]) -> None:
    """Reject copied-task cleanup before it can reset another task's token."""

    if _current_task() is not owner:
        raise RuntimeError("ADK context callback exited from a different task")


__all__ = ["adk_agent_context_plugins"]

"""Application-owned sandbox runtimes exposed only to explicitly authored tools."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Mapping

from .sandbox import Sandbox
from .sandbox_assignments import assigned_sandboxes
from .sandbox_control import execution_control
from .sandbox_runtime import SandboxProviderContractError, SandboxRuntime
from .sandbox_types import SandboxBackend, SandboxContext, SandboxFile, SandboxRequest, SandboxResult


_DENIED: ContextVar[bool] = ContextVar("harnest_native_sandbox_authority_denied", default=False)


@contextmanager
def deny_sandbox_authority():
    """Keep unmanaged native components from inheriting managed parent grants."""
    token = _DENIED.set(True)
    try:
        yield
    finally:
        _DENIED.reset(token)


class SandboxRegistry:
    """Retain one lazy provider per compiled agent grant, without model tools."""

    def __init__(self, grants: Mapping[str, Mapping[str, Sandbox]] | None = None, *, framework: str = "adk") -> None:
        """Snapshot definitions and allocate runtimes without invoking factories."""
        self._agents = MappingProxyType({
            agent: MappingProxyType({name: SandboxRuntime(value, framework) for name, value in bindings.items()})
            for agent, bindings in (grants or {}).items()
        })

    @classmethod
    def from_definition(cls, root: Any, framework: str) -> "SandboxRegistry":
        """Collect resolved grants across graphs and children, rejecting ambiguity."""
        from .agent import AgentDefinition, _AdvancedAgentDefinition
        from .graph import Graph

        grants = {}
        seen = set()
        pending = [root]
        while pending:
            value = pending.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, _AdvancedAgentDefinition):
                value = value.target
            if isinstance(value, AgentDefinition):
                _record_grants(grants, value.name, dict(assigned_sandboxes(value)))
                pending.extend(value.subagents)
            elif isinstance(value, Graph):
                pending.extend(value.nodes.values())
            elif callable(getattr(value, "run_async", None)):
                _record_grants(grants, value.name, {})
                pending.extend(getattr(value, "sub_agents", ()))
        return cls(grants, framework=framework)

    async def aclose(self) -> None:
        """Close constructed providers without constructing any unused grant."""
        failures = []
        for bindings in self._agents.values():
            for runtime in bindings.values():
                try:
                    await runtime.aclose()
                except Exception as error:
                    failures.append(error)
        if failures:
            raise RuntimeError("sandbox provider cleanup could not be confirmed") from None

    def access(self, active: Any) -> "ScopedSandboxes":
        """Capture a revocable view, not an enumerable application-wide registry."""
        active._require_active()
        return ScopedSandboxes(self, active)

    def _runtime(self, agent_name: str, name: str) -> SandboxRuntime:
        """Resolve only grants belonging to the currently executing agent."""
        from .context import ContextResourceError

        runtime = self._agents.get(agent_name, {}).get(name)
        if runtime is None:
            raise ContextResourceError(
                f"sandbox {name!r} is not assigned to agent {agent_name!r}; "
                "add its declared name to that agent's sandboxes=[...] list"
            )
        return runtime


class ScopedSandboxes:
    """Look up one explicitly granted sandbox in the captured managed scope."""

    def __init__(self, registry: SandboxRegistry, active: Any) -> None:
        """Retain the scope token without exposing provider objects or settings."""
        self._registry = registry
        self._active = active

    def __getitem__(self, name: str) -> "SandboxHandle":
        """Reject unassigned names before creating or executing any provider."""
        _require_scope(self._registry, self._active)
        if not isinstance(name, str):
            raise TypeError("sandbox lookup requires its declared name as text")
        self._registry._runtime(self._active.agent_name, name)
        return SandboxHandle(self._registry, self._active, name)


class SandboxHandle:
    """A revocable agent-and-invocation-bound execution capability."""

    def __init__(self, registry: SandboxRegistry, active: Any, name: str) -> None:
        """Keep only the grant identity; every execution rechecks live authority."""
        self._registry = registry
        self._active = active
        self._name = name

    def execute(self, code: str, *, input_files: tuple[SandboxFile, ...] = ()) -> SandboxResult:
        """Execute tool-authored code with immutable provider policy and identity."""
        active = _require_scope(self._registry, self._active)
        runtime = self._registry._runtime(active.agent_name, self._name)
        definition = runtime.definition
        with execution_control(definition.timeout_seconds):
            request = SandboxRequest(
                code=code, input_files=input_files, metadata=definition.metadata,
                timeout_seconds=definition.timeout_seconds,
                context=SandboxContext(
                    agent_name=active.agent_name, invocation_id=active.invocation_id,
                    user_id=active.user_id, session_id=active.session_id,
                ),
            )
            return runtime.run(lambda backend: _execute(backend, request))

    async def aexecute(self, code: str, *, input_files: tuple[SandboxFile, ...] = ()) -> SandboxResult:
        """Keep blocking providers off the event loop and revoke cancelled work."""
        active = _require_scope(self._registry, self._active)
        runtime = self._registry._runtime(active.agent_name, self._name)
        with execution_control(runtime.definition.timeout_seconds) as control:
            try:
                return await asyncio.to_thread(self.execute, code, input_files=input_files)
            except asyncio.CancelledError:
                control.cancelled.set()
                raise


def _require_scope(registry: SandboxRegistry, owner: Any) -> Any:
    """Prevent retained handles crossing applications, agents, or invocations."""
    from .context import ContextUnavailableError, context

    owner._require_active()
    active = context.current()
    if (
        _DENIED.get()
        or active._sandbox_registry is not registry
        or active._lifetime is not owner._lifetime
        or active.agent_name != owner.agent_name
        or active.invocation_id != owner.invocation_id
    ):
        raise ContextUnavailableError(
            "sandbox handles may be used only by the agent and managed invocation that obtained them; "
            "look up context.sandboxes[name] inside the current authored tool"
        )
    return active


def _execute(backend: Any, request: SandboxRequest) -> SandboxResult:
    """Require portable providers and typed results at the capability boundary."""
    if not isinstance(backend, SandboxBackend):
        raise SandboxProviderContractError()
    result = backend.execute(request)
    if not isinstance(result, SandboxResult):
        raise SandboxProviderContractError()
    return result


def _record_grants(grants: dict[str, Any], name: str, bindings: Mapping[str, Sandbox]) -> None:
    """Permit repeated graph references, but reject identity-dependent authority."""
    if name in grants and grants[name] != bindings:
        raise ValueError(f"duplicate agent identity {name!r} has conflicting sandbox grants")
    grants[name] = bindings

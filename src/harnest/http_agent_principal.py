"""Agent Runtime Principal policy for Harnest's built-in invocation routes.

Custom routes pass ``agent_principal=`` to ``AgentInvoker`` themselves. The
built-in routes (``/responses``, live, AG-UI and A2A) have no application code
to do that, so they get their principal from one optional
``@lifecycle.http.agent_principal`` hook instead. Without that hook they run in
compatibility mode, unless ``server.agentPrincipal: required`` is set, in which
case they refuse to invoke the agent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import inspect
from typing import Any

from .agent_principal import AgentRuntimePrincipal, validate_agent_principal
from .lifecycle import LifecycleListener
from .runtime_auth import _connection_context, principal_for


PHASE = "http_agent_principal"
_DECORATOR = "@lifecycle.http.agent_principal"


class AgentPrincipalDenied(PermissionError):
    """A built-in route could not obtain an Agent Runtime Principal."""

    status_code = 403


@dataclass(frozen=True, slots=True)
class BuiltinPrincipalPolicy:
    """Decide which Agent Runtime Principal a built-in route invokes with."""

    grant: LifecycleListener | None = None
    required: bool = False

    def __post_init__(self) -> None:
        """Reject truthy non-booleans at this security boundary."""

        if not isinstance(self.required, bool):
            raise TypeError("agent_principal_required must be boolean")
        if self.grant is not None and not isinstance(self.grant, LifecycleListener):
            raise TypeError("agent principal grant must be a LifecycleListener")

    @property
    def refuses_all(self) -> bool:
        """True when ``required`` is set but nothing can supply a principal."""

        return self.required and self.grant is None

    def check_available(self) -> None:
        """Fail fast where a route can refuse before reading its request."""

        if self.refuses_all:
            raise AgentPrincipalDenied(
                "Agent Runtime Principal is required: add a "
                f"{_DECORATOR} hook to use built-in routes"
            )

    async def resolve(self, connection: Any) -> AgentRuntimePrincipal | None:
        """Return the principal for one built-in invocation, or ``None``.

        ``None`` means compatibility mode and is only returned when no hook
        exists and ``required`` is not set. A hook that returns ``None`` denies
        the request, so a missing ``return`` can never widen access.
        """

        self.check_available()
        if self.grant is None:
            return None
        # Same read-only view `authenticate` hooks get, never the FastAPI object.
        value = self.grant.callback(
            _connection_context(connection), principal_for(connection)
        )
        if inspect.isawaitable(value):
            value = await value
        if value is None:
            raise AgentPrincipalDenied("Agent Runtime Principal was not granted")
        if not isinstance(value, AgentRuntimePrincipal):
            raise TypeError(
                f"agent principal hook {self.grant.identity} must return "
                "AgentRuntimePrincipal or None"
            )
        validate_agent_principal(value)
        return value


def builtin_principal_policy(
    listeners: Sequence[Any], *, required: bool
) -> BuiltinPrincipalPolicy:
    """Select the application's single grant hook from discovered listeners."""

    selected = tuple(
        item
        for item in listeners
        if isinstance(item, LifecycleListener) and item.phase == PHASE
    )
    if len(selected) > 1:
        identities = ", ".join(item.identity for item in selected)
        raise ValueError(f"only one {_DECORATOR} hook is allowed: {identities}")
    return BuiltinPrincipalPolicy(selected[0] if selected else None, required)


__all__ = [
    "AgentPrincipalDenied",
    "BuiltinPrincipalPolicy",
    "PHASE",
    "builtin_principal_policy",
]

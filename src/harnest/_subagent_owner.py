"""Private ownership scopes that close runtime SubAgents when their creator ends.

The tool wrapper imports this module on every managed tool call, so it must stay
free of heavy imports. ``harnest._subagent`` holds the SubAgent implementation.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


class SubAgentOwner:
    """Track SubAgents started inside one tool call, task run, or invocation."""

    __slots__ = ("kind", "_children", "_closed")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._children: list[Any] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        """Report whether this owner has finished and released its SubAgents."""

        return self._closed

    def adopt(self, child: Any) -> None:
        """Take ownership of one started SubAgent; a finished owner cannot adopt."""

        if self._closed:
            from ._subagent import SubAgentUnavailableError

            raise SubAgentUnavailableError(
                f"the {self.kind} that would own this SubAgent has already finished"
            )
        self._children.append(child)

    def release(self, child: Any) -> None:
        """Forget a SubAgent its holder closed before this owner finished."""

        try:
            self._children.remove(child)
        except ValueError:
            return

    async def aclose(self) -> None:
        """Close every still-open SubAgent, newest first, without masking errors.

        Cleanup failures are logged rather than raised: the owner is usually
        unwinding from its own result or exception, which must reach the caller.
        """

        if self._closed:
            return
        self._closed = True
        while self._children:
            child = self._children.pop()
            try:
                await child._close_from_owner()
            except Exception as error:
                from .logging import get_logger

                get_logger("subagent.audit").warning(
                    "subagent.close",
                    operation="subagent.close",
                    owner=self.kind,
                    outcome="failed",
                    error=type(error).__name__,
                )


_TOOL_OWNER: ContextVar[SubAgentOwner | None] = ContextVar(
    "harnest_subagent_tool_owner", default=None
)


class tool_owner_scope:
    """Make the current managed tool call the owner of SubAgents it starts."""

    __slots__ = ("_owner", "_token")

    async def __aenter__(self) -> SubAgentOwner:
        self._owner = SubAgentOwner("tool call")
        self._token = _TOOL_OWNER.set(self._owner)
        return self._owner

    async def __aexit__(self, *exc_info: Any) -> None:
        _TOOL_OWNER.reset(self._token)
        await self._owner.aclose()


def current_tool_owner() -> SubAgentOwner | None:
    """Return the innermost running tool call's owner, if any."""

    owner = _TOOL_OWNER.get()
    return None if owner is None or owner.closed else owner


__all__ = ["SubAgentOwner", "current_tool_owner", "tool_owner_scope"]

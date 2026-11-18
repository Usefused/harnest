"""Runtime SubAgents: short-lived agents that code creates and owns during a run.

Public names are exported from ``harnest.agent``. A SubAgent lowers an ordinary
``Agent`` definition through the running application's framework adapter and
runs it on a private runtime driver with in-memory sessions, so its transcript
never enters the application's session store. It inherits the owner's user,
agent principal, credentials, and the root's model and tool lifecycle hooks;
tools, MCP servers, and model come only from the definition it was given.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
import math
from typing import TYPE_CHECKING, Any

from ._subagent_owner import SubAgentOwner, current_tool_owner

if TYPE_CHECKING:
    from ._context_agent import AgentResponse
    from .agent import AgentDefinition


MAX_SUBAGENT_DEPTH = 3
"""How many SubAgents may be nested inside one another's runs."""

_DEFAULT_TIMEOUT = 300.0
_NESTING: ContextVar[int] = ContextVar("harnest_subagent_nesting", default=0)


class SubAgentError(RuntimeError):
    """Base class for SubAgent failures."""


class SubAgentUnavailableError(SubAgentError):
    """A SubAgent cannot start or run in the current place."""


class SubAgentDepthError(SubAgentUnavailableError):
    """Starting this SubAgent would exceed ``MAX_SUBAGENT_DEPTH`` nested levels."""


class SubAgentClosedError(SubAgentError):
    """The SubAgent was closed by its holder or by the owner that started it."""


class SubAgentHost:
    """Build private SubAgent runtimes from one compiled managed application."""

    def __init__(self, application: Any) -> None:
        """Retain the compiled application; nothing is built until a SubAgent starts."""

        self._application = application
        self._tool_pipeline: Any | None = None

    @property
    def framework(self) -> str:
        return self._application.framework

    def tool_pipeline(self) -> Any:
        """Return the root's tool lifecycle hooks for runs without an active pipeline."""

        if self._tool_pipeline is None:
            from .tool_lifecycle import ToolLifecyclePipeline

            self._tool_pipeline = ToolLifecyclePipeline(
                tuple(self._application.lifecycle_extensions)
            )
        return self._tool_pipeline

    def check_mcp_ownership(self, definition: "AgentDefinition") -> None:
        """Refuse MCP lifecycles that closing this SubAgent would close for others.

        An MCP client with lifecycle hooks is bound to one connection: closing it
        cannot be undone. A SubAgent closes what it starts, so it must not share
        such a client with the root agent or with another SubAgent.
        """

        claimed = {
            id(controller)
            for controller in _mcp_lifecycle_controllers(
                self._application.managed_definition
            )
        }
        for controller, name in _mcp_lifecycle_controllers(definition, named=True):
            if id(controller) in claimed or getattr(controller, "_state", "new") != "new":
                raise SubAgentError(
                    f"SubAgent {definition.name!r} reuses MCP client {name!r}, whose "
                    "lifecycle hooks belong to another agent; give each SubAgent "
                    "its own MCPClient and lifecycle instance"
                )

    async def open_driver(self, definition: "AgentDefinition") -> Any:
        """Lower one definition and start a driver that owns private sessions."""

        from .checkpoint import MemoryStore
        from .frameworks.base import RuntimeSessions
        from .frameworks.registry import get_framework

        application = self._application
        adapter = get_framework(application.framework)
        # Credential binders, framework extensions, and the model-lifecycle
        # extension apply to SubAgents exactly as they do to the root.
        extensions = tuple(application.framework_extensions)
        # Managed agents always run with a Harnest checkpoint store; a SubAgent
        # gets a private in-memory one that is discarded when it closes.
        checkpoints = MemoryStore()
        framework_checkpointer = adapter.managed_checkpointer(checkpoints)
        target = adapter.lower_managed(
            definition,
            framework_extensions=extensions,
            checkpointer=framework_checkpointer,
        )
        framework_app = adapter.wrap_managed(target, framework_extensions=extensions)
        variant = replace(
            application,
            name=definition.name,
            kind="agent",
            target=target,
            framework_app=framework_app,
            managed_definition=definition,
            bridge=None,
            input_schema=None,
            output_schema=None,
            # No shared storage, tasks, routes, or extensions: the driver falls
            # back to in-memory sessions that disappear when the SubAgent closes.
            session_store=None,
            checkpointer=checkpoints,
            framework_checkpointer=framework_checkpointer,
            task_store=None,
            cron_store=None,
            tasks=(),
            crons=(),
            http_routes=(),
            extensions=(),
        )
        driver = adapter.create_runtime_driver(
            variant,
            card=None,
            extra_endpoints=None,
            sessions=RuntimeSessions(),
            extension_manager=None,
        )
        try:
            starter = getattr(driver, "start", None)
            if callable(starter):
                await starter()
        except BaseException:
            await _close_quietly(driver)
            raise
        return driver


class SubAgent:
    """Spin up an ``Agent`` at runtime; whoever starts it owns its lifecycle.

    Use it inside a tool, a task, or a cron job::

        async with SubAgent(Agent(name="researcher", model=..., instruction=...)) as sub:
            reply = await sub.run("Find three sources")
            reply = await sub.run("Summarise them")   # same conversation
        # leaving the block closes it

    ``await SubAgent(...).start()`` also works without a ``with`` block. Either
    way, the SubAgent belongs to the innermost tool call, task run, or agent
    invocation that started it, and is closed automatically when that owner
    finishes if your code has not closed it already.
    """

    def __init__(
        self,
        agent: "AgentDefinition",
        *,
        request_timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """Validate the definition now; nothing runs until ``start``."""

        _validate_definition(agent)
        _validate_timeout(request_timeout)
        self._definition = agent
        self._request_timeout = float(request_timeout)
        self._state = "new"
        self._lock = asyncio.Lock()
        self._host: SubAgentHost | None = None
        self._driver: Any | None = None
        self._session: Any | None = None
        self._owner: SubAgentOwner | None = None
        self._user_id: str | None = None
        self._depth = 0

    @property
    def name(self) -> str:
        """Return the name of the agent definition this SubAgent runs."""

        return self._definition.name

    @property
    def is_open(self) -> bool:
        """Report whether ``run`` can be called."""

        return self._state == "open"

    @property
    def session_id(self) -> str | None:
        """Return the private session ID while open, for logs and debugging."""

        return None if self._session is None else self._session.id

    async def start(self) -> "SubAgent":
        """Build the agent and attach it to the current owner."""

        if self._state == "open":
            raise SubAgentError(f"SubAgent {self.name!r} is already started")
        if self._state == "closed":
            raise SubAgentClosedError(f"SubAgent {self.name!r} is closed")
        active = _active_context()
        host = getattr(active, "_subagent_host", None)
        if host is None:
            raise SubAgentUnavailableError(
                "SubAgent is available only in managed Harnest agents; "
                "advanced mode is not supported yet"
            )
        depth = _NESTING.get()
        if depth >= MAX_SUBAGENT_DEPTH:
            raise SubAgentDepthError(
                f"SubAgents can be nested at most {MAX_SUBAGENT_DEPTH} levels deep"
            )
        owner = current_tool_owner() or getattr(active, "_subagent_owner", None)
        if owner is None or owner.closed:
            raise SubAgentUnavailableError(
                "SubAgent needs an owner: start it inside a tool call, a task, "
                "or an agent invocation"
            )
        from ._context_agent import LocalAgentRuntime

        host.check_mcp_ownership(self._definition)
        driver = await host.open_driver(self._definition)
        try:
            runtime = LocalAgentRuntime(
                driver,
                user_id=active.user_id,
                transport="subagent",
                trigger="agent",
                request_timeout=self._request_timeout,
                metadata=active.metadata,
            )
            runtime._audit_action = "subagent"
            session = await runtime.create_session()
            owner.adopt(self)
        except BaseException:
            await _close_quietly(driver)
            raise
        self._host = host
        self._driver = driver
        self._session = session
        self._owner = owner
        self._user_id = active.user_id
        self._depth = depth
        self._state = "open"
        return self

    async def run(
        self, message: str, *, request_timeout: float | None = None
    ) -> "AgentResponse":
        """Send one message and wait for the reply; turns share one conversation.

        Turns run one at a time. Tools inside a SubAgent cannot wait for human
        approval, client tools, or durable results yet; reaching one raises
        ``SubAgentError``.
        """

        self._require_open()
        if not isinstance(message, str) or not message.strip():
            raise ValueError("SubAgent message must be non-empty text")
        if request_timeout is not None:
            _validate_timeout(request_timeout)
        active = _active_context()
        if active.user_id != self._user_id:
            raise SubAgentUnavailableError(
                "a SubAgent can run only for the user whose run started it"
            )
        from . import context as _context
        from ._context_agent import (
            AgentContinuationUnsupportedError,
            AgentPendingResponse,
        )
        from .tool_lifecycle import _ACTIVE_TOOL_PIPELINE, _tool_lifecycle_pipeline_scope

        async with self._lock:
            self._require_open()
            assert self._session is not None and self._host is not None
            token = _NESTING.set(self._depth + 1)
            try:
                with ExitStack() as scopes:
                    if _ACTIVE_TOOL_PIPELINE.get() is None:
                        # Tasks have no invocation pipeline; the root's tool
                        # hooks still govern what the SubAgent's tools do.
                        scopes.enter_context(
                            _tool_lifecycle_pipeline_scope(self._host.tool_pipeline())
                        )
                    scopes.enter_context(
                        _context.activate_context(
                            _context.derive_agent_context(active, agent_name=self.name)
                        )
                    )
                    response = await self._session.invoke(
                        message, request_timeout=request_timeout
                    )
            except AgentContinuationUnsupportedError as error:
                raise _unsupported_wait(self.name, error.kind) from error
            finally:
                _NESTING.reset(token)
        if isinstance(response, AgentPendingResponse):
            raise _unsupported_wait(self.name, "external_continuation")
        return response

    async def close(self) -> None:
        """Release the SubAgent's model, MCP connections, and session; idempotent."""

        if self._state == "closed":
            return
        previous, self._state = self._state, "closed"
        if previous == "new":
            return
        owner, self._owner = self._owner, None
        if owner is not None:
            owner.release(self)
        driver, self._driver = self._driver, None
        self._session = None
        if driver is not None:
            await driver.close()

    async def _close_from_owner(self) -> None:
        """Close when the owning tool call, task run, or invocation finishes."""

        await self.close()

    async def __aenter__(self) -> "SubAgent":
        if self._state == "new":
            await self.start()
        else:
            self._require_open()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"SubAgent(name={self.name!r}, state={self._state!r})"

    def _require_open(self) -> None:
        if self._state == "new":
            raise SubAgentError(
                f"SubAgent {self.name!r} is not started; use 'async with' or 'await start()'"
            )
        if self._state == "closed":
            raise SubAgentClosedError(f"SubAgent {self.name!r} is closed")


def _active_context() -> Any:
    """Return the managed run context or explain where SubAgents can be used."""

    from .context import optional_active_context

    try:
        active = optional_active_context()
    except Exception as error:
        raise SubAgentUnavailableError(
            "the Harnest run that owned this SubAgent has finished"
        ) from error
    if active is None:
        raise SubAgentUnavailableError(
            "SubAgent can be used only during a managed Harnest run: inside a "
            "tool, a task, or a cron job"
        )
    return active


_WAIT_LABELS = {
    "approval": "a human approval",
    "client_tool": "a client tool",
    "external_continuation": "a durable",
}


def _unsupported_wait(name: str, kind: str) -> SubAgentError:
    label = _WAIT_LABELS.get(kind, "an unsupported")
    return SubAgentError(
        f"SubAgent {name!r} stopped at {label} wait; human approval, client tools, "
        "and durable waits are not supported inside SubAgents yet"
    )


def _validate_definition(agent: Any) -> None:
    """Accept only the managed features a private SubAgent runtime supports."""

    from .agent import AgentDefinition

    if not isinstance(agent, AgentDefinition):
        raise TypeError("SubAgent needs a harnest.agent.Agent definition")
    if agent.instruction is None:
        raise ValueError(
            f"SubAgent {agent.name!r} needs an instruction; it has no "
            "instructions.md folder to read one from"
        )
    if agent.input_schema is not None or agent.output_schema is not None:
        raise ValueError(
            f"SubAgent {agent.name!r} cannot use input_schema or output_schema "
            "yet; SubAgents exchange text"
        )
    if agent.sandboxes:
        raise ValueError(f"SubAgent {agent.name!r} cannot use sandboxes yet")


def _validate_timeout(value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("SubAgent request_timeout must be positive and finite")


def _mcp_lifecycle_controllers(root: Any, *, named: bool = False) -> list[Any]:
    """Collect MCP lifecycle controllers declared anywhere in an agent tree."""

    if root is None:
        return []
    from .agent import AgentDefinition
    from .agent_tree import walk_agent_tree

    found: list[Any] = []
    for node in walk_agent_tree(root):
        if not isinstance(node, AgentDefinition):
            continue
        for client in node.mcp:
            controller = getattr(client, "_lifecycle_controller", None)
            if controller is None:
                continue
            describe = getattr(client, "_client_name", None)
            name = describe() if callable(describe) else type(client).__name__
            found.append((controller, name) if named else controller)
    return found


async def _close_quietly(driver: Any) -> None:
    """Release a half-built driver without replacing the original failure."""

    try:
        await driver.close()
    except Exception:
        return


async def close_owned_subagents(active: Any) -> None:
    """Close what an invocation or task run still owns before it is revoked."""

    owner = getattr(active, "_subagent_owner", None)
    if owner is not None:
        await owner.aclose()


__all__ = [
    "MAX_SUBAGENT_DEPTH",
    "SubAgent",
    "SubAgentClosedError",
    "SubAgentDepthError",
    "SubAgentError",
    "SubAgentHost",
    "SubAgentUnavailableError",
    "close_owned_subagents",
]

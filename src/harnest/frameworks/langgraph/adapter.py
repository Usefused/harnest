"""LangGraph adapter: how Harnest compiles, serves, and evaluates LangGraph agents.

This class is the table of contents for the LangGraph package. Each hook is
short and delegates to a module beside it; most of the adapter lives in two:

- ``lowering.py`` turns ``Agent`` definitions into LangChain agents and
  ``Graph`` definitions into compiled ``StateGraph`` applications
  (``lower_agent``, ``lower_graph``).
- ``runtime.py`` is ``LangGraphRuntimeDriver``, which runs the compiled graph
  for every Harnest transport (``create_runtime_driver``).

How LangGraph differs from the default contract, and why:

- Middleware is fixed when a graph is compiled, so ``framework_extensions``
  go straight into ``lower_agent``/``lower_graph`` and there is no
  ``wrap_managed`` application. For the same reason advanced graphs cannot
  take discovered middleware (``attach_advanced_extensions``).
- LangGraph saves checkpoints itself through a saver, so
  ``managed_checkpointer`` returns ``HarnestCheckpointSaver``
  (``checkpoint.py``) backed by the lifecycle's Harnest store.
- Durable tools pause with LangGraph's ``interrupt``, and the resulting
  ``GraphInterrupt`` is reported by ``is_suspension`` so hooks let it pass.
- Harnest's built-in evaluator is ADK's library, so ``evaluation.py`` presents
  the LangGraph runtime to it as an importable agent module.

Every hook imports LangGraph and LangChain lazily so selecting another framework
never requires them to be installed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from ..base import (
    AdvancedTarget,
    AdvancedTargetError,
    FrameworkAdapter,
    FrameworkConventionError,
    FrameworkDependencyError,
    RuntimeSessions,
    sdk_isinstance,
)

if TYPE_CHECKING:
    from ...agent import AgentDefinition, _AdvancedAgentDefinition
    from ...graph import Graph


class LangGraphFramework(FrameworkAdapter):
    """Lower portable definitions to compiled LangGraph graphs."""

    framework_extension_label: ClassVar[str] = "LangChain AgentMiddleware"
    # LangGraph re-enters the interrupted tool, so a resumed handle can restore
    # the authored exception contract without placing an exception in state.
    resume_reenters_tool: ClassVar[bool] = True
    retains_live_tool_frame: ClassVar[bool] = False

    # -- Compile -----------------------------------------------------------

    def lower_agent(
        self,
        definition: AgentDefinition,
        *,
        framework_extensions: Sequence[Any],
        checkpointer: Any,
    ) -> Any:
        """Compile one managed agent with middleware and the managed checkpointer."""

        from .lowering import lower_agent

        return lower_agent(
            definition, middleware=framework_extensions, checkpointer=checkpointer
        )

    def lower_graph(
        self,
        graph: Graph,
        *,
        framework_extensions: Sequence[Any],
        checkpointer: Any,
    ) -> Any:
        """Compile one portable graph with middleware and the managed checkpointer."""

        from .lowering import lower_graph

        return lower_graph(graph, middleware=framework_extensions, checkpointer=checkpointer)

    def validate_advanced(
        self, value: _AdvancedAgentDefinition, *, fallback_name: str
    ) -> AdvancedTarget:
        """Accept only compiled Pregel targets at the advanced ownership boundary."""

        try:
            from langgraph.pregel import Pregel
        except ImportError as exc:  # pragma: no cover - optional backend
            raise FrameworkDependencyError(
                "advanced LangGraph mode requires langgraph"
            ) from exc

        target = value.target
        if not isinstance(target, Pregel):
            raise AdvancedTargetError(
                "advanced LangGraph mode expects a compiled langgraph Pregel"
            )
        target.validate()
        return AdvancedTarget.named(
            target, authored_name=value.name, fallback_name=fallback_name
        )

    def attach_advanced_extensions(
        self,
        advanced: AdvancedTarget,
        framework_extensions: Sequence[Any],
        prepend_extensions: Sequence[Any] = (),
    ) -> AdvancedTarget:
        """Middleware is fixed once an advanced graph has been compiled."""

        if not framework_extensions and not prepend_extensions:
            return advanced
        raise FrameworkConventionError(
            "advanced LangGraph applications cannot attach discovered middleware "
            "after graph compilation; apply it before Agent.advanced(...)"
        )

    def managed_checkpointer(self, provider: Any) -> Any | None:
        """Install Harnest storage through LangGraph's checkpoint protocol."""

        from .storage import _checkpointer

        return _checkpointer(provider)

    def validate_advanced_checkpointer(
        self, advanced: AdvancedTarget, provider: Any
    ) -> None:
        """Match lifecycle ownership to the checkpointer compiled into the graph."""

        from ...checkpoint import HarnestStore
        from .storage import LangGraphStore

        native = getattr(advanced.target, "checkpointer", None)
        if isinstance(provider, LangGraphStore):
            if native is not provider:
                raise FrameworkConventionError(
                    "advanced LangGraph target must compile with the same LangGraphStore "
                    "returned by @lifecycle.storage.checkpoints"
                )
            return
        if not isinstance(provider, HarnestStore):
            raise FrameworkConventionError(
                "advanced LangGraph agents require HarnestStore or LangGraphStore"
            )
        if getattr(native, "store", None) is not provider:
            raise FrameworkConventionError(
                "advanced LangGraph target must compile with "
                "store.framework_checkpointer() when Harnest owns checkpointing"
            )

    # -- Run ---------------------------------------------------------------

    def build_litellm_model(self, model: Any) -> Any:
        """Build LangChain's ``ChatLiteLLM`` adapter for one ``LiteLLMModel``."""

        from .models import build_litellm_model

        return build_litellm_model(model)

    def create_runtime_driver(
        self,
        application: Any,
        *,
        card: Mapping[str, Any] | None,
        extra_endpoints: Mapping[str, str] | None,
        sessions: RuntimeSessions,
        extension_manager: Any | None,
    ) -> Any:
        """Resolve compiled versus host-injected LangGraph session ownership."""

        from ...runtime import AgentRuntimeError
        from .runtime import LangGraphRuntimeDriver

        lifecycle_store = application.runtime_capabilities.session_store
        if lifecycle_store is not None and sessions.store is not None:
            raise AgentRuntimeError(
                "@lifecycle.storage.sessions cannot be combined with an injected "
                "LangGraph session store"
            )
        return LangGraphRuntimeDriver(
            application,
            card=card,
            session_store=(
                lifecycle_store if lifecycle_store is not None else sessions.store
            ),
        )

    # -- Lifecycle ---------------------------------------------------------

    def framework_extension_type(self) -> type[Any]:
        """Return LangChain's agent middleware base class."""

        from langchain.agents.middleware import AgentMiddleware

        return AgentMiddleware

    def is_injected_tool_context(self, value: Any) -> bool:
        """LangGraph injects a ``ToolRuntime`` into tools that declare one."""

        # The module check avoids importing LangGraph for every non-LangGraph argument.
        return type(value).__module__.startswith("langgraph.") and sdk_isinstance(
            value, "langgraph.prebuilt.tool_node", "ToolRuntime"
        )

    def model_lifecycle_extension(self, pipeline: Any) -> Any:
        """Adapt portable model hooks to one LangChain agent middleware."""

        from .model_hooks import _langgraph_middleware

        return _langgraph_middleware(pipeline)

    # -- Messages ----------------------------------------------------------

    def is_user_turn(self, message: dict[str, Any]) -> bool:
        """LangChain marks human turns with the ``human`` message type."""

        return message.get("type") == "human"

    def is_instruction(self, message: dict[str, Any]) -> bool:
        """Pin system/developer messages independently of conversation retention."""

        return message.get("type") in {"system", "developer"}

    def reduce_tool_result(self, message: dict[str, Any], limit: int) -> None:
        """Keep non-text content blocks intact, including media and provider data."""

        from .tokens import reduce_tool_result_text

        reduce_tool_result_text(message, limit)

    # -- MCP ---------------------------------------------------------------

    def mcp_tools(
        self,
        calls: Any,
        schemas: list[Any],
        *,
        server_name: str | None,
        interceptors: Any,
    ) -> list[Any]:
        """Convert MCP schemas with the LangChain MCP adapters."""

        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

        return [
            convert_mcp_tool_to_langchain_tool(
                calls,
                schema,
                server_name=server_name,
                tool_name_prefix=True,
                tool_interceptors=interceptors,
            )
            for schema in schemas
        ]

    # -- Durable tools -----------------------------------------------------

    def is_suspension(self, error: BaseException) -> bool:
        """Recognize checkpoint interrupts without importing an optional SDK."""

        error_type = type(error)
        return (
            error_type.__name__ == "GraphInterrupt"
            and error_type.__module__.startswith("langgraph.")
        )

    def suspend_durable(self, pending: Any) -> Any:
        """Suspend through LangGraph's checkpointed ``interrupt``."""

        from langgraph.types import interrupt

        return interrupt(pending.public())

    # -- Evaluations -------------------------------------------------------

    def run_builtin_evals(
        self,
        *,
        artifact: Any,
        application: Any,
        suite: Any,
        trajectory: str,
        print_results: bool,
        result_output: Any,
    ) -> int:
        """Evaluate the compiled LangGraph application through ADK's evaluator."""

        from .evaluation import run_builtin_evals

        return run_builtin_evals(
            application,
            suite,
            trajectory=trajectory,
            print_results=print_results,
            result_output=result_output,
        )

    def playground_eval_agent_module(self, application: Any, driver: Any) -> Any:
        """Evaluate the active neutral LangGraph driver."""

        from ...evaluation import EvaluationError
        from .evaluation import runtime_eval_agent_module

        if driver is None:
            raise EvaluationError("LangGraph playground evals require a runtime driver")
        return runtime_eval_agent_module(driver)


__all__ = ["LangGraphFramework"]

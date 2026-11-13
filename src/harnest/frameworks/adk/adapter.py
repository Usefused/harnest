"""Google ADK adapter: how Harnest compiles, serves, and evaluates ADK agents.

This class is the table of contents for the ADK package. Each hook is short
and delegates to a module beside it; most of the adapter lives in two of them:

- ``lowering.py`` turns ``Agent`` and ``Graph`` definitions into ADK
  ``LlmAgent`` objects and workflow nodes (``lower_agent``, ``lower_graph``).
- ``runtime.py`` is ``ADKRuntimeDriver``, which runs the ADK ``App`` on ADK's
  ``Runner`` for every Harnest transport (``create_runtime_driver``).

How ADK differs from the default contract, and why:

- Plugins attach to the ``App``, not the agent. ``lower_agent`` ignores
  ``framework_extensions``; ``wrap_managed`` builds the ``App`` with them and
  with resumability on, which approvals and checkpoints need.
- ADK persists through its session service, so ``managed_checkpointer`` keeps
  the default ``None`` and ``host_sessions``/``create_runtime_driver`` choose
  exactly one session service (``sessions.py`` bridges a Harnest
  ``SessionStore``; ``storage.py`` holds ``ADKStore`` for advanced apps).
- Advanced apps are served by ADK's own FastAPI server (``server.py``) so
  ADK's routes keep working, behind Harnest's limits and auth.
- ADK's evaluator is Harnest's built-in eval engine, so ``evaluation.py``
  runs the native ``App`` directly.

Every hook imports ADK lazily so selecting another framework never requires
``google-adk`` to be installed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
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


class ADKFramework(FrameworkAdapter):
    """Lower portable definitions to ADK agents and run them on ADK's Runner."""

    framework_extension_label: ClassVar[str] = "ADK BasePlugin"
    framework_session_store_label: ClassVar[str | None] = "ADKStore"
    framework_code_executor_label: ClassVar[str | None] = "ADK BaseCodeExecutor"
    # ADK resumes its model loop rather than the Python tool frame, and keeps
    # a live tool frame waiting while its live transport stays attached.
    resume_reenters_tool: ClassVar[bool] = False
    retains_live_tool_frame: ClassVar[bool] = True

    # -- Compile -----------------------------------------------------------

    def lower_agent(
        self,
        definition: AgentDefinition,
        *,
        framework_extensions: Sequence[Any],
        checkpointer: Any,
    ) -> Any:
        """Build the ADK ``LlmAgent`` the definition describes."""

        # Plugins (framework_extensions) belong to the App built in
        # wrap_managed, and ADK checkpoints through its session service, so
        # neither argument is used here.
        from .lowering import build_agent

        return build_agent(definition)

    def lower_graph(
        self,
        graph: Graph,
        *,
        framework_extensions: Sequence[Any],
        checkpointer: Any,
    ) -> Any:
        """Lower a portable graph to ADK workflow nodes."""

        from .lowering import lower_graph

        return lower_graph(graph)

    def wrap_managed(
        self, target: Any, *, framework_extensions: Sequence[Any] = ()
    ) -> Any | None:
        """Create the ADK App only after capabilities and plugins are complete."""

        from ...agent import AgentDefinition

        # Tests and compiler extensions may replace ADK lowering with a
        # validation-only definition. Preserve that observable boundary without
        # pretending it is a runnable ADK application.
        if isinstance(target, AgentDefinition):
            return None
        try:
            from google.adk.apps import App
            from google.adk.apps.app import ResumabilityConfig
        except ImportError as exc:  # pragma: no cover - optional backend
            raise FrameworkDependencyError("ADK compilation requires google-adk") from exc
        from ._warnings import suppress_adk_warnings

        # Resumability is required by Harnest's approval/checkpoint contract; its
        # experimental status is an implementation choice, not an authoring issue.
        with suppress_adk_warnings("resumability"):
            return App(
                name=target.name,
                root_agent=target,
                plugins=list(framework_extensions),
                resumability_config=ResumabilityConfig(is_resumable=True),
            )

    def validate_advanced(
        self, value: _AdvancedAgentDefinition, *, fallback_name: str
    ) -> AdvancedTarget:
        """Normalize supported native ADK roots without exposing NativeApp publicly."""

        try:
            from google.adk.agents import BaseAgent
            from google.adk.apps import App
            from google.adk.workflow import BaseNode
        except ImportError as exc:  # pragma: no cover - optional backend
            raise FrameworkDependencyError("advanced ADK mode requires google-adk") from exc

        target = value.target
        framework_app = None
        if isinstance(target, App):
            framework_app = target
            target = target.root_agent
        elif isinstance(target, (BaseAgent, BaseNode)):
            name = value.name or getattr(target, "name", fallback_name)
            framework_app = App(name=name, root_agent=target)
        else:
            raise AdvancedTargetError(
                "advanced ADK mode expects google.adk App, BaseAgent, or BaseNode"
            )
        return AdvancedTarget.named(
            target,
            authored_name=value.name,
            fallback_name=fallback_name,
            framework_app=framework_app,
        )

    def prepare_advanced(self, advanced: AdvancedTarget) -> AdvancedTarget:
        """Enable the ADK resumability the checkpoint contract requires."""

        if advanced.framework_app is None:
            return advanced
        config = getattr(advanced.framework_app, "resumability_config", None)
        if bool(getattr(config, "is_resumable", False)):
            return advanced
        from google.adk.apps.app import ResumabilityConfig

        from ._warnings import suppress_adk_warnings

        with suppress_adk_warnings("resumability"):
            framework_app = advanced.framework_app.model_copy(
                update={"resumability_config": ResumabilityConfig(is_resumable=True)}
            )
        return replace(advanced, framework_app=framework_app)

    def attach_advanced_extensions(
        self,
        advanced: AdvancedTarget,
        framework_extensions: Sequence[Any],
        prepend_extensions: Sequence[Any] = (),
    ) -> AdvancedTarget:
        """Attach ADK plugins only where native construction remains mutable."""

        if not framework_extensions and not prepend_extensions:
            return advanced
        app = advanced.framework_app
        if app is None:
            raise FrameworkConventionError("advanced ADK lifecycle plugins require an ADK App")
        # Harnest-owned security binders must run before plugins already attached
        # to an advanced App; ordinary discovered plugins retain append semantics.
        plugins = (
            tuple(prepend_extensions)
            + tuple(getattr(app, "plugins", ()))
            + tuple(framework_extensions)
        )
        _reject_duplicate_plugins(plugins, "duplicate ADK application plugin names: ")
        framework_app = app.model_copy(update={"plugins": list(plugins)})
        return replace(advanced, framework_app=framework_app)

    def validate_advanced_checkpointer(
        self, advanced: AdvancedTarget, provider: Any
    ) -> None:
        """Require Harnest or ADK ownership and an explicitly resumable App."""

        from ...checkpoint import HarnestStore
        from .storage import ADKStore

        if not isinstance(provider, (HarnestStore, ADKStore)):
            raise FrameworkConventionError(
                "advanced ADK agents require HarnestStore or ADKStore checkpoint ownership"
            )
        if advanced.framework_app is None:
            return
        config = getattr(advanced.framework_app, "resumability_config", None)
        if not bool(getattr(config, "is_resumable", False)):
            raise FrameworkConventionError(
                "advanced ADK App must enable ResumabilityConfig(is_resumable=True)"
            )

    def credential_extensions(
        self, provider: Any, framework_extensions: Sequence[Any]
    ) -> tuple[Any, ...]:
        """Bind private credentials across ADK's neutral and native run surfaces."""

        if provider is None:
            return ()
        from .credentials import adk_credential_plugin

        plugin = adk_credential_plugin(provider)
        if any(
            getattr(candidate, "name", None) == plugin.name
            for candidate in framework_extensions
        ):
            raise FrameworkConventionError(
                f"ADK plugin name {plugin.name!r} is reserved by Harnest credentials"
            )
        return (plugin,)

    # -- Run ---------------------------------------------------------------

    def build_litellm_model(self, model: Any) -> Any:
        """Build ADK's ``LiteLlm`` adapter for one ``LiteLLMModel``."""

        from .models import build_litellm_model

        return build_litellm_model(model)

    def host_sessions(
        self,
        artifact: str | Path,
        application: Any,
        *,
        framework_storage: Any | None,
        store: Any | None,
    ) -> RuntimeSessions:
        """Select one ADK session authority before the native application is built."""

        from .storage import ADKStore

        if isinstance(application.checkpointer, ADKStore):
            if framework_storage is not None:
                raise FrameworkConventionError(
                    "ADKStore cannot be combined with deployment ADK session storage"
                )
            return RuntimeSessions(framework_service=application.checkpointer.session_service)
        if store is not None and framework_storage is not None:
            raise FrameworkConventionError(
                "session_store and framework_session_storage cannot be combined"
            )
        if framework_storage is not None:
            source = str(Path(artifact).resolve() / "source")
            return RuntimeSessions(framework_service=framework_storage.create_service(source))
        # A portable host store reaches ADK through the same session-service
        # bridge as a lifecycle-owned store.
        portable = store if store is not None else application.session_store
        if portable is None:
            return RuntimeSessions()
        from .sessions import create_adk_session_service

        return RuntimeSessions(framework_service=create_adk_session_service(portable))

    def create_runtime_driver(
        self,
        application: Any,
        *,
        card: Mapping[str, Any] | None,
        extra_endpoints: Mapping[str, str] | None,
        sessions: RuntimeSessions,
        extension_manager: Any | None,
    ) -> Any:
        """Give one ADK service sole ownership of native session persistence."""

        from .storage import ADKStore
        from ...runtime import AgentRuntimeError
        from .runtime import ADKRuntimeDriver

        session_service = sessions.framework_service
        capabilities = application.runtime_capabilities
        provider = capabilities.checkpointer
        if isinstance(provider, ADKStore):
            if session_service is not None:
                raise AgentRuntimeError(
                    "ADKStore cannot be combined with an injected ADK session service"
                )
            session_service = provider.session_service
        if capabilities.session_store is not None and session_service is None:
            from .sessions import create_adk_session_service

            session_service = create_adk_session_service(capabilities.session_store)
        return ADKRuntimeDriver(
            application,
            card=card,
            extra_endpoints=extra_endpoints,
            session_service=session_service,
            extension_manager=extension_manager,
        )

    def observability_defaults(self) -> Mapping[str, str]:
        """Keep prompts and responses out of ADK and GenAI spans by default."""

        return {
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
            "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
        }

    def exposes_framework_server(self, application: Any) -> bool:
        """Serve advanced ADK applications through ADK's own API surface."""

        return application.mode == "advanced"

    def build_framework_server(self, artifact: Any, application: Any, **options: Any) -> Any:
        """Build ADK's FastAPI server behind Harnest's neutral transport policy."""

        from .server import _build_native_adk_app

        return _build_native_adk_app(artifact, application=application, **options)

    # -- Lifecycle ---------------------------------------------------------

    def framework_extension_type(self) -> type[Any]:
        """Return ADK's plugin base class."""

        from google.adk.plugins.base_plugin import BasePlugin

        return BasePlugin

    def validate_framework_extensions(self, values: tuple[Any, ...]) -> None:
        """ADK identifies plugins by name, so names must be unique."""

        _reject_duplicate_plugins(values, "duplicate ADK framework lifecycle plugin names: ")

    def model_lifecycle_extension(self, pipeline: Any) -> Any:
        """Adapt portable model hooks to one ADK plugin."""

        from .model_hooks import _adk_plugin

        return _adk_plugin(pipeline)

    def is_framework_code_executor(self, value: Any) -> bool:
        """Accept ADK ``BaseCodeExecutor`` providers for native ADK compatibility."""

        return sdk_isinstance(value, "google.adk.code_executors", "BaseCodeExecutor")

    def is_injected_tool_context(self, value: Any) -> bool:
        """ADK injects a ``ToolContext`` into tools that declare one."""

        # The module check avoids importing ADK for every non-ADK argument.
        return type(value).__module__.startswith("google.adk.") and sdk_isinstance(
            value, "google.adk.tools.tool_context", "ToolContext"
        )

    def accepts_framework_session_store(self, value: Any) -> bool:
        """Accept the explicit advanced ADK storage wrapper from lifecycle factories."""

        from .storage import ADKStore

        return isinstance(value, ADKStore)

    # -- Messages ----------------------------------------------------------

    def is_user_turn(self, message: dict[str, Any]) -> bool:
        """Distinguish human turns from ADK user-role function responses."""

        return message.get("role") == "user" and not any(
            part.get("function_response") for part in message.get("parts", ())
        )

    def is_instruction(self, message: dict[str, Any]) -> bool:
        """Pin system/developer messages independently of conversation retention."""

        return message.get("role") in {"system", "developer"}

    def reduce_tool_result(self, message: dict[str, Any], limit: int) -> None:
        """Retain function response identity while reducing its JSON text leaves."""

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
        """Reuse ADK schema conversion with Harnest's governed MCP transport."""

        from .mcp_tools import http_mcp_tool

        return [http_mcp_tool(schema, calls) for schema in schemas]

    # -- Durable tools -----------------------------------------------------

    def suspend_durable(self, pending: Any) -> Any:
        """Suspend through ADK's long-running tool contract."""

        from ...durable import FrameworkDurableSuspended

        # ADK suppresses its FunctionResponse only when the long-running tool
        # returns no value. Control flow prevents authored statements after the
        # wait from observing a misleading pending dictionary.
        raise FrameworkDurableSuspended

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
        """Evaluate the native ADK App with ADK's evaluator."""

        from .evaluation import run_builtin_evals

        return run_builtin_evals(
            artifact,
            suite,
            trajectory=trajectory,
            print_results=print_results,
            result_output=result_output,
        )

    def evaluation_runtime(self, application: Any, driver: Any = None) -> Any:
        """Own or borrow ADK runtime capabilities for one evaluation run."""

        from .evaluation import adk_evaluation_runtime

        return adk_evaluation_runtime(application, driver)

    def playground_eval_agent_module(self, application: Any, driver: Any) -> Any:
        """Evaluate the native ADK App for full fidelity."""

        from .evaluation import adk_eval_agent_module

        return adk_eval_agent_module(application)


def _reject_duplicate_plugins(plugins: Sequence[Any], message: str) -> None:
    """Reject plugin sets ADK would silently collapse by name."""

    names = [getattr(plugin, "name", None) for plugin in plugins]
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    if duplicates:
        raise FrameworkConventionError(message + ", ".join(duplicates))


__all__ = ["ADKFramework"]

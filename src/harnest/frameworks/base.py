"""The contract every Harnest framework adapter implements.

A framework adapter is the only place that knows how a specific agent
framework (Google ADK, LangGraph, ...) works. Harnest's compiler, runtime,
lifecycle loader, token policies, MCP client, durable tools, and evaluators
call the hooks on :class:`FrameworkAdapter` and never compare framework names.
Anything two frameworks would implement the same way belongs to Harnest, not to
an adapter; an adapter only translates between Harnest's contracts and the
framework's own objects.

Adding a framework
==================

1. Describe it in ``frameworks.json`` (name, display name, packages, version
   range, capabilities) and copy the file to ``internal/frameworks/`` for the
   Go CLI.
2. Create ``harnest/frameworks/<name>/adapter.py`` with a subclass of
   :class:`FrameworkAdapter`. Implement every ``@abstractmethod`` hook, then
   the optional hooks behind each :class:`Capability` you declare.
3. Register the class in ``_ADAPTERS`` in ``registry.py``.

``docs/frameworks.md`` has the full checklist (CLI scaffold, storage schema,
tests). The smallest working adapter is ``_EchoFramework`` in
``tests/python/test_framework_adapters.py``.

When Harnest calls each hook
============================

``harnest compile`` (managed mode, ``Agent(...)`` or ``Graph(...)`` roots)
    ``model_lifecycle_extension`` → ``credential_extensions`` →
    ``managed_checkpointer`` → ``lower_managed`` (which calls ``lower_agent``
    or ``lower_graph``) → ``wrap_managed``.
``harnest compile`` (advanced mode, ``Agent.advanced(target=...)``)
    ``validate_advanced`` → ``credential_extensions`` →
    ``attach_advanced_extensions`` → ``prepare_advanced`` →
    ``validate_advanced_checkpointer``.
Lifecycle discovery (before lowering)
    ``framework_extension_type``, ``validate_framework_extensions``,
    ``accepts_framework_session_store``.
Serving an artifact
    ``observability_defaults`` → ``host_sessions`` → ``create_runtime_driver``;
    when ``exposes_framework_server`` is true, ``build_framework_server``
    replaces Harnest's neutral server.
During invocations
    ``build_litellm_model`` (model construction), ``mcp_tools``, token-policy
    hooks (``is_user_turn``, ``is_instruction``, ``reduce_tool_result``),
    durable-tool hooks (``suspend_durable``, ``is_suspension``).
``harnest test --evals`` and playground evals
    ``run_builtin_evals``, ``evaluation_runtime``, ``playground_eval_agent_module``.

Rules every adapter follows
===========================

- Import the framework SDK inside hook bodies, never at module import time.
  The registry loads every adapter, for example to ask all of them whether an
  exception is framework control flow, even when only one SDK is installed.
  Use :func:`sdk_isinstance` for "is this an SDK object" checks.
- Optional hooks document their default: neutral behavior or an
  :class:`UnsupportedFrameworkFeature` error. Check the relevant capability
  before requesting optional features.
- Declare a :class:`Capability` only after implementing its hooks;
  ``test_framework_adapters.py`` checks the two match.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from importlib import import_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ..agent import AgentDefinition, _AdvancedAgentDefinition
    from ..graph import Graph
    from ..session import SessionStore

FrameworkName = str
"""A registered framework identifier, as written in ``spec.framework.name``."""


class Capability(str, enum.Enum):
    """Optional behavior a framework declares in ``frameworks.json``.

    Callers check ``adapter.supports(capability)`` before using the matching
    hooks, and the Go CLI reads the same list (for example to offer
    ``harnest add subagent`` only where it works).
    """

    GRAPHS = "graphs"
    """Managed ``Graph`` roots can be lowered (``lower_graph``)."""

    FRAMEWORK_API_SERVER = "framework-api-server"
    """Advanced applications are served by the framework's own HTTP API
    (``exposes_framework_server``, ``build_framework_server``)."""

    FRAMEWORK_SESSION_SERVICE = "framework-session-service"
    """Hosts may inject a framework-specific session service instead of a
    SessionStore (``host_sessions``, ``accepts_framework_session_store``)."""

    ROOT_AGENT_SUBAGENTS = "root-agent-subagents"
    """A managed root agent consumes ``subagents/`` resources directly. Checked
    by the compiler and CLI; no adapter hook."""

    EVALS = "evals"
    """``harnest test --evals`` and playground evals can drive this framework
    (``run_builtin_evals``, ``playground_eval_agent_module``)."""

    GENERATE_CONTENT_CONFIG = "generate-content-config"
    """``Agent(generate_content_config=...)`` reaches the framework's model.
    Without it the compiler warns and ``lower_agent`` must ignore the field."""


class UnknownFrameworkError(ValueError):
    """The configured framework is not registered with this Harnest release."""


class FrameworkDependencyError(RuntimeError):
    """The selected framework's optional runtime dependencies are unavailable.

    Raise it from a hook when importing the SDK fails; the compiler reports it
    as a missing-package error rather than a bug.
    """


class AdvancedTargetError(TypeError):
    """An advanced-mode export is not executable by the selected framework."""


class FrameworkConventionError(ValueError):
    """Authored resources conflict with a framework-specific ownership rule.

    Raise it for configurations the user can fix (two session authorities,
    duplicate plugin names, extensions that cannot be attached).
    """


class UnsupportedFrameworkFeature(RuntimeError):
    """An optional hook was used with a framework that does not implement it."""


@dataclass(frozen=True, slots=True)
class DoctorModule:
    """One importable module ``harnest doctor`` checks for this framework."""

    module: str
    distribution: str


@dataclass(frozen=True, slots=True)
class FrameworkMetadata:
    """Release-owned facts shared with the Go CLI through ``frameworks.json``.

    Adapters read it through :attr:`FrameworkAdapter.metadata`; nothing here is
    set in Python code.
    """

    name: FrameworkName
    display_name: str
    distribution: str
    versions: str
    install_extra: str
    requirements: tuple[str, ...]
    mcp_requirements: tuple[str, ...]
    doctor_modules: tuple[DoctorModule, ...]
    capabilities: frozenset[Capability]


@dataclass(frozen=True, slots=True)
class AdvancedTarget:
    """A validated advanced export plus any framework application wrapper.

    ``target`` is the framework's runnable root. ``framework_app`` is an
    optional application object around it (ADK's ``App``) that some hooks need
    to update, for example to attach plugins.
    """

    name: str
    target: Any
    framework_app: Any | None = None

    @classmethod
    def named(
        cls,
        target: Any,
        *,
        authored_name: str | None,
        fallback_name: str,
        framework_app: Any | None = None,
    ) -> AdvancedTarget:
        """Name a target by its authored name, then the framework's name, then its folder.

        Use this from ``validate_advanced`` so every framework names advanced
        agents the same way.
        """

        name = authored_name or getattr(target, "name", None) or fallback_name.replace("-", "_")
        return cls(name=name, target=target, framework_app=framework_app)


@dataclass(frozen=True, slots=True)
class RuntimeSessions:
    """Session authorities a host offers to a framework runtime driver.

    ``framework_service`` is a framework-specific session service (for example an ADK
    ``BaseSessionService``) and is only offered to frameworks that declare
    :attr:`Capability.FRAMEWORK_SESSION_SERVICE`. ``store`` is the portable
    Harnest ``SessionStore`` every framework can accept.
    """

    framework_service: Any | None = None
    store: SessionStore | None = None


class FrameworkAdapter(ABC):
    """Everything Harnest needs to compile, serve, and evaluate one framework.

    The registry creates one instance per framework and caches it, so adapters
    must be stateless apart from their metadata. Hooks marked abstract are
    required; every other hook has a working default, described in its
    docstring.
    """

    framework_extension_label: ClassVar[str] = "framework lifecycle extension"
    """Name of the type ``@lifecycle.framework_extension`` factories return,
    used in error messages (for example ``"ADK BasePlugin"``)."""

    framework_session_store_label: ClassVar[str | None] = None
    """Name of the framework-specific type a lifecycle session factory may return
    besides ``SessionStore`` (for example ``"ADKStore"``), used in error
    messages. ``None`` when ``accepts_framework_session_store`` is always false."""

    framework_code_executor_label: ClassVar[str | None] = None
    """Name of the framework's own code executor a sandbox provider may return
    besides ``SandboxBackend``, used in error messages. ``None`` when
    ``is_framework_code_executor`` is always false."""

    resume_reenters_tool: ClassVar[bool] = True
    """Whether resuming a suspended durable tool re-enters the Python tool frame.

    When false, the framework resumes its model loop and receives the stored
    outcome as the tool's response instead. External continuations read this
    to decide how a resumed tool sees its result.
    """

    retains_live_tool_frame: ClassVar[bool] = False
    """Whether a live (WebSocket) transport can keep a suspended tool frame
    waiting in-process instead of persisting it."""

    def __init__(self, metadata: FrameworkMetadata) -> None:
        """Bind this adapter to its release-owned metadata."""

        self._metadata = metadata

    @property
    def name(self) -> FrameworkName:
        """Return the identifier used in ``spec.framework.name``."""

        return self._metadata.name

    @property
    def metadata(self) -> FrameworkMetadata:
        """Return the shared release metadata for this framework."""

        return self._metadata

    def supports(self, capability: Capability) -> bool:
        """Report whether this framework declares one optional capability."""

        return capability in self._metadata.capabilities

    def _unsupported(self, feature: str) -> UnsupportedFrameworkFeature:
        """Build one consistent error for an optional hook this framework lacks."""

        return UnsupportedFrameworkFeature(
            f"{self._metadata.display_name} does not support {feature}"
        )

    # -- Compile -----------------------------------------------------------
    # Called by the compiler (bundle.py) while it turns an agent folder into a
    # CompiledApplication. Managed roots are lowered; advanced roots are
    # validated and adjusted, never rebuilt.

    @abstractmethod
    def lower_agent(
        self,
        definition: AgentDefinition,
        *,
        framework_extensions: Sequence[Any],
        checkpointer: Any,
    ) -> Any:
        """Lower one managed ``Agent`` definition to the framework's runnable root.

        Called through ``lower_managed`` when the agent root is an ``Agent``,
        and by ``Agent.build()`` with no extensions and no checkpointer.
        ``definition`` is fully composed: tools, MCP clients, skills,
        subagents, and the model are already resolved.
        ``framework_extensions`` holds, in order, credential binders,
        ``@lifecycle.framework_extension`` results, and the model-lifecycle
        extension. ``checkpointer`` is what ``managed_checkpointer`` returned.

        Return the object the runtime driver will execute (ADK: an
        ``LlmAgent``; LangGraph: a compiled graph). Raise ``ValueError`` for
        definition features the framework cannot express, except
        ``generate_content_config``, which is ignored unless the framework
        declares :attr:`Capability.GENERATE_CONTENT_CONFIG`.
        """

    def lower_graph(
        self,
        graph: Graph,
        *,
        framework_extensions: Sequence[Any],
        checkpointer: Any,
    ) -> Any:
        """Lower one managed ``Graph``; requires :attr:`Capability.GRAPHS`.

        Same inputs and result as ``lower_agent``, for ``Graph`` roots and
        ``Graph.build()``. The default raises ``UnsupportedFrameworkFeature``.
        """

        raise self._unsupported("managed graphs")

    def lower_managed(
        self,
        value: AgentDefinition | Graph,
        *,
        framework_extensions: Sequence[Any] = (),
        checkpointer: Any = None,
    ) -> Any:
        """Route one portable root through the agent or graph lowering hook.

        The compiler's entry point for managed roots. Adapters rarely override
        it; override ``lower_agent`` and ``lower_graph`` instead.
        """

        from ..agent import AgentDefinition
        from ..graph import Graph

        if isinstance(value, AgentDefinition):
            return self.lower_agent(
                value, framework_extensions=framework_extensions, checkpointer=checkpointer
            )
        if isinstance(value, Graph):
            return self.lower_graph(
                value, framework_extensions=framework_extensions, checkpointer=checkpointer
            )
        raise TypeError("managed backend input must be AgentDefinition or Graph")

    def wrap_managed(
        self, target: Any, *, framework_extensions: Sequence[Any] = ()
    ) -> Any | None:
        """Wrap a lowered root in the framework's application object, if it has one.

        Called once after lowering, with the same ``framework_extensions``. The
        result becomes ``CompiledApplication.framework_app``. ADK returns an
        ``App`` carrying its plugins; frameworks whose lowered root runs on its
        own (the default) return ``None``.
        """

        return None

    @abstractmethod
    def validate_advanced(
        self, value: _AdvancedAgentDefinition, *, fallback_name: str
    ) -> AdvancedTarget:
        """Accept one ``Agent.advanced(target=...)`` export.

        ``value.target`` is whatever the author built with the framework
        directly. Check it is a type the framework can run, then return
        ``AdvancedTarget.named(target, authored_name=value.name,
        fallback_name=fallback_name)``; ``fallback_name`` is the agent folder
        name. Raise ``AdvancedTargetError`` for the wrong type and
        ``FrameworkDependencyError`` when the SDK is missing.
        """

    def prepare_advanced(self, advanced: AdvancedTarget) -> AdvancedTarget:
        """Turn on framework settings Harnest's contracts depend on.

        Called after extensions are attached and before the checkpoint check.
        Return a copy rather than mutating the author's object. ADK enables resumability here, which
        approvals and checkpoints need. The default returns ``advanced`` unchanged.
        """

        return advanced

    def attach_advanced_extensions(
        self,
        advanced: AdvancedTarget,
        framework_extensions: Sequence[Any],
        prepend_extensions: Sequence[Any] = (),
    ) -> AdvancedTarget:
        """Attach discovered framework extensions to an advanced target, if possible.

        ``prepend_extensions`` are Harnest's credential binders and must run
        before anything already attached; ``framework_extensions`` are
        discovered ``@lifecycle.framework_extension`` results. Most frameworks
        cannot change an object the author already built, so the default
        raises ``FrameworkConventionError`` when there is anything to attach.
        """

        if not framework_extensions and not prepend_extensions:
            return advanced
        raise FrameworkConventionError(
            f"advanced {self._metadata.display_name} applications cannot attach "
            "discovered framework extensions; apply them before Agent.advanced(...)"
        )

    def managed_checkpointer(self, provider: Any) -> Any | None:
        """Adapt Harnest's checkpoint store to the framework's checkpoint interface.

        ``provider`` is the ``HarnestStore`` returned by
        ``@lifecycle.storage.checkpoints``. Return an object the framework
        calls to save and load checkpoints (LangGraph: a checkpoint saver); it
        is passed to ``lower_agent``/``lower_graph`` and returned by
        ``store.framework_checkpointer()``. Return ``None`` (the default) when
        Harnest's runtime driver saves checkpoints itself.
        """

        return None

    def validate_advanced_checkpointer(
        self, advanced: AdvancedTarget, provider: Any
    ) -> None:
        """Check an advanced target uses the checkpoint store the lifecycle declared.

        ``provider`` is what ``@lifecycle.storage.checkpoints`` returned. Raise
        ``FrameworkConventionError`` if the target persists somewhere else, so
        there is only ever one checkpoint authority. The default accepts any.
        """

    def credential_extensions(
        self, provider: Any, framework_extensions: Sequence[Any]
    ) -> tuple[Any, ...]:
        """Return framework extensions that bind private credentials to native runs.

        ``provider`` is the agent's credential provider (``None`` when there is
        none). Returned extensions are placed before every other extension so
        native callbacks see the same credentials as tools. Return ``()`` (the
        default) when ``harnest.credentials`` already reaches all code paths.
        """

        return ()

    # -- Run ---------------------------------------------------------------
    # Called by the runtime (runtime.py) when a compiled artifact starts, and
    # while it serves requests.

    @abstractmethod
    def build_litellm_model(self, model: Any) -> Any:
        """Build the framework's LiteLLM model object for one ``LiteLLMModel``.

        Called when ``LiteLLMModel.build()`` runs: while lowering a managed
        agent, or in advanced code that builds a model itself. Read settings
        from ``model`` (model name, credentials, transport) and return the
        framework's own LiteLLM wrapper.
        """

    def host_sessions(
        self,
        artifact: Any,
        application: Any,
        *,
        framework_storage: Any | None,
        store: SessionStore | None,
    ) -> RuntimeSessions:
        """Resolve host-injected session storage into runtime session authorities.

        Called by ``create_fastapi_app`` each time a server starts, with the
        ``session_store`` and ``framework_session_storage`` the host passed
        (often both ``None``). ``framework_storage`` is a deployment-provided
        framework-specific storage factory (for example
        ``harnest.frameworks.adk.ADKSessionStorage``). The result is passed to
        ``create_runtime_driver`` or ``build_framework_server``. The default accepts only a portable
        ``store`` and rejects ``framework_storage`` for frameworks without
        :attr:`Capability.FRAMEWORK_SESSION_SERVICE`.
        """

        if framework_storage is not None:
            raise FrameworkConventionError(
                f"framework_session_storage cannot be used with a "
                f"{self._metadata.display_name} agent"
            )
        return RuntimeSessions(store=store)

    @abstractmethod
    def create_runtime_driver(
        self,
        application: Any,
        *,
        card: Mapping[str, Any] | None,
        extra_endpoints: Mapping[str, str] | None,
        sessions: RuntimeSessions,
        extension_manager: Any | None,
    ) -> Any:
        """Create the ``RuntimeDriver`` that executes one compiled application.

        This is the heart of an adapter at run time. Return an object that
        implements ``harnest.runtime_contract.RuntimeDriver`` (``info``,
        session methods, ``invoke``, ``stream``, ``close``) by running
        ``application.target`` or ``application.framework_app``. Harnest wraps
        it with plugins, lifecycle hooks, credentials, continuations, and
        tasks, and every transport (HTTP, SSE, WebSocket, A2A, AG-UI) calls it.

        ``application`` is the ``CompiledApplication``;
        ``application.runtime_capabilities`` holds the lifecycle-declared
        stores. ``card`` and ``extra_endpoints`` describe the agent for
        ``info()``. ``sessions`` comes from ``host_sessions`` (empty when the
        host injected nothing). ``extension_manager`` runs same-process Harnest
        Extensions and may be ``None``. Raise ``AgentRuntimeError`` when two
        session authorities conflict.
        """

    def observability_defaults(self) -> Mapping[str, str]:
        """Return environment defaults that keep framework telemetry privacy-safe.

        Applied before telemetry starts, without overriding values the user
        set. ADK turns off prompt and response capture in its spans. The
        default is empty.
        """

        return {}

    def exposes_framework_server(self, application: Any) -> bool:
        """Report whether the framework's own HTTP API should serve this application.

        Asked once at startup. When true, ``build_framework_server`` replaces
        Harnest's neutral server. The default is false.
        """

        return False

    def build_framework_server(self, artifact: Any, application: Any, **options: Any) -> Any:
        """Build the framework's own server; requires ``FRAMEWORK_API_SERVER``.

        Return ``(app, telemetry)``: an ASGI app that still applies Harnest's
        server options (limits, auth, transports) and the telemetry handle to
        close on shutdown. The default raises ``UnsupportedFrameworkFeature``.
        """

        raise self._unsupported("the framework's API server")

    # -- Lifecycle ---------------------------------------------------------
    # Called by the lifecycle and extension loaders while discovering
    # decorated factories in lifecycle/ and Harnest Extensions.

    @abstractmethod
    def framework_extension_type(self) -> type[Any]:
        """Return the type ``@lifecycle.framework_extension`` factories must return.

        The loader rejects anything else, naming ``framework_extension_label``.
        ADK: ``BasePlugin``; LangGraph: ``AgentMiddleware``.
        """

    def validate_framework_extensions(self, values: tuple[Any, ...]) -> None:
        """Reject a set of framework extensions the framework cannot install together.

        ``values`` are all discovered extensions, in order. ADK rejects
        duplicate plugin names here. The default accepts any set.
        """

    @abstractmethod
    def model_lifecycle_extension(self, pipeline: Any) -> Any:
        """Wrap portable model hooks as one framework extension.

        Called once at compile time when the agent declares
        ``@lifecycle.model.*`` hooks. ``pipeline`` is a
        ``ModelLifecyclePipeline`` with the before, after, and error stages.
        Return an extension (ADK plugin, LangChain middleware) that calls it
        around every model request; it is added to ``framework_extensions``.
        """

    def is_framework_code_executor(self, value: Any) -> bool:
        """Report whether a sandbox provider returned this framework's own code executor.

        Asked of every registered adapter, so it must not require the SDK; use
        ``sdk_isinstance``. The default is false.
        """

        return False

    def is_injected_tool_context(self, value: Any) -> bool:
        """Report whether the framework injected ``value`` as a tool's context argument.

        Approvals use it to leave framework-injected arguments (ADK
        ``ToolContext``, LangGraph ``ToolRuntime``) out of what a person
        approves. Asked of every registered adapter, so check the value's
        module before importing anything. The default is false.
        """

        return False

    def accepts_framework_session_store(self, value: Any) -> bool:
        """Report whether a lifecycle session factory may return a framework-specific store.

        Session factories normally return a ``SessionStore``. Frameworks with
        :attr:`Capability.FRAMEWORK_SESSION_SERVICE` may also accept their own
        store type here (ADK: ``ADKStore``). The default is false.
        """

        return False

    # -- Messages (token policy) -------------------------------------------
    # Called by harnest.tokens policies on messages already serialized to the
    # framework's own dictionary shape, before each model request.

    @abstractmethod
    def is_user_turn(self, message: dict[str, Any]) -> bool:
        """Report whether one serialized message starts a human turn.

        Token policies count turns with it, so tool results that the framework
        sends with a user role must return false.
        """

    @abstractmethod
    def is_instruction(self, message: dict[str, Any]) -> bool:
        """Report whether one serialized message is a system or developer instruction.

        Instructions are always kept when token policies drop older turns.
        """

    @abstractmethod
    def reduce_tool_result(self, message: dict[str, Any], limit: int) -> None:
        """Shorten the tool-result text in one serialized message, in place.

        Cut text to ``limit`` characters while keeping the identifiers the
        framework needs to match the result to its call, and leave non-text
        content (media, provider data) untouched.
        """

    # -- MCP ---------------------------------------------------------------

    @abstractmethod
    def mcp_tools(
        self,
        calls: Any,
        schemas: list[Any],
        *,
        server_name: str | None,
        interceptors: Any,
    ) -> list[Any]:
        """Turn discovered MCP tool schemas into the framework's tool objects.

        Used for MCP clients served over Harnest's own HTTP transport.
        ``schemas`` are MCP ``Tool`` definitions; ``calls`` executes a tool
        through Harnest's governed transport (``await calls.call_tool(name,
        arguments)``). ``server_name`` names the server for tool prefixes, and
        ``interceptors`` are Harnest's tool interceptors for frameworks whose
        MCP adapters accept them. Return one framework tool per schema.
        """

    # -- Durable tools -----------------------------------------------------
    # Called while a durable tool (harnest.durable) waits for external work.

    def is_suspension(self, error: BaseException) -> bool:
        """Recognize framework control-flow exceptions that hooks must not catch.

        Tool lifecycle hooks and approvals let an exception pass untouched when
        any registered adapter returns true, because it is a pause, not a
        failure (for example LangGraph's ``GraphInterrupt``).
        Check names and modules rather than importing the SDK. The default is
        false.
        """

        return False

    def suspend_durable(self, pending: Any) -> Any:
        """Enter the framework's persisted wait for one durable tool call.

        Called inside the tool when it must wait. Persist through the
        framework's own mechanism: LangGraph calls ``interrupt(pending.public())``;
        ADK raises ``FrameworkDurableSuspended`` so its long-running tool
        returns no value. Set ``resume_reenters_tool`` to match how the
        framework resumes. The default raises ``UnsupportedFrameworkFeature``.
        """

        raise self._unsupported("durable tool suspension")

    # -- Evaluations -------------------------------------------------------
    # Harnest's built-in evaluator is ADK's evaluation library and drives any
    # framework through an importable agent module.

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
        """Run ``harnest test --evals``; requires :attr:`Capability.EVALS`.

        Implement it by calling ``harnest.testing._run_eval_suite`` with an
        async context that yields the evaluator's module name and the target
        whose models it borrows; that function prints and publishes results
        and returns the exit code. The default raises
        ``UnsupportedFrameworkFeature``.
        """

        raise self._unsupported("built-in evaluations")

    def evaluation_runtime(self, application: Any, driver: Any = None) -> Any:
        """Return an async context that prepares runtime capabilities for evals.

        Playground evals enter it around a run. The default does nothing.
        """

        from contextlib import nullcontext

        return nullcontext()

    def playground_eval_agent_module(self, application: Any, driver: Any) -> Any:
        """Return the agent module playground evals run; requires ``EVALS``.

        ``driver`` is the playground's live runtime driver, if any. The
        default raises ``UnsupportedFrameworkFeature``.
        """

        raise self._unsupported("playground evaluations")


def sdk_isinstance(value: Any, module: str, name: str) -> bool:
    """Report whether ``value`` is a ``module.name`` instance; False without that SDK."""

    try:
        kind = getattr(import_module(module), name)
    except (ImportError, AttributeError):
        return False
    return isinstance(value, kind)


__all__ = [
    "AdvancedTarget",
    "AdvancedTargetError",
    "Capability",
    "DoctorModule",
    "FrameworkAdapter",
    "FrameworkConventionError",
    "FrameworkDependencyError",
    "FrameworkMetadata",
    "FrameworkName",
    "RuntimeSessions",
    "UnknownFrameworkError",
    "UnsupportedFrameworkFeature",
    "sdk_isinstance",
]

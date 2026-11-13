"""Framework adapter contract: every registered framework is complete, and a new
framework plugs into shared Harnest code without editing it."""

from _framework_testing import build_for_framework

import ast
import asyncio
from contextlib import contextmanager
from contextvars import copy_context
import tempfile
from dataclasses import replace
import importlib.util
import inspect
from pathlib import Path
import re
import tomllib
import unittest
from unittest.mock import patch
import warnings

from harnest.application import CompiledApplication
from harnest.frameworks import (
    Capability,
    FrameworkAdapter,
    FrameworkConventionError,
    RuntimeSessions,
    UnsupportedFrameworkFeature,
    framework_metadata,
    framework_names,
    get_framework,
    shared_runtime_requirements,
)
from harnest.frameworks import registry
from harnest.frameworks.context import current_framework, framework_scope
from harnest.bundle import compile_application
from harnest._library import release_authored_library
from harnest.model import LiteLLMModel
from harnest.runtime import _runtime_driver
from harnest.runtime_contract import AgentInfo, InvocationRequest, InvocationResult
from harnest.store_postgres_schema import SCHEMA_SQL
from harnest.token_reduction import reduce_request
from harnest.tokens import TokenPolicy, TokenRequest

_ROOT = Path(__file__).resolve().parents[2]

# Each capability promises callers an optional hook; declaring one without
# implementing its hooks would fail only when a user reaches that feature.
_CAPABILITY_HOOKS = {
    Capability.GRAPHS: ("lower_graph",),
    Capability.FRAMEWORK_API_SERVER: ("exposes_framework_server", "build_framework_server"),
    Capability.FRAMEWORK_SESSION_SERVICE: ("host_sessions", "accepts_framework_session_store"),
    Capability.EVALS: ("run_builtin_evals", "playground_eval_agent_module"),
}


def _overrides(adapter: FrameworkAdapter, hook: str) -> bool:
    """Report whether an adapter replaces the base implementation of one hook."""

    return getattr(type(adapter), hook) is not getattr(FrameworkAdapter, hook)


class RegisteredFrameworkContractTests(unittest.TestCase):
    def test_every_registered_framework_has_a_complete_adapter(self):
        for name in framework_names():
            with self.subTest(framework=name):
                adapter = get_framework(name)
                self.assertIsInstance(adapter, FrameworkAdapter)
                self.assertFalse(inspect.isabstract(type(adapter)))
                for capability in adapter.metadata.capabilities:
                    for hook in _CAPABILITY_HOOKS.get(capability, ()):
                        self.assertTrue(
                            _overrides(adapter, hook),
                            f"{name} declares {capability.value} but keeps base {hook}",
                        )

    def test_frameworks_without_a_capability_report_it_clearly(self):
        adapter = get_framework("langgraph")
        self.assertFalse(adapter.supports(Capability.FRAMEWORK_API_SERVER))
        with self.assertRaisesRegex(UnsupportedFrameworkFeature, "LangGraph does not support"):
            adapter.build_framework_server("artifact", object())

    def test_package_extras_match_framework_metadata(self):
        extras = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"][
            "optional-dependencies"
        ]
        for name, metadata in framework_metadata().items():
            with self.subTest(framework=name):
                expected = set(metadata.requirements) | set(shared_runtime_requirements())
                self.assertEqual(set(extras[metadata.install_extra]), expected)

    def test_go_cli_embeds_identical_metadata(self):
        canonical = _ROOT / "src/harnest/frameworks/frameworks.json"
        embedded = _ROOT / "internal/frameworks/frameworks.json"
        self.assertEqual(canonical.read_bytes(), embedded.read_bytes())

    def test_postgres_schema_accepts_exactly_the_registered_frameworks(self):
        # The durable store constrains framework names. Registering a framework
        # therefore needs a store schema migration, not just a new adapter.
        constraints = re.findall(r"CHECK \(framework IN \(([^)]*)\)\)", SCHEMA_SQL)
        self.assertTrue(constraints)
        for constraint in constraints:
            names = tuple(re.findall(r"'([^']+)'", constraint))
            self.assertEqual(names, framework_names())


class _EchoDriver:
    """A framework runtime with no SDK: it answers with the user's input."""

    def __init__(self, application):
        self.info = AgentInfo(
            id=application.name,
            name=application.name,
            description="echo fixture",
            card={},
            framework="echo",
            mode=application.mode,
        )
        self.closed = False

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            text=request.input,
            events=({"type": "message", "text": request.input},),
            result=request.input,
            session_id=request.session_id,
            metadata={**request.metadata, "model_adapter": LiteLLMModel("echo/model").build()[0]},
        )

    async def create_session(self, *, session_id, user_id, state):
        raise NotImplementedError

    async def get_session(self, *, session_id, user_id):
        return None

    async def list_sessions(self, *, user_id, after=None, limit=None):
        return ()

    async def get_session_messages(self, *, session_id, user_id, **options):
        return ()

    async def update_session(self, *, session_id, user_id, **changes):
        raise NotImplementedError

    async def delete_session(self, *, session_id, user_id):
        return False

    async def stream(self, request: InvocationRequest):
        result = await self.invoke(request)
        yield {"type": "message", "text": result.text}

    async def close(self) -> None:
        self.closed = True


class _EchoFramework(FrameworkAdapter):
    """The smallest adapter: only the required hooks, no optional capabilities."""

    framework_extension_label = "EchoExtension"

    def lower_agent(self, definition, *, framework_extensions, checkpointer):
        return definition

    def validate_advanced(self, value, *, fallback_name):
        raise NotImplementedError

    def build_litellm_model(self, model):
        return ("echo-model", model)

    def create_runtime_driver(
        self, application, *, card, extra_endpoints, sessions, extension_manager
    ):
        return _EchoDriver(application)

    def framework_extension_type(self):
        return object

    def model_lifecycle_extension(self, pipeline):
        return None

    def is_user_turn(self, message):
        return message.get("speaker") == "person"

    def is_instruction(self, message):
        return message.get("speaker") == "system"

    def reduce_tool_result(self, message, limit):
        if message.get("speaker") == "tool":
            message["text"] = message["text"][:limit]

    def mcp_tools(self, calls, schemas, *, server_name, interceptors):
        return list(schemas)


@contextmanager
def _registered_echo_framework():
    """Register the fixture the same way an in-tree framework is registered."""

    metadata = replace(
        framework_metadata()["langgraph"],
        name="echo",
        display_name="Echo",
        distribution="echo-framework",
        capabilities=frozenset(),
    )
    extended = {**framework_metadata(), "echo": metadata}
    adapters = {**registry._ADAPTERS, "echo": f"{__name__}:_EchoFramework"}
    registry.get_framework.cache_clear()
    try:
        with patch.object(registry, "framework_metadata", return_value=extended), patch.object(
            registry, "_ADAPTERS", adapters
        ):
            yield get_framework("echo")
    finally:
        registry.get_framework.cache_clear()


class NewFrameworkExtensibilityTests(unittest.TestCase):
    def test_new_framework_plugs_into_shared_code_without_edits(self):
        with _registered_echo_framework() as adapter:
            self.assertIsInstance(adapter, _EchoFramework)
            self.assertIn("echo", framework_names())

            connector = LiteLLMModel("echo/model")
            self.assertEqual(build_for_framework(connector, "echo"), ("echo-model", connector))

            request = TokenRequest(
                framework="echo",
                model="echo/model",
                messages=(
                    {"speaker": "system", "text": "rules"},
                    {"speaker": "person", "text": "first"},
                    {"speaker": "tool", "text": "a long tool result"},
                    {"speaker": "person", "text": "second"},
                ),
            )
            reduced = reduce_request(
                request, TokenPolicy(keep_recent_turns=1, max_tool_result_chars=4)
            )
            self.assertEqual(
                [message["text"] for message in reduced.messages], ["rules", "second"]
            )

            application = CompiledApplication(
                name="echo_agent", framework="echo", mode="managed", target=object()
            )
            runtime = _runtime_driver(application, sessions=RuntimeSessions())
            request = InvocationRequest(
                input="hello",
                user_id="alice",
                session_id="session",
                invocation_id="invocation",
                metadata={},
                state_delta={},
            )

            async def exercise():
                result = await runtime.invoke(request)
                await runtime.close()
                return result

            result = asyncio.run(exercise())
            self.assertEqual(result.text, "hello")
            self.assertEqual(result.metadata["model_adapter"], "echo-model")

    def test_optional_hooks_fail_with_a_named_feature(self):
        with _registered_echo_framework() as adapter:
            with self.assertRaisesRegex(UnsupportedFrameworkFeature, "Echo does not support managed graphs"):
                adapter.lower_graph(object(), framework_extensions=(), checkpointer=None)
            with self.assertRaisesRegex(UnsupportedFrameworkFeature, "built-in evaluations"):
                adapter.run_builtin_evals(
                    artifact=None,
                    application=None,
                    suite=None,
                    trajectory="none",
                    print_results=False,
                    result_output=None,
                )


_SRC = _ROOT / "src" / "harnest"
_SDK_ROOTS = ("google.adk", "google.genai", "langgraph", "langchain")

# The only shared modules allowed to import a framework SDK: Harnest's built-in
# eval engine, which is ADK's evaluation library and evaluates agents from every
# framework. It is not the ADK framework adapter. Any other SDK use belongs in
# an adapter package.
_SDK_IMPORT_DEBT = {
    "eval_errors.py": "built-in ADK evaluator",
    "eval_metrics.py": "built-in ADK evaluator",
    "eval_model_transport.py": "built-in ADK evaluator",
    "evaluation.py": "built-in ADK evaluator",
}


def _shared_modules():
    """Yield every Harnest module outside the framework adapter packages."""

    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if not relative.startswith(("frameworks/", "_playground/", "_studio/")):
            yield relative, ast.parse(path.read_text())


def _imported_modules(relative, tree):
    """Resolve absolute module names for every import in one shared module."""

    package = ["harnest", *relative.split("/")[:-1]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)


class OwnershipBoundaryTests(unittest.TestCase):
    def test_shared_imports_respect_adapter_ownership(self):
        """Check adapter internals and SDK debt in one shared-source inventory."""
        violations = []
        sdk_importers = set()
        for relative, tree in _shared_modules():
            for module in _imported_modules(relative, tree):
                parts = module.split(".")
                if parts[:2] == ["harnest", "frameworks"] and len(parts) > 2 and parts[2] in framework_names():
                    violations.append(f"{relative}: {module}")
                if module.startswith(_SDK_ROOTS):
                    sdk_importers.add(relative)
        self.assertEqual(violations, [], "go through get_framework(...) instead")
        self.assertEqual(sdk_importers, set(_SDK_IMPORT_DEBT), "update SDK debt when imports change")


class FrameworkSelectionTests(unittest.IsolatedAsyncioTestCase):
    """Authoring follows compiler selection without shared mutable defaults."""

    def test_nested_selection_restores_after_failure_and_revokes_copied_context(self):
        with framework_scope("adk"):
            with self.assertRaisesRegex(ValueError, "compile failed"):
                with framework_scope("langgraph"):
                    self.assertEqual(current_framework().name, "langgraph")
                    raise ValueError("compile failed")
            self.assertEqual(current_framework().name, "adk")
            copied = copy_context()
        with self.assertRaisesRegex(RuntimeError, "requires Harnest compilation"):
            copied.run(current_framework)
        with self.assertRaisesRegex(RuntimeError, "requires Harnest compilation"):
            LiteLLMModel("openai/test").build()

    async def test_concurrent_selections_are_task_local(self):
        ready = asyncio.Event()

        async def selected(name):
            with framework_scope(name):
                if name == "langgraph":
                    ready.set()
                await ready.wait()
                await asyncio.sleep(0)
                return current_framework().name

        self.assertEqual(
            await asyncio.gather(selected("adk"), selected("langgraph")),
            ["adk", "langgraph"],
        )

    def test_compiler_selects_models_before_importing_author_code(self):
        source = '''from harnest.agent import Agent
from harnest.model import LiteLLMModel, resolve_model
root_agent = Agent(name="root", instruction="Respond briefly.",
                   model=resolve_model(LiteLLMModel("openai/test")))
'''
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "agent.py").write_text(source)
                (root / "instructions.md").write_text("Respond briefly.")
                (root / "lifecycle").mkdir()
                (root / "lifecycle" / "storage.py").write_text(
                    "from harnest import lifecycle\nfrom harnest.store import MemoryStore\n"
                    "store = MemoryStore()\n"
                    "@lifecycle.storage.sessions\ndef sessions(): return store\n"
                    "@lifecycle.storage.checkpoints\ndef checkpoints(): return store\n"
                )
                try:
                    application = compile_application(
                        root, entrypoint="agent:root_agent", framework=framework
                    )
                    self.assertEqual(application.framework, framework)
                    self.assertIsNotNone(application.target)
                finally:
                    release_authored_library(root)
            with self.assertRaisesRegex(RuntimeError, "requires Harnest compilation"):
                current_framework()

    def test_compiler_failure_does_not_leave_a_framework_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text('raise RuntimeError("authored failure")')
            with self.assertRaises(Exception):
                compile_application(root, entrypoint="agent:root_agent", framework="langgraph")
        with self.assertRaisesRegex(RuntimeError, "requires Harnest compilation"):
            current_framework()



class GenerationConfigWarningTests(unittest.TestCase):
    """generate_content_config is ADK-only; other frameworks warn and ignore it."""

    @staticmethod
    def _warnings(root, framework):
        from harnest.bundle import _warn_unused_generation_config

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _warn_unused_generation_config(root, framework)
        return [str(item.message) for item in caught]

    @staticmethod
    def _graph():
        from harnest.agent import Agent
        from harnest.graph import START, Edge, Graph

        return Graph(
            name="flow",
            nodes={
                "draft": Agent(
                    name="drafter",
                    model="openai:test",
                    instruction="Draft.",
                    generate_content_config={"temperature": 0.2},
                ),
                "check": Agent(name="checker", model="openai:test", instruction="Check."),
            },
            edges=(Edge(START, "draft"), Edge("draft", "check")),
        )

    def test_frameworks_without_the_capability_warn_with_agent_names(self):
        from harnest.agent import Agent
        from harnest.graph import Edge

        graph = self._graph()
        # Direct and wrapped references to the same target produce one warning entry.
        graph = replace(
            graph, nodes={**graph.nodes, "alias": Agent.advanced(graph.nodes["draft"])},
            edges=(*graph.edges, Edge("check", "alias")),
        )
        self.assertEqual(
            self._warnings(graph, "langgraph"),
            ["LangGraph does not use generate_content_config; ignoring it on 'drafter'"],
        )
        with _registered_echo_framework():
            self.assertEqual(
                self._warnings(graph, "echo"),
                ["Echo does not use generate_content_config; ignoring it on 'drafter'"],
            )

    def test_adk_uses_the_config_so_nothing_is_reported(self):
        self.assertTrue(get_framework("adk").supports(Capability.GENERATE_CONTENT_CONFIG))
        self.assertEqual(self._warnings(self._graph(), "adk"), [])

    @unittest.skipUnless(importlib.util.find_spec("langchain"), "langchain is not installed")
    def test_artifact_warns_but_runtime_compilation_is_quiet(self):
        from harnest.bundle import compile_artifact

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "instructions.md").write_text("Write.")
            (source / "agent.py").write_text(
                'from harnest.agent import Agent\n'
                'from langchain_core.language_models.fake_chat_models import GenericFakeChatModel\n'
                'from langchain_core.messages import AIMessage\n'
                'root_agent = Agent(name="writer", instruction="Write.",\n'
                '    model=GenericFakeChatModel(messages=iter([AIMessage(content="hi")])),\n'
                '    generate_content_config={"temperature": 0.2})\n'
            )
            (source / "lifecycle").mkdir()
            (source / "lifecycle" / "storage.py").write_text(
                "from harnest import lifecycle\nfrom harnest.store import MemoryStore\n"
                "store = MemoryStore()\n"
                "@lifecycle.storage.sessions\ndef sessions(): return store\n"
                "@lifecycle.storage.checkpoints\ndef checkpoints(): return store\n"
            )
            artifact = Path(directory) / "compiled"
            try:
                with self.assertWarnsRegex(UserWarning, "ignoring it on 'writer'"):
                    manifest = compile_artifact(source, artifact, framework="langgraph")
                self.assertEqual(manifest["framework"]["name"], "langgraph")
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    application = compile_application(
                        artifact / "source", entrypoint="agent:root_agent", framework="langgraph"
                    )
                self.assertIsNotNone(application.target)
                self.assertFalse(any("generate_content_config" in str(w.message) for w in caught))
                # Promoting warnings to errors must still release compiler-owned extensions.
                with warnings.catch_warnings(), patch("harnest.bundle._release_compiled_extensions") as release:
                    warnings.filterwarnings("error", message=".*does not use generate_content_config")
                    with self.assertRaisesRegex(UserWarning, "generate_content_config"):
                        compile_artifact(source, artifact, framework="langgraph")
                release.assert_called_once()
            finally:
                release_authored_library(source)
                release_authored_library(artifact / "source")


class HostSessionTests(unittest.TestCase):
    def test_adk_accepts_a_portable_host_session_store(self):
        application = CompiledApplication(
            name="root", framework="adk", mode="managed", target=object()
        )
        store, service = object(), object()
        with patch(
            "harnest.frameworks.adk.sessions.create_adk_session_service", return_value=service
        ) as bridge:
            sessions = get_framework("adk").host_sessions(
                "artifact", application, framework_storage=None, store=store
            )
        bridge.assert_called_once_with(store)
        self.assertIs(sessions.framework_service, service)

    def test_portable_and_native_host_storage_cannot_be_combined(self):
        application = CompiledApplication(
            name="root", framework="adk", mode="managed", target=object()
        )
        with self.assertRaisesRegex(FrameworkConventionError, "cannot be combined"):
            get_framework("adk").host_sessions(
                "artifact", application, framework_storage=object(), store=object()
            )

    def test_frameworks_without_native_sessions_reject_framework_storage(self):
        application = CompiledApplication(
            name="root", framework="langgraph", mode="managed", target=object()
        )
        with self.assertRaisesRegex(
            FrameworkConventionError, "framework_session_storage cannot be used with a LangGraph agent"
        ):
            get_framework("langgraph").host_sessions(
                "artifact", application, framework_storage=object(), store=None
            )


if __name__ == "__main__":
    unittest.main()

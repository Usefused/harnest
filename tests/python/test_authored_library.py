import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from harnest._library import (
    LibraryConventionError,
    activate_authored_library,
    release_authored_library,
)
from harnest.bundle import (
    BundleConventionError,
    compile_application,
    compile_artifact,
)
from harnest.runtime import (
    AgentRuntimeError,
    create_fastapi_app,
    load_compiled_application,
    run_agent_message,
)
from harnest.testing import run_agent_tests
from _session_store_fixture import write_session_store


class AuthoredLibraryTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    @staticmethod
    def _backend() -> SimpleNamespace:
        return SimpleNamespace(
            lower_managed=lambda value, **_kwargs: value,
            wrap_managed=lambda target, native_extensions=(): None,
            validate_advanced=lambda value, fallback_name: SimpleNamespace(
                name=fallback_name,
                target=value.target,
                native_app=None,
            ),
        )

    def _write_agent(self, root: Path, *, lazy_import: bool = False) -> None:
        write_session_store(root)
        self._write(
            root / "agent.py",
            "from harnest.agent import Agent\n"
            "root_agent = Agent(name='root', model='test/model')\n",
        )
        self._write(root / "instructions.md", "Answer clearly.\n")
        eager_import = (
            "" if lazy_import else "from harnest.lib.storage.queries import normalize\n"
        )
        lazy_import_line = (
            "    from harnest.lib.storage.queries import normalize\n"
            if lazy_import
            else ""
        )
        body = (
            "from harnest.tool import tool\n"
            f"{eager_import}"
            "@tool\n"
            "def lookup(value):\n"
            "    \"\"\"Normalize a lookup value.\"\"\"\n"
            f"{lazy_import_line}"
            "    return normalize(value)\n"
        )
        self._write(root / "tools" / "lookup.py", body)
        self._write(
            root / "lib" / "storage" / "queries.py",
            "def normalize(value):\n"
            "    return str(value).strip().lower()\n",
        )

    def test_nested_namespace_modules_are_available_without_init_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            self._write(
                root / "lib" / "not_a_resource.py",
                "from harnest.tool import tool\n"
                "@tool\n"
                "def hidden(value):\n"
                "    return value\n",
            )
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                application = compile_application(
                    root, entrypoint="agent:root_agent"
                )

        self.assertEqual([tool.__name__ for tool in application.target.tools], ["lookup"])
        self.assertEqual(application.target.tools[0]("  VALUE "), "value")

    def test_root_initializer_can_export_shared_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from harnest.lib import normalize\n"
                "root_agent = Agent(\n"
                "    name=normalize(' ROOT '), model='test/model', instruction='Help.'\n"
                ")\n",
            )
            self._write(
                root / "lib" / "__init__.py",
                "from .formatting import normalize\n",
            )
            self._write(
                root / "lib" / "formatting.py",
                "def normalize(value):\n"
                "    return value.strip().lower()\n",
            )
            self._write(root / "instructions.md", "Help.\n")
            write_session_store(root)
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                application = compile_application(
                    root, entrypoint="agent:root_agent"
                )

        self.assertEqual(application.target.name, "root")

    def test_advanced_mode_can_import_root_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from harnest.lib.values import APP\n"
                "root_agent = Agent.advanced(APP)\n",
            )
            self._write(root / "lib" / "values.py", "APP = object()\n")
            write_session_store(root)
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                application = compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    mode="advanced",
                )

        self.assertIsNotNone(application.target)

    def test_compiled_artifact_keeps_lazy_library_imports_available(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "agent"
            artifact = workspace / "artifact"
            self._write_agent(root, lazy_import=True)
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                compile_artifact(root, artifact)
                application = load_compiled_application(artifact)
            try:
                self.assertEqual(application.target.tools[0](" LATE "), "late")
            finally:
                release_authored_library(artifact / "source")

    def test_compilation_retains_telemetry_factory_without_calling_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            self._write(
                root / "extensions" / "telemetry.py",
                "from harnest import lifecycle\n"
                "@lifecycle.telemetry_exporter\n"
                "def destination():\n"
                "    raise RuntimeError('must run only at runtime')\n",
            )
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                application = compile_application(
                    root, entrypoint="agent:root_agent", framework="langgraph"
                )

        self.assertEqual(len(application.telemetry_exporters), 1)
        self.assertEqual(
            application.telemetry_exporters[0].function_name, "destination"
        )

    def test_models_namespace_supplies_agent_contracts_in_compiled_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "agent"
            artifact = workspace / "artifact"
            write_session_store(root)
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "models" / "support.py",
                "from pydantic import BaseModel\n"
                "class SupportRequest(BaseModel):\n"
                "    question: str\n"
                "class SupportResponse(BaseModel):\n"
                "    answer: str\n",
            )
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from harnest.models.support import SupportRequest, SupportResponse\n"
                "root_agent = Agent(\n"
                "    name='root', model='test/model',\n"
                "    input_schema=SupportRequest, output_schema=SupportResponse,\n"
                ")\n",
            )
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                compile_artifact(root, artifact)
                application = load_compiled_application(artifact)
            try:
                self.assertEqual(application.input_schema.__name__, "SupportRequest")
                self.assertEqual(application.output_schema.__name__, "SupportResponse")
                self.assertEqual(
                    application.input_schema.model_validate(
                        {"question": "Status?"}
                    ).question,
                    "Status?",
                )
            finally:
                release_authored_library(artifact / "source")

    def test_models_namespace_supplies_graph_output_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "agent"
            artifact = workspace / "artifact"
            write_session_store(root)
            self._write(
                root / "models" / "support.py",
                "from typing import Any\n"
                "from harnest import FrameworkMetadata\n"
                "from pydantic import BaseModel\n"
                "class TurnMetadata(BaseModel):\n"
                "    adk: dict[str, Any] | None = None\n"
                "    langgraph: dict[str, Any] | None = None\n"
                "class SupportResponse(BaseModel):\n"
                "    answer: str\n"
                "    metadata: FrameworkMetadata[TurnMetadata]\n",
            )
            self._write(
                root / "agent.py",
                "from harnest.graph import START, Edge, Event, Graph\n"
                "from harnest.models.support import SupportResponse\n"
                "def answer(value):\n"
                "    return Event(output={'answer': value})\n"
                "root_agent = Graph(\n"
                "    name='root', nodes={'answer': answer},\n"
                "    edges=(Edge(START, 'answer'),),\n"
                "    output_schema=SupportResponse,\n"
                ")\n",
            )
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                compile_artifact(root, artifact)
                application = load_compiled_application(artifact)
            try:
                from harnest.structured import framework_metadata_field

                self.assertEqual(application.output_schema.__name__, "SupportResponse")
                self.assertEqual(
                    framework_metadata_field(application.output_schema), "metadata"
                )
            finally:
                release_authored_library(artifact / "source")

    def test_authored_unit_and_smoke_tests_can_import_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            test_source = (
                "from harnest.lib.storage.queries import normalize\n"
                "def test_library_is_importable(agent):\n"
                "    assert agent.name == 'root'\n"
                "    assert normalize(' TEST ') == 'test'\n"
            )
            self._write(root / "tests" / "unit" / "test_lib.py", test_source)
            self._write(root / "tests" / "smoke" / "test_smoke_lib.py", test_source)
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                exit_code = run_agent_tests(root, include_smoke=True)

        self.assertEqual(exit_code, 0)

    def test_authored_unit_lane_does_not_start_mcp_client_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            self._write(
                root / "lib" / "mcp_gateway.py",
                "from harnest.mcp import MCPClientLifecycle\n"
                "starts = 0\n"
                "class GatewayLifecycle(MCPClientLifecycle):\n"
                "    def start(self, context):\n"
                "        global starts\n"
                "        starts += 1\n"
                "        raise AssertionError('unit tests started MCP lifecycle')\n",
            )
            self._write(
                root / "mcp" / "catalog.py",
                "from harnest.lib.mcp_gateway import GatewayLifecycle\n"
                "from harnest.mcp import MCPClient\n"
                "def client():\n"
                "    return MCPClient.streamable_http(\n"
                "        'https://offline.invalid/mcp',\n"
                "        lifecycle=GatewayLifecycle(),\n"
                "    )\n",
            )
            self._write(
                root / "tests" / "unit" / "test_mcp_gateway.py",
                "from harnest.lib import mcp_gateway\n"
                "def test_lifecycle_remains_lazy(agent):\n"
                "    assert agent.name == 'root'\n"
                "    assert mcp_gateway.starts == 0\n",
            )
            with (
                patch("harnest.bundle.get_backend", return_value=self._backend()),
                patch("harnest.testing.create_fastapi_app") as create_app,
            ):
                exit_code = run_agent_tests(root)

        self.assertEqual(exit_code, 0)
        create_app.assert_not_called()

    def test_smoke_tests_share_one_compiled_server_lifecycle(self):
        lifecycle = {"started": 0, "closed": 0}

        @asynccontextmanager
        async def lifespan(_app):
            lifecycle["started"] += 1
            yield
            lifecycle["closed"] += 1

        app = FastAPI(lifespan=lifespan)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            self._write(
                root / "tests/unit/test_shared_smoke_lifecycle_unit.py",
                "def test_agent(agent):\n    assert agent.name == 'root'\n",
            )
            first_smoke_test = (
                "def test_server(client):\n"
                "    assert client.get('/health').json() == {'status': 'ok'}\n"
                "    client.headers['x-test-user'] = 'first'\n"
                "    client.cookies.set('test-session', 'first')\n"
            )
            second_smoke_test = (
                "def test_server(client):\n"
                "    assert client.get('/health').json() == {'status': 'ok'}\n"
                "    assert 'x-test-user' not in client.headers\n"
                "    assert client.cookies.get('test-session') is None\n"
            )
            self._write(
                root / "tests/smoke/test_shared_smoke_lifecycle_first.py",
                first_smoke_test,
            )
            self._write(
                root / "tests/smoke/test_shared_smoke_lifecycle_second.py",
                second_smoke_test,
            )
            with (
                patch("harnest.bundle.get_backend", return_value=self._backend()),
                patch("harnest.testing.create_fastapi_app", return_value=app),
            ):
                exit_code = run_agent_tests(root, include_smoke=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(lifecycle, {"started": 1, "closed": 1})

    def test_unit_client_fixture_rejection_does_not_start_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            self._write(
                root / "tests/unit/test_invalid_client.py",
                "def test_client_is_smoke_only(client):\n"
                "    raise AssertionError('the fixture should reject first')\n"
                "def test_smoke_is_smoke_only(smoke):\n"
                "    raise AssertionError('the fixture should reject first')\n",
            )
            with (
                patch("harnest.bundle.get_backend", return_value=self._backend()),
                patch("harnest.testing.create_fastapi_app") as create_app,
            ):
                exit_code = run_agent_tests(root)

        self.assertNotEqual(exit_code, 0)
        create_app.assert_not_called()

    def test_smoke_tests_keep_shared_memory_store_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write(
                root / "agent.py",
                "from harnest.graph import START, Edge, Event, Graph\n"
                "def answer(value):\n"
                "    return Event(output=f'{value}:ok', message=f'{value}:ok')\n"
                "root_agent = Graph(\n"
                "    name='shared_store', nodes={'answer': answer},\n"
                "    edges=(Edge(START, 'answer'),),\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Respond deterministically.\n")
            self._write(
                root / "agent-card.yaml",
                "name: Shared store\n"
                "description: Verifies smoke-test storage ownership.\n"
                "version: 0.1.0\n",
            )
            self._write(
                root / "lib/storage.py",
                "from harnest.store import MemoryStore\n"
                "store = MemoryStore()\n",
            )
            self._write(
                root / "extensions/storage.py",
                "from harnest.lib.storage import store\n"
                "from harnest.lifecycle import lifecycle\n"
                "@lifecycle.session_store\n"
                "def sessions(): return store\n"
                "@lifecycle.checkpointer\n"
                "def checkpoints(): return store\n",
            )
            self._write(
                root / "tests/unit/test_shared_memory_store_agent.py",
                "def test_agent(agent):\n"
                "    assert agent.name == 'shared_store'\n",
            )
            for name, value in (("first", "one"), ("second", "two")):
                self._write(
                    root / f"tests/smoke/test_{name}.py",
                    "def test_response(smoke):\n"
                    f"    result = smoke.respond({value!r})\n"
                    f"    assert result['outputText'] == {f'{value}:ok'!r}\n",
                )

            exit_code = run_agent_tests(root, include_smoke=True)

        self.assertEqual(exit_code, 0)

    def test_nested_subagents_cannot_define_their_own_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            nested = root / "subagents" / "helper"
            self._write(
                nested / "agent.py",
                "from harnest.agent import Agent\n"
                "helper = Agent(name='helper', model='test/model')\n",
            )
            self._write(nested / "instructions.md", "Help.\n")
            self._write(nested / "lib" / "private.py", "VALUE = 1\n")
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(
                    BundleConventionError, "authored libraries are root-only"
                ):
                    compile_application(root, entrypoint="agent:root_agent")

    def test_nested_subagents_cannot_define_their_own_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            nested = root / "subagents" / "helper"
            self._write(
                nested / "agent.py",
                "from harnest.agent import Agent\n"
                "helper = Agent(name='helper', model='test/model')\n",
            )
            self._write(nested / "instructions.md", "Help.\n")
            self._write(nested / "models" / "private.py", "VALUE = 1\n")
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(
                    BundleConventionError, "authored models are root-only"
                ):
                    compile_application(root, entrypoint="agent:root_agent")

    def test_models_namespace_rejects_invalid_module_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            self._write_agent(root)
            self._write(root / "models" / "bad-name.py", "VALUE = 1\n")
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(
                    BundleConventionError,
                    "authored models module names must be valid Python identifiers",
                ):
                    compile_application(root, entrypoint="agent:root_agent")

    def test_models_namespace_cannot_mix_two_agent_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = workspace / "first"
            second = workspace / "second"
            self._write(first / "models" / "contract.py", "VALUE = 1\n")
            self._write(second / "models" / "contract.py", "VALUE = 2\n")
            self.assertTrue(activate_authored_library(first))
            try:
                with self.assertRaisesRegex(
                    LibraryConventionError, "harnest.models is already bound"
                ):
                    activate_authored_library(second)
            finally:
                release_authored_library(first)

    def test_library_rejects_symlinks_invalid_names_and_import_collisions(self):
        cases = {
            "symlink": "authored lib cannot contain symlinks",
            "invalid": "valid Python identifiers",
            "collision": "authored lib import collision",
        }
        for case, expected in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "agent"
                self._write_agent(root)
                if case == "symlink":
                    (root / "lib" / "linked.py").symlink_to(
                        root / "lib" / "storage" / "queries.py"
                    )
                elif case == "invalid":
                    self._write(root / "lib" / "bad-name.py", "VALUE = 1\n")
                else:
                    self._write(root / "lib" / "storage.py", "VALUE = 1\n")
                with patch("harnest.bundle.get_backend", return_value=self._backend()):
                    with self.assertRaisesRegex(BundleConventionError, expected):
                        compile_application(root, entrypoint="agent:root_agent")

    def test_runtime_rejects_binding_two_agent_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            artifacts = []
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                for name in ("first", "second"):
                    root = workspace / name
                    artifact = workspace / f"{name}-artifact"
                    self._write_agent(root)
                    compile_artifact(root, artifact)
                    artifacts.append(artifact)
                load_compiled_application(artifacts[0])
                try:
                    with self.assertRaisesRegex(
                        AgentRuntimeError, "harnest.lib is already bound"
                    ):
                        load_compiled_application(artifacts[1])
                finally:
                    release_authored_library(artifacts[0] / "source")

    def test_direct_execution_failure_releases_library_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = self._compile_named_artifact(workspace, "first")
            second = self._compile_named_artifact(workspace, "second")
            with patch(
                "harnest.runtime._apply_observability_defaults",
                side_effect=RuntimeError("observability failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "observability failed"):
                    asyncio.run(run_agent_message(first, "hello"))
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                load_compiled_application(second)
            release_authored_library(second / "source")

    def test_server_construction_failure_releases_library_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = self._compile_named_artifact(workspace, "first")
            second = self._compile_named_artifact(workspace, "second")
            with self.assertRaisesRegex(AgentRuntimeError, "agent-card.yaml"):
                create_fastapi_app(first)
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                load_compiled_application(second)
            release_authored_library(second / "source")

    def _compile_named_artifact(self, workspace: Path, name: str) -> Path:
        root = workspace / name
        artifact = workspace / f"{name}-artifact"
        self._write_agent(root)
        with patch("harnest.bundle.get_backend", return_value=self._backend()):
            compile_artifact(root, artifact)
        return artifact


if __name__ == "__main__":
    unittest.main()

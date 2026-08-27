import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from harnest._library import release_authored_library
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

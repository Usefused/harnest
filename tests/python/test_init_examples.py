"""Exercise generated CLI samples through the real managed compiler boundary."""

import ast
import asyncio
import json
import os
import runpy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from harnest.bundle import compile_application, discover_evals
from harnest.extensions import release_extensions


_ROOT = Path(__file__).resolve().parents[2]
_DOCKER_EXTENSION = _ROOT / "official-extensions" / "docker"


class InitExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Build the CLI once so tests consume actual generated files."""
        cls.workspace = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.workspace.cleanup)
        cls.binary = Path(cls.workspace.name) / "harnest"
        environment = dict(os.environ, GOCACHE=str(_ROOT / ".cache" / "go-build"))
        subprocess.run(
            ["go", "build", "-o", str(cls.binary), "./cmd/harnest"],
            cwd=_ROOT, env=environment, check=True, capture_output=True, timeout=120,
        )

    def _scaffold(self, framework):
        """Create a fresh project with only the explicit example flag enabled."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "sample-agent"
        subprocess.run(
            [str(self.binary), "init", str(root), "--framework", framework, "--example"],
            check=True, capture_output=True, timeout=30,
        )
        return root

    def _compile_scaffold(self, root, framework):
        """Apply generated non-secret settings as the CLI does before compiling."""
        config = yaml.safe_load((root / "config.yaml").read_text())
        environment = {key: str(value) for key, value in config["spec"]["environment"].items()}
        with patch.dict(os.environ, environment):
            return compile_application(root, entrypoint="agent:root_agent", framework=framework)

    def test_samples_are_valid_source_but_never_imported_by_default(self):
        """Ignored examples cannot activate tools, tasks, extensions, or evals."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                root = self._scaffold(framework)
                self._validate_and_poison_samples(root)
                application = self._compile_scaffold(root, framework)
                self.assertEqual(application.kind, "agent")
                self.assertEqual(application.tasks, ())
                self.assertEqual(application.crons, ())
                self.assertEqual(application.extensions, ())
                self.assertEqual(discover_evals(root / "agent.py").eval_sets, ())

    def _validate_and_poison_samples(self, root):
        """Check every Python template, then make accidental imports fail loudly."""
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
            # Native-format extension directories are ignored as a whole, too.
            if any(part.startswith("_") for part in path.relative_to(root).parts):
                path.write_text(source + '\nraise AssertionError("ignored sample imported")\n', encoding="utf-8")
        sample = json.loads((root / "evals" / "_example.evalset.json").read_text())
        self.assertEqual(sample["eval_set_id"], "starter")

    def test_renamed_samples_satisfy_the_managed_contract(self):
        """Activating sample paths produces real capabilities on both backends."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                root = self._scaffold(framework)
                self._activate_samples(root)
                self._install_docker_extension(root)
                application = self._compile_scaffold(root, framework)
                try:
                    self._assert_activated_samples(root, application)
                finally:
                    # Release before the next subtest compiles another project
                    # with the same process-owned extension namespace.
                    release_extensions(tuple(extension.descriptor for extension in application.extensions))

    def _assert_activated_samples(self, root, application):
        """Check actual task linkage and runtime-extension/eval discovery."""
        self.assertEqual(len(application.extensions), 2)
        self.assertEqual(len(application.tasks), 1)
        self.assertEqual(len(application.crons), 1)
        self.assertIs(application.crons[0].task, application.tasks[0])
        self.assertEqual(
            asyncio.run(application.tasks[0].authored(subject="daily")),
            {"subject": "daily", "status": "ready"},
        )
        self.assertEqual(len(discover_evals(root / "agent.py").eval_sets), 1)

    def _activate_samples(self, root):
        """Use the same filenames documented by the generated template headers."""
        paths = {
            "tools/_example.py": "tools/echo.py",
            "tasks/_example.py": "tasks/prepare_report.py",
            "cron/_example.py": "cron/daily_report.py",
            "extensions/_example": "extensions/starter_runtime",
            "skills/_example": "skills/getting-started",
            "evals/_example.evalset.json": "evals/starter.evalset.json",
            "sandbox/_example.py": "sandbox/calculations.py",
        }
        for source, destination in paths.items():
            (root / source).rename(root / destination)
        # Renaming registers a definition; an explicit user grant enables it.
        agent = root / "agent.py"
        agent.write_text(
            agent.read_text(encoding="utf-8")
            + "\nfrom dataclasses import replace\n"
            + "root_agent = replace(root_agent, sandboxes=['calculations'])\n",
            encoding="utf-8",
        )

    def _install_docker_extension(self, root):
        """Stage the optional provider exactly where the install command materializes it."""
        installed = root / "extensions" / "docker"
        installed.mkdir(parents=True)
        for name in ("extension.py", "extension.yaml", "pyproject.toml"):
            shutil.copy2(_DOCKER_EXTENSION / name, installed / name)
        shutil.copytree(_DOCKER_EXTENSION / "lib", installed / "lib")


class ExampleContractTests(unittest.TestCase):
    """Keep checked-in agent examples on the current project contracts."""

    def test_mcp_example_uses_explicit_compatible_model_configuration(self):
        """Import the committed MCP example and build its real model adapter offline."""
        root = _ROOT / "mcp-agent"
        config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        environment = {key: str(value) for key, value in config["spec"]["environment"].items()}
        environment.update(
            OPENAI_MODEL="team/chosen", OPENAI_BASE_URL="https://models.example.invalid/v1",
            HARNEST_MCP_TOKEN="synthetic-test-token",
        )
        with patch.dict(os.environ, environment, clear=True):
            definition = runpy.run_path(str(root / "agent.py"))["root_agent"]
            self.assertEqual(definition.model.model, "openai/team/chosen")
            self.assertEqual(definition.model.completion_args["api_base"], environment["OPENAI_BASE_URL"])
            # Exercise the connector independently of the example's exact framework pin.
            adapter = definition.model.build_for("adk")
            self.assertEqual(adapter.model, "openai/team/chosen")
        for name in ("agent.py", "config.yaml", "README.md"):
            with self.subTest(file=name):
                self.assertNotIn("ollama", (root / name).read_text(encoding="utf-8").lower())

    def test_managed_examples_pin_their_selected_framework(self):
        """Prevent examples from silently resolving a different framework release."""
        examples = _ROOT / "examples"
        configs = sorted(examples.rglob("config.yaml"))
        self.assertGreater(len(configs), 0)
        for config_path in configs:
            with self.subTest(example=config_path.parent.relative_to(examples)):
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                lock_path = config_path.with_name("harnest.lock")
                self.assertTrue(lock_path.is_file())
                lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(lock["projectSchema"], 6)
                self.assertEqual(
                    lock["framework"]["name"], config["spec"]["framework"]["name"]
                )
                self.assertRegex(lock["framework"]["version"], r"^\d+\.\d+\.\d+$")

    def test_examples_author_server_overrides_in_project_config(self):
        """Keep examples on the single-file server configuration introduced in 0.12."""
        self.assertEqual(list((_ROOT / "examples").rglob("server.yaml")), [])

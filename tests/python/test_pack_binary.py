"""Tests for standalone pack binary manifests, payload staging, and pack-owned extensions."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from zipfile import ZipFile

from harnest.authoring.binary_payload import BINARY_FORMAT, BINARY_MANIFEST, prepare_pack_binary
from harnest.authoring.contracts import ProjectError
from harnest.authoring.pack_cli import run_pack_command
from harnest.authoring.packs import load_pack
from harnest.bundle import BundleConventionError, compile_artifact
from harnest.extension_descriptors import discover_application_extensions
from harnest.extensions import release_extensions
from harnest.runtime import load_compiled_application


def _write_minimal_wheel(path: Path, name: str, version: str = "1.0.0") -> None:
    """Create a minimal valid pure-Python wheel archive for offline testing."""
    dist = name.replace("-", "_")
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{dist}/__init__.py", f'VALUE = "{name}:{version}"\n')
        archive.writestr(
            f"{dist}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist}-{version}.dist-info/RECORD", "")


def _write_local_package(root: Path, name: str) -> None:
    """Create a small hatchling-free/standard local package buildable into a wheel."""
    pkg = name.replace("-", "_")
    (root / pkg).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling>=1.25"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = []\n\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["{pkg}"]\n',
        encoding="utf-8",
    )
    (root / pkg / "__init__.py").write_text('GREETING = "from-local-package"\n', encoding="utf-8")


def _write_pack_extension(root: Path, name: str) -> None:
    """Create a canonical Harnest Extension with a lifecycle output_policy factory."""
    (root / "lifecycle").mkdir(parents=True, exist_ok=True)
    (root / "extension.yaml").write_text(
        "apiVersion: harnest.dev/v1alpha1\n"
        "kind: Extension\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  version: 1.0.0\n"
        "runtime:\n"
        "  entrypoint: extension:extension\n"
        "capabilities:\n"
        "  - policy.output\n"
        "contributes:\n"
        "  lifecycle:\n"
        "    - lifecycle\n",
        encoding="utf-8",
    )
    (root / "extension.py").write_text(
        "from harnest.extensions import Extension\n\n"
        "class PackGuardExtension(Extension):\n"
        "    \"\"\"Company guard extension.\"\"\"\n\n"
        f"extension = PackGuardExtension(name={name!r})\n",
        encoding="utf-8",
    )
    (root / "lifecycle" / "policy.py").write_text(
        "from harnest import lifecycle\n"
        "from harnest.output import AgentMetadataMode, OutputPolicy\n\n"
        "@lifecycle.output_policy\n"
        "def company_output_policy() -> OutputPolicy:\n"
        "    return OutputPolicy(\n"
        "        tool_activity=False,\n"
        "        agent_metadata=AgentMetadataMode.SUPPRESS,\n"
        "    )\n",
        encoding="utf-8",
    )


def _write_minimal_agent(root: Path) -> None:
    """Create a minimal managed Harnest agent with no extensions/ folder."""
    (root / "lifecycle").mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        "apiVersion: harnest.dev/v1alpha1\n"
        "kind: Agent\n"
        "metadata:\n"
        "  name: demo-agent\n"
        "spec:\n"
        "  framework:\n"
        "    name: adk\n"
        "    mode: managed\n"
        "  entrypoint: agent:root_agent\n"
        "  runtime:\n"
        "    version: '3.14'\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo-agent'\nversion = '0.1.0'\ndependencies = []\n",
        encoding="utf-8",
    )
    (root / "instructions.md").write_text("You are a helpful test agent.\n", encoding="utf-8")
    (root / "agent.py").write_text(
        "from harnest.agent import Agent\n\n"
        "root_agent = Agent(name='demo_agent', model='openai/gpt-4o-mini')\n",
        encoding="utf-8",
    )
    (root / "lifecycle" / "storage.py").write_text(
        "from harnest import lifecycle\n"
        "from harnest.store import MemoryStore\n\n"
        "store = MemoryStore()\n\n"
        "@lifecycle.storage.sessions\n"
        "def sessions() -> MemoryStore:\n"
        "    return store\n\n"
        "@lifecycle.storage.checkpoints\n"
        "def checkpoints() -> MemoryStore:\n"
        "    return store\n",
        encoding="utf-8",
    )


class PackBinaryTests(unittest.TestCase):
    """Exercise pack binary validation, wheel/skill/extension staging, and compiler auto-loading."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="harnest-pack-binary-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _create_full_pack(self, *, include_local_pkg: bool = True) -> Path:
        """Create a pack declaring coding-IDE skills, wheels, requirements, and an extension."""
        pack_dir = self.root / "acme-pack"
        pack_dir.mkdir()
        skill_dir = pack_dir / "coding-skills" / "acme-standards"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: acme-standards\ndescription: Acme agent coding standards.\n---\n# Acme\n",
            encoding="utf-8",
        )
        _write_minimal_wheel(pack_dir / "wheels" / "acme_prebuilt-1.0.0-py3-none-any.whl", "acme-prebuilt")
        packages_yaml = "  - wheels/acme_prebuilt-1.0.0-py3-none-any.whl\n"
        if include_local_pkg:
            _write_local_package(pack_dir / "local-pkg", "acme-local")
            packages_yaml += "  - local-pkg\n"
        packages_yaml += "  - acme-sdk==2.1.0\n"
        _write_pack_extension(pack_dir / "company-ext" / "acme_guard", "acme_guard")
        (pack_dir / "pack.yaml").write_text(
            "apiVersion: harnest.dev/pack/v1\n"
            "name: acme\n"
            "version: 1.0.0\n"
            "title: Acme agents\n"
            "command: acme\n"
            "skills:\n"
            "  - coding-skills/acme-standards\n"
            "packages:\n"
            f"{packages_yaml}"
            "extensions:\n"
            "  - company-ext/acme_guard\n",
            encoding="utf-8",
        )
        return pack_dir

    def test_prepare_pack_binary_stages_all_capabilities(self) -> None:
        """prepare_pack_binary validates and stages skills, prebuilt+local wheels, requirements, and extensions."""
        pack_dir = self._create_full_pack(include_local_pkg=True)
        staging = self.root / "staged"
        manifest = prepare_pack_binary(pack_dir, staging)
        self.assertEqual(manifest["format"], BINARY_FORMAT)
        self.assertEqual(manifest["command"], "acme")
        self.assertEqual([s["name"] for s in manifest["skills"]], ["acme-standards"])
        self.assertEqual([w["name"] for w in manifest["wheels"]], ["acme-prebuilt", "acme-local"])
        self.assertEqual(manifest["requirements"], ["acme-sdk==2.1.0"])
        self.assertEqual(manifest["protectedPackages"], ["acme-local", "acme-prebuilt", "acme-sdk"])
        self.assertEqual([e["name"] for e in manifest["extensions"]], ["acme_guard"])
        self.assertTrue((staging / BINARY_MANIFEST).is_file())
        for wheel in manifest["wheels"]:
            wheel_file = staging / wheel["filename"]
            self.assertTrue(wheel_file.is_file())
            self.assertEqual(hashlib.sha256(wheel_file.read_bytes()).hexdigest(), wheel["sha256"])

    def test_pack_manifest_rejects_compiler_owned_and_unsafe_entries(self) -> None:
        """pack.yaml blocks overriding Harnest/framework distributions and escaping pack paths."""
        bad_req = self.root / "bad-req"
        bad_req.mkdir()
        (bad_req / "pack.yaml").write_text(
            "apiVersion: harnest.dev/pack/v1\n"
            "name: acme\nversion: 1.0.0\ntitle: Acme\n"
            "packages:\n  - harnest==1.0.0\n",
            encoding="utf-8",
        )
        with self.assertRaises(ProjectError):
            load_pack(bad_req)

        # Reject the removed packages.wheels/requirements mapping with a clear error message.
        legacy_map = self.root / "legacy-map"
        legacy_map.mkdir()
        (legacy_map / "pack.yaml").write_text(
            "apiVersion: harnest.dev/pack/v1\n"
            "name: acme\nversion: 1.0.0\ntitle: Acme\n"
            "packages:\n  requirements:\n    - acme-sdk==2.1.0\n",
            encoding="utf-8",
        )
        with self.assertRaises(ProjectError):
            load_pack(legacy_map)

        bad_skill = self.root / "bad-skill"
        (bad_skill / "skills" / "wrong-name").mkdir(parents=True)
        (bad_skill / "skills" / "wrong-name" / "SKILL.md").write_text(
            "---\nname: other-name\ndescription: mismatch\n---\n# Bad\n",
            encoding="utf-8",
        )
        (bad_skill / "pack.yaml").write_text(
            "apiVersion: harnest.dev/pack/v1\n"
            "name: acme\nversion: 1.0.0\ntitle: Acme\n"
            "skills:\n  - skills/wrong-name\n",
            encoding="utf-8",
        )
        with self.assertRaises(ProjectError):
            prepare_pack_binary(bad_skill, self.root / "bad-stage")

    def test_validate_reports_pack_binary_capabilities(self) -> None:
        """`harnest pack validate` validates and summarizes skills, packages, and extensions."""
        pack_dir = self._create_full_pack(include_local_pkg=False)
        from io import StringIO

        stdout, stderr = StringIO(), StringIO()
        code = run_pack_command(["validate", str(pack_dir)], stdout=stdout, stderr=stderr)
        self.assertEqual(code, 0, stderr.getvalue())
        output = stdout.getvalue()
        self.assertIn("skills: 1", output)
        self.assertIn("packages: 2", output)
        self.assertIn("extensions: 1", output)

    def test_pack_extensions_autoload_into_compiled_artifact_without_copying_to_agent_repo(self) -> None:
        """Pack-owned extensions compile into the artifact and run without touching the agent repo."""
        pack_dir = self._create_full_pack(include_local_pkg=False)
        staging = self.root / "staged"
        prepare_pack_binary(pack_dir, staging)
        agent_dir = self.root / "demo-agent"
        _write_minimal_agent(agent_dir)
        artifact_dir = self.root / "compiled"

        previous = os.environ.get("HARNEST_PACK_EXTENSIONS")
        os.environ["HARNEST_PACK_EXTENSIONS"] = str(staging / "extensions")
        try:
            manifest = compile_artifact(agent_dir, artifact_dir, framework="adk", mode="managed")
            # The user's authored agent repo must never receive copied pack extension files.
            self.assertFalse((agent_dir / "extensions").exists())
            # The compiled artifact must include the pack extension and record it in the manifest.
            self.assertEqual([item["name"] for item in manifest["extensions"]], ["acme_guard"])
            self.assertTrue((artifact_dir / "source" / "extensions" / "acme_guard" / "extension.yaml").is_file())
            # Loading the compiled artifact with HARNEST_PACK_EXTENSIONS still set must not double-load.
            loaded = load_compiled_application(artifact_dir)
            try:
                self.assertFalse(loaded.output_policy.tool_activity)
            finally:
                # Release the activated pack extension namespace before compiling
                # another agent in this test or subsequent tests in the same process.
                release_extensions(discover_application_extensions(artifact_dir / "source"))
            # Shadowing a pack-owned extension inside the agent repo must fail compilation.
            _write_pack_extension(agent_dir / "extensions" / "acme_guard", "acme_guard")
            with self.assertRaises(BundleConventionError):
                compile_artifact(agent_dir, self.root / "compiled-conflict", framework="adk", mode="managed")
        finally:
            if previous is None:
                os.environ.pop("HARNEST_PACK_EXTENSIONS", None)
            else:
                os.environ["HARNEST_PACK_EXTENSIONS"] = previous

    def test_branded_cli_name_prefixes_errors_and_sanitizes_env(self) -> None:
        """HARNEST_CLI_NAME rebrands Python CLI/pack errors and wheel launchers while rejecting unsafe values."""
        from io import StringIO
        from unittest.mock import patch
        from harnest.authoring.distribution import _launcher
        from harnest.cli import cli_name, main as cli_main

        # Valid kebab-case HARNEST_CLI_NAME must prefix pack_cli and harnest.cli errors.
        with patch.dict(os.environ, {"HARNEST_CLI_NAME": "acme-cli"}):
            self.assertEqual(cli_name(), "acme-cli")
            stderr = StringIO()
            code = run_pack_command(["validate", str(self.root / "missing-pack")], stdout=StringIO(), stderr=stderr)
            self.assertEqual(code, 1)
            self.assertTrue(stderr.getvalue().startswith("acme-cli: "), stderr.getvalue())

            run_err = StringIO()
            run_code = run_pack_command(["run", str(self.root / "missing-pack")], stdout=StringIO(), stderr=run_err)
            self.assertEqual(run_code, 2)
            self.assertIn("usage: acme-cli pack run", run_err.getvalue())

            cli_err = StringIO()
            with patch("sys.stderr", cli_err):
                cli_code = cli_main(["compile", str(self.root / "missing-agent"), "--output", str(self.root / "out")])
            self.assertEqual(cli_code, 2)
            self.assertTrue(cli_err.getvalue().startswith("acme-cli: "), cli_err.getvalue())

        # Unsafe or malformed HARNEST_CLI_NAME values must fall back to "harnest".
        with patch.dict(os.environ, {"HARNEST_CLI_NAME": "bad\nname"}):
            self.assertEqual(cli_name(), "harnest")

        # Generated wheel launchers must pass HARNEST_CLI_NAME when invoking harnest.
        pack = load_pack(self._create_full_pack(include_local_pkg=False))
        launcher_source = _launcher(pack, ())
        self.assertIn('"HARNEST_CLI_NAME": NAME', launcher_source)

    def test_pack_finish_omits_backup_line_for_init_and_keeps_it_for_upgrade(self) -> None:
        """Pack init prints Initialized without a confusing Backup path; upgrade keeps Applied. Backup."""
        from io import StringIO
        from unittest.mock import patch
        from harnest.authoring.contracts import ChangeKind, ProjectChange, ProjectPlan
        from harnest.authoring.pack_cli import _finish

        target = self.root / "new-agent"
        plan = ProjectPlan(
            root=target,
            changes=(ProjectChange(ChangeKind.CREATE, "config.yaml", ("harnest",)),),
            blockers=(),
            _before={},
            _after={},
            _root_existed=False,
        )
        fake_backup = target / ".harnest" / "project-backups" / "abc123"
        with patch("harnest.authoring.pack_cli.apply_project_plan", return_value=fake_backup):
            init_out = StringIO()
            self.assertEqual(_finish(plan, applying=True, as_json=False, output=init_out, is_init=True), 0)
            self.assertIn(f"Initialized {target}", init_out.getvalue())
            self.assertNotIn("Backup:", init_out.getvalue())

            upgrade_out = StringIO()
            self.assertEqual(_finish(plan, applying=True, as_json=False, output=upgrade_out, is_init=False), 0)
            self.assertIn(f"Applied. Backup: {fake_backup}", upgrade_out.getvalue())

    def test_pack_build_excludes_authoring_environment_and_vscode_state(self) -> None:
        """Pack wheels and binary staging skip .harnest, .venv, and .vscode in both pack roots and pack extensions."""
        from harnest.authoring.distribution import build_pack

        pack_dir = self._create_full_pack(include_local_pkg=False)
        # Simulate local IDE sync artifacts at both the pack root and inside a bundled extension.
        for base in (pack_dir, pack_dir / "company-ext" / "acme_guard"):
            (base / ".harnest" / "environments" / "authoring").mkdir(parents=True, exist_ok=True)
            (base / ".harnest" / "environments" / "authoring" / "marker.txt").write_text("local\n", encoding="utf-8")
            (base / ".vscode").mkdir(parents=True, exist_ok=True)
            (base / ".vscode" / "settings.json").write_text('{"python.defaultInterpreterPath": "x"}\n', encoding="utf-8")
            (base / ".venv").symlink_to(base / ".harnest" / "environments" / "authoring")

        wheel = build_pack(pack_dir, output=self.root / "dist")
        with ZipFile(wheel) as archive:
            names = archive.namelist()
        for forbidden in (".harnest", ".venv", ".vscode"):
            self.assertFalse(any(forbidden in name for name in names), f"wheel leaked {forbidden}: {names}")

        staging = self.root / "binary-stage"
        prepare_pack_binary(pack_dir, staging)
        staged_files = [path.relative_to(staging).as_posix() for path in staging.rglob("*")]
        for forbidden in (".harnest", ".venv", ".vscode"):
            self.assertFalse(any(forbidden in name for name in staged_files), f"binary stage leaked {forbidden}: {staged_files}")

    def test_pack_extension_unexpected_entry_lists_allowed_entries(self) -> None:
        """Unexpected root entries in a pack extension report the allowed root names."""
        pack_dir = self._create_full_pack(include_local_pkg=False)
        (pack_dir / "company-ext" / "acme_guard" / "libs").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ProjectError) as raised:
            prepare_pack_binary(pack_dir, self.root / "bad-stage")
        message = str(raised.exception)
        self.assertIn("unexpected entry: libs", message)
        self.assertIn("allowed:", message)
        self.assertIn("lib", message)
        self.assertIn("contributes:", message)

    def test_pack_extension_lifecycle_and_tools_import_own_lib_via_relative_imports(self) -> None:
        """Pack extensions resolve 'from ..lib import ...' inside lifecycle/ and tools/ without colliding with agent lib/."""
        pack_dir = self._create_full_pack(include_local_pkg=False)
        ext_root = pack_dir / "company-ext" / "acme_guard"
        (ext_root / "lib").mkdir(parents=True, exist_ok=True)
        (ext_root / "tools").mkdir(parents=True, exist_ok=True)
        (ext_root / "extension.yaml").write_text(
            "apiVersion: harnest.dev/v1alpha1\n"
            "kind: Extension\n"
            "metadata:\n"
            "  name: acme_guard\n"
            "  version: 1.0.0\n"
            "capabilities:\n"
            "  - policy.output\n"
            "  - content.tools\n"
            "contributes:\n"
            "  lifecycle:\n"
            "    - lifecycle\n"
            "  tools:\n"
            "    - tools\n",
            encoding="utf-8",
        )
        (ext_root / "lib" / "helpers.py").write_text(
            'SOURCE = "extension-lib"\n'
            'TOOL_ACTIVITY = False\n',
            encoding="utf-8",
        )
        (ext_root / "lifecycle" / "policy.py").write_text(
            "from harnest import lifecycle\n"
            "from harnest.output import AgentMetadataMode, OutputPolicy\n"
            "from ..lib.helpers import TOOL_ACTIVITY\n\n"
            "@lifecycle.output_policy\n"
            "def company_output_policy() -> OutputPolicy:\n"
            "    return OutputPolicy(\n"
            "        tool_activity=TOOL_ACTIVITY,\n"
            "        agent_metadata=AgentMetadataMode.SUPPRESS,\n"
            "    )\n",
            encoding="utf-8",
        )
        (ext_root / "tools" / "whoami.py").write_text(
            "from harnest.agent import tool\n"
            "from ..lib.helpers import SOURCE\n\n"
            "@tool\n"
            "def whoami() -> str:\n"
            '    """Return the helper source."""\n'
            "    return SOURCE\n",
            encoding="utf-8",
        )

        staging = self.root / "rel-stage"
        prepare_pack_binary(pack_dir, staging)
        agent_dir = self.root / "agent-with-own-lib"
        _write_minimal_agent(agent_dir)
        # Give the agent a competing top-level lib/helpers.py to verify extension relative imports stay isolated.
        (agent_dir / "lib").mkdir(parents=True, exist_ok=True)
        (agent_dir / "lib" / "helpers.py").write_text('SOURCE = "agent-lib"\nTOOL_ACTIVITY = True\n', encoding="utf-8")

        compiled_dir = self.root / "compiled-rel"
        previous = os.environ.get("HARNEST_PACK_EXTENSIONS")
        os.environ["HARNEST_PACK_EXTENSIONS"] = str(staging / "extensions")
        try:
            compile_artifact(agent_dir, compiled_dir)
        finally:
            if previous is None:
                os.environ.pop("HARNEST_PACK_EXTENSIONS", None)
            else:
                os.environ["HARNEST_PACK_EXTENSIONS"] = previous

        app = load_compiled_application(compiled_dir)
        try:
            self.assertIsNotNone(app.output_policy)
            self.assertFalse(app.output_policy.tool_activity)
            tool_names = [getattr(item, "__name__", getattr(item, "name", "")) for item in app.target.tools]
            self.assertIn("whoami", tool_names)
        finally:
            from harnest._library import release_authored_library
            release_extensions(discover_application_extensions(compiled_dir / "source"))
            release_authored_library(compiled_dir / "source")

    def test_pack_extension_missing_lib_import_reports_module_name_and_hint(self) -> None:
        """ModuleNotFoundError inside an extension lifecycle module names the missing module and suggests relative imports."""
        pack_dir = self._create_full_pack(include_local_pkg=False)
        ext_root = pack_dir / "company-ext" / "acme_guard"
        (ext_root / "lifecycle" / "policy.py").write_text(
            "from lib.only_in_extension import VALUE\n",
            encoding="utf-8",
        )
        staging = self.root / "err-stage"
        prepare_pack_binary(pack_dir, staging)
        agent_dir = self.root / "agent-err"
        _write_minimal_agent(agent_dir)

        previous = os.environ.get("HARNEST_PACK_EXTENSIONS")
        os.environ["HARNEST_PACK_EXTENSIONS"] = str(staging / "extensions")
        try:
            with self.assertRaises(BundleConventionError) as raised:
                compile_artifact(agent_dir, self.root / "compiled-err")
        finally:
            if previous is None:
                os.environ.pop("HARNEST_PACK_EXTENSIONS", None)
            else:
                os.environ["HARNEST_PACK_EXTENSIONS"] = previous
        message = str(raised.exception)
        self.assertIn("ModuleNotFoundError", message)
        self.assertIn("No module named 'lib'", message)
        self.assertIn("from ..lib.<module> import ...", message)


if __name__ == "__main__":
    unittest.main()

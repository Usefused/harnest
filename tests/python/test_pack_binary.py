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
        wheels_yaml = "    - wheels/acme_prebuilt-1.0.0-py3-none-any.whl\n"
        if include_local_pkg:
            _write_local_package(pack_dir / "local-pkg", "acme-local")
            wheels_yaml += "    - local-pkg\n"
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
            "  wheels:\n"
            f"{wheels_yaml}"
            "  requirements:\n"
            "    - acme-sdk==2.1.0\n"
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
            "packages:\n  requirements:\n    - harnest==1.0.0\n",
            encoding="utf-8",
        )
        with self.assertRaises(ProjectError):
            load_pack(bad_req)

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


if __name__ == "__main__":
    unittest.main()

"""End-to-end test for standalone branded pack CLI binaries built with `harnest pack build`."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


def _write_prebuilt_wheel(path: Path, name: str, version: str = "1.0.0") -> None:
    """Create a minimal importable pure-Python wheel for offline E2E testing."""
    pkg = name.replace("-", "_")
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{pkg}/__init__.py", f'VALUE = "{name}:{version}"\n')
        archive.writestr(
            f"{pkg}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{pkg}-{version}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: e2e\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{pkg}-{version}.dist-info/RECORD", "")


def _write_local_package(root: Path, name: str) -> None:
    """Write a local Python package directory built into a wheel during `pack build`."""
    pkg = name.replace("-", "_")
    (root / pkg).mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling>=1.25"]\nbuild-backend = "hatchling.build"\n\n'
        f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n\n'
        f'[tool.hatch.build.targets.wheel]\npackages = ["{pkg}"]\n',
        encoding="utf-8",
    )
    (root / pkg / "__init__.py").write_text('TAG = "acme-local-v1"\n', encoding="utf-8")


def _write_guard_extension(root: Path) -> None:
    """Write a canonical Harnest Extension with a lifecycle output_policy factory."""
    (root / "lifecycle").mkdir(parents=True, exist_ok=True)
    (root / "extension.yaml").write_text(
        "apiVersion: harnest.dev/v1alpha1\nkind: Extension\n"
        "metadata:\n  name: acme_guard\n  version: 1.0.0\n"
        "runtime:\n  entrypoint: extension:extension\n"
        "capabilities:\n  - policy.output\n"
        "contributes:\n  lifecycle:\n    - lifecycle\n",
        encoding="utf-8",
    )
    (root / "extension.py").write_text(
        'from harnest.extensions import Extension\n\n'
        'class AcmeGuard(Extension):\n    """Acme guard extension."""\n\n'
        'extension = AcmeGuard(name="acme_guard")\n',
        encoding="utf-8",
    )
    (root / "lifecycle" / "policy.py").write_text(
        "from harnest import lifecycle\n"
        "from harnest.output import AgentMetadataMode, OutputPolicy\n\n"
        "@lifecycle.output_policy\n"
        "def acme_policy() -> OutputPolicy:\n"
        "    return OutputPolicy(tool_activity=False, agent_metadata=AgentMetadataMode.SUPPRESS)\n",
        encoding="utf-8",
    )


def _create_company_pack(pack_dir: Path) -> None:
    """Author a complete pack with skills, wheels, requirements, extension, hooks, and command."""
    (pack_dir / "coding-skills" / "acme-standards").mkdir(parents=True)
    (pack_dir / "coding-skills" / "acme-standards" / "SKILL.md").write_text(
        "---\nname: acme-standards\ndescription: Acme coding rules.\n---\n# Acme Standards\n",
        encoding="utf-8",
    )
    (pack_dir / "coding-skills" / "harnest-authoring").mkdir(parents=True)
    (pack_dir / "coding-skills" / "harnest-authoring" / "SKILL.md").write_text(
        "---\nname: harnest-authoring\ndescription: Acme authoring override.\n---\n# Acme Authoring Override\n",
        encoding="utf-8",
    )
    _write_prebuilt_wheel(pack_dir / "wheels" / "acme_prebuilt-1.0.0-py3-none-any.whl", "acme-prebuilt")
    _write_local_package(pack_dir / "local-pkg", "acme-local")
    _write_guard_extension(pack_dir / "company-ext" / "acme_guard")
    (pack_dir / "pack.py").write_text(
        "from harnest.authoring import ChangePlan, ProjectContext, ProjectPack\n\n"
        "pack = ProjectPack(schema_version=1)\n\n"
        "@pack.initialize\n"
        "def init_hook(context: ProjectContext) -> ChangePlan:\n"
        '    return ChangePlan(context.yaml.set("acme.yaml", key=("managed_by",), value="acme-cli"))\n\n'
        '@pack.command("whoami", help="Print pack identity")\n'
        "def whoami(args: list[str]) -> int:\n"
        '    print("acme-command:" + ",".join(args))\n'
        "    return 0\n",
        encoding="utf-8",
    )
    (pack_dir / "pack.yaml").write_text(
        "apiVersion: harnest.dev/pack/v1\nname: acme\nversion: 1.5.0\n"
        "title: Acme agents\ncommand: acme\n"
        "skills:\n  - coding-skills/acme-standards\n  - coding-skills/harnest-authoring\n"
        "packages:\n  wheels:\n    - wheels/acme_prebuilt-1.0.0-py3-none-any.whl\n    - local-pkg\n"
        "  requirements:\n    - acme-sdk==2.1.0\n"
        "extensions:\n  - company-ext/acme_guard\n",
        encoding="utf-8",
    )


class PackBinaryE2ETests(unittest.TestCase):
    """Exercise the real stamped branded CLI binary without harnest installed on PATH."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="harnest-pack-e2e-"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def _build_branded_binary(self) -> Path:
        """Build the Go CLI, stamp the company pack, and delete both base binary and pack source."""
        go_bin = shutil.which("go")
        if go_bin is None:
            self.skipTest("go toolchain unavailable for standalone pack binary E2E test")
        base_bin = self.work / ("harnest-base.exe" if os.name == "nt" else "harnest-base")
        go_env = {**os.environ, "GOCACHE": str(ROOT / ".cache" / "go-build")}
        subprocess.run([go_bin, "build", "-o", str(base_bin), "./cmd/harnest"], cwd=ROOT, env=go_env, check=True)
        pack_dir = self.work / "acme-pack"
        _create_company_pack(pack_dir)
        dist = self.work / "dist"
        subprocess.run([str(base_bin), "--python", sys.executable, "pack", "validate", str(pack_dir)], check=True)
        subprocess.run([str(base_bin), "--python", sys.executable, "pack", "build", str(pack_dir), "-o", str(dist)], check=True)
        branded = dist / ("acme.exe" if os.name == "nt" else "acme")
        self.assertTrue(branded.is_file())
        # Remove both base binary and pack source to prove the stamped binary is self-contained.
        base_bin.unlink()
        shutil.rmtree(pack_dir)
        return branded

    def _isolated_env(self) -> dict[str, str]:
        """Construct a minimal environment with no harnest executable on PATH."""
        home = self.work / "home"
        home.mkdir(exist_ok=True)
        return {
            "PATH": os.defpath,
            "HOME": str(home),
            "HARNEST_PACK_CACHE": str(self.work / "cache"),
            "HARNEST_PYTHON": sys.executable,
            "OPENAI_MODEL": "openai/gpt-4o-mini",
            "OPENAI_BASE_URL": "https://models.example.invalid/v1",
        }

    def _assert_branding_and_skills(self, acme: Path, agent_dir: Path, env: dict[str, str]) -> None:
        """Verify branded version, custom command, init hook, and additive/overridden IDE skills."""
        ver = subprocess.run([str(acme), "--version"], env=env, capture_output=True, text=True, check=True)
        self.assertIn("acme version 1.5.0", ver.stdout)
        who = subprocess.run([str(acme), "whoami", "--strict", "payments"], env=env, capture_output=True, text=True, check=True)
        self.assertIn("acme-command:--strict,payments", who.stdout)
        subprocess.run([str(acme), "init", str(agent_dir), "--minimal"], env=env, check=True)
        self.assertEqual((agent_dir / "acme.yaml").read_text(encoding="utf-8").strip(), "managed_by: acme-cli")
        self.assertFalse((agent_dir / "extensions").exists())
        subprocess.run([str(acme), "skills", "install", "--target", "claude", "--project", str(agent_dir)], env=env, check=True)
        self.assertIn("# Acme Standards", (agent_dir / ".claude/skills/acme-standards/SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("# Acme Authoring Override", (agent_dir / ".claude/skills/harnest-authoring/SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((agent_dir / ".claude/skills/harnest-authentication/SKILL.md").is_file())

    def _assert_compile_test_and_guards(self, acme: Path, agent_dir: Path, env: dict[str, str]) -> None:
        """Verify compiled artifact embeds pack extension, runs cleanly, and blocks shadowing/overrides."""
        compiled = self.work / "compiled"
        subprocess.run([str(acme), "compile", str(agent_dir), "--output", str(compiled)], env=env, check=True)
        self.assertFalse((agent_dir / "extensions").exists())
        manifest = json.loads((compiled / "harnest-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in manifest["extensions"]], ["acme_guard"])
        # Load the compiled artifact in a clean Python process without the acme binary.
        probe = subprocess.run(
            [
                sys.executable, "-c",
                f"from harnest.runtime import load_compiled_application; "
                f"app = load_compiled_application({str(compiled)!r}); "
                f"assert app.output_policy.tool_activity is False",
            ],
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(ROOT / "src"),
                "OPENAI_MODEL": "openai/gpt-4o-mini",
                "OPENAI_BASE_URL": "https://models.example.invalid/v1",
            },
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(probe.returncode, 0)
        subprocess.run([str(acme), "test", str(agent_dir)], env=env, check=True)
        _write_guard_extension(agent_dir / "extensions" / "acme_guard")
        shadow = subprocess.run([str(acme), "compile", str(agent_dir), "--output", str(self.work / "bad")], env=env, capture_output=True, text=True)
        self.assertNotEqual(shadow.returncode, 0)
        self.assertIn("conflicts with pack-owned extension", shadow.stderr)
        shutil.rmtree(agent_dir / "extensions")
        (agent_dir / "pyproject.toml").write_text('[project]\nname = "my-agent"\nversion = "0.1.0"\ndependencies = ["acme-local==9.9.9"]\n', encoding="utf-8")
        override = subprocess.run([str(acme), "env", "sync", str(agent_dir)], env=env, capture_output=True, text=True)
        self.assertNotEqual(override.returncode, 0)
        self.assertIn("pack-owned package", override.stderr)

    def test_standalone_pack_binary_full_lifecycle(self) -> None:
        """Build, isolate, and exercise a complete standalone branded pack binary end to end."""
        acme = self._build_branded_binary()
        env = self._isolated_env()
        agent_dir = self.work / "my-agent"
        self._assert_branding_and_skills(acme, agent_dir, env)
        self._assert_compile_test_and_guards(acme, agent_dir, env)
        # Verify the materialized bundled wheels inside the cache are valid importable wheels.
        wheels = sorted((self.work / "cache" / "harnest" / "pack-binaries").glob("acme-*/wheels/*.whl"))
        self.assertEqual(len(wheels), 2)


if __name__ == "__main__":
    unittest.main()

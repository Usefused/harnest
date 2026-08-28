import re
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_yaml(relative_path: str):
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def workflow_events(workflow):
    # PyYAML follows YAML 1.1 and may decode GitHub's `on` key as boolean true.
    return workflow.get("on", workflow.get(True))


def write_executable(path: Path, source: str):
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def write_installer_fakes(directory: Path, version: str):
    write_executable(
        directory / "uname",
        """
        #!/bin/sh
        case "$1" in
          -s) printf 'Darwin\\n' ;;
          -m) printf 'arm64\\n' ;;
        esac
        """,
    )
    write_executable(
        directory / "curl",
        f"""
        #!/bin/sh
        output=
        while [ "$#" -gt 0 ]; do
          if [ "$1" = "--output" ]; then shift; output=$1; fi
          shift
        done
        case "$output" in
          *checksums.txt) printf 'abc harnest_{version}_darwin_arm64.tar.gz\\n' > "$output" ;;
          *) printf 'archive' > "$output" ;;
        esac
        """,
    )
    write_executable(
        directory / "shasum",
        """
        #!/bin/sh
        printf 'abc  %s\\n' "$3"
        """,
    )
    write_executable(
        directory / "sha256sum",
        """
        #!/bin/sh
        printf 'abc  %s\\n' "$1"
        """,
    )
    write_executable(
        directory / "tar",
        f"""
        #!/bin/sh
        destination=
        while [ "$#" -gt 0 ]; do
          if [ "$1" = "-C" ]; then shift; destination=$1; fi
          shift
        done
        printf '%s\\n' '#!/bin/sh' 'printf "harnest version {version}\\n"' > "$destination/harnest"
        """,
    )


class ReleaseWorkflowTests(unittest.TestCase):
    def test_ci_validates_before_owning_the_release_tag(self):
        workflow = load_yaml(".github/workflows/ci.yml")
        quality_job = workflow["jobs"]["quality"]
        tag_job = workflow["jobs"]["release-tag"]
        quality_step = next(
            step for step in quality_job["steps"] if step["name"] == "Run quality gates"
        )

        self.assertEqual(workflow_events(workflow)["push"]["branches"], ["**"])
        self.assertEqual(quality_step["run"], "make quality PYTHON=python")
        self.assertEqual(tag_job["needs"], "quality")
        self.assertEqual(tag_job["permissions"]["contents"], "write")
        self.assertIn("refs/heads/main", tag_job["if"])
        self.assertIn(
            "git push origin",
            tag_job["steps"][-1]["run"],
        )

    def test_release_runs_after_ci_and_never_creates_tags(self):
        workflow = load_yaml(".github/workflows/release.yml")
        events = workflow_events(workflow)
        release_job = workflow["jobs"]["goreleaser"]
        scripts = "\n".join(
            step.get("run", "") for step in release_job["steps"]
        )

        self.assertEqual(events["workflow_run"]["workflows"], ["CI"])
        self.assertNotIn("push", events)
        self.assertIn("workflow_run.head_sha", release_job["env"]["RELEASE_SHA"])
        self.assertIn('tagged_sha=$(git rev-list -n 1 "${tag}")', scripts)
        self.assertNotIn("git tag --annotate", scripts)
        self.assertNotIn("git push origin", scripts)

    def test_goreleaser_embeds_the_versioned_wheel_before_go_build(self):
        config = load_yaml(".goreleaser.yaml")
        uv_hook = config["before"]["hooks"][1]
        wheel_hook = config["before"]["hooks"][2]

        self.assertEqual(uv_hook, "python3 scripts/prepare_uv_assets.py")
        self.assertEqual(
            wheel_hook,
            "env HATCH_BUILD_VERSION={{ .Version }} python3 -m build --wheel "
            "--outdir internal/runtimewheel/assets",
        )
        self.assertEqual(config["archives"][0]["ids"], ["harnest"])
        self.assertEqual(config["builds"][0]["tags"], ["harnest_release"])
        self.assertEqual(
            config["archives"][0]["files"],
            ["LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/uv-LICENSE-MIT", "README.md"],
        )
        self.assertEqual(config["snapshot"]["version_template"], "{{ incpatch .Version }}.dev0")

    def test_goreleaser_changelog_omits_commit_identity(self):
        config = load_yaml(".goreleaser.yaml")

        # Release notes should describe changes without exposing implementation IDs.
        self.assertEqual(config["changelog"]["format"], "{{ .Message }}")

    def test_installer_bootstraps_the_runtime_from_the_binary(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('"${extract_directory}/harnest" runtime install', installer)
        self.assertIn('[ -n "${HARNEST_BOOTSTRAP_PYTHON:-}" ]', installer)
        self.assertNotIn("HARNEST_BOOTSTRAP_PYTHON:-python3", installer)
        self.assertNotIn("python/harnest-", installer)
        self.assertIn("Warning: harnest currently resolves to", installer)
        self.assertIn("is_legacy_python_launcher", installer)
        self.assertIn("install_directory=$existing_directory", installer)
        self.assertIn("replaced the legacy Python launcher", installer)
        self.assertIn("harnest init my-agent --framework adk", installer)
        self.assertIn("harnest env sync .", installer)
        self.assertNotIn("%s init my-agent", installer)
        self.assertIn("managed internally by Harnest", installer)
        self.assertIn("does not require a preinstalled Python", readme)

    def test_python_wheel_does_not_publish_the_native_cli_name(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        # The Go binary owns `harnest`; the bundled wheel is an internal runtime.
        self.assertNotIn("[project.scripts]", project)

    def test_compiled_framework_extras_include_builtin_store_drivers(self):
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["optional-dependencies"]

        # The CLI installs exactly one framework extra into every compiled
        # environment, so both extras must carry all built-in store drivers.
        for extra in ("adk", "langgraph", "all"):
            requirements = project[extra]
            self.assertTrue(any(value.startswith("asyncpg") for value in requirements))
            self.assertTrue(any(value.startswith("redis") for value in requirements))

    def test_installer_replaces_a_writable_legacy_python_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "fake-bin"
            legacy_bin = root / "legacy-bin"
            test_version = "9.8.7"
            fake_bin.mkdir()
            legacy_bin.mkdir()
            write_installer_fakes(fake_bin, test_version)
            write_executable(
                legacy_bin / "harnest",
                """
                #!/usr/bin/env python3
                from harnest.cli import main
                """,
            )
            environment = {
                "PATH": f"{fake_bin}:{legacy_bin}:/usr/bin:/bin",
                "HOME": str(root / "home"),
                "HARNEST_VERSION": test_version,
                "HARNEST_YES": "1",
            }

            result = subprocess.run(
                ["/bin/sh", str(ROOT / "install.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"CLI:     {legacy_bin}/harnest", result.stdout)
            self.assertIn("Migrated: replaced the legacy Python launcher", result.stdout)
            self.assertNotIn("Warning:", result.stdout)
            self.assertIn("\n  harnest doctor\n", result.stdout)
            self.assertNotIn(f"\n  {legacy_bin}/harnest doctor\n", result.stdout)
            self.assertIn(
                f"harnest version {test_version}",
                (legacy_bin / "harnest").read_text(),
            )
            self.assertFalse((root / "home" / ".local/bin/harnest").exists())

    def test_source_fallback_matches_project_version(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        compatibility = (ROOT / "src/harnest/compatibility.py").read_text(
            encoding="utf-8"
        )
        project_version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)
        source_version = re.search(
            r'^_SOURCE_VERSION = "([^"]+)"$', compatibility, re.MULTILINE
        )

        self.assertIsNotNone(project_version)
        self.assertIsNotNone(source_version)
        self.assertEqual(project_version.group(1), source_version.group(1))


if __name__ == "__main__":
    unittest.main()

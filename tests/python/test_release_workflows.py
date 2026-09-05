import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the maintained compatibility package.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]


def load_yaml(relative_path: str):
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def workflow_events(workflow):
    # PyYAML follows YAML 1.1 and may decode GitHub's `on` key as boolean true.
    return workflow.get("on", workflow.get(True))


def distribution_requirements(metadata: str) -> dict[str, str]:
    """Return normalized dependency constraints from wheel metadata."""
    requirements = {}
    for line in metadata.splitlines():
        if line.startswith("Requires-Dist: "):
            requirement = Requirement(line.removeprefix("Requires-Dist: "))
            requirements[requirement.name] = str(requirement.specifier)
    return requirements


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
    def test_official_extensions_publish_with_trusted_publishing(self):
        workflow = load_yaml(".github/workflows/publish-extensions.yml")
        events = workflow_events(workflow)
        build_job = workflow["jobs"]["build"]
        scripts = "\n".join(step.get("run", "") for step in build_job["steps"])

        self.assertEqual(workflow["name"], "Publish Official Extensions")
        self.assertEqual(
            events["push"]["tags"],
            [
                "harnest-extension-docker-v*",
                "harnest-extension-hatchet-v*",
            ],
        )
        self.assertEqual(
            events["push"]["paths"],
            [
                "official-extensions/docker/**",
                "official-extensions/hatchet/**",
                ".github/workflows/publish-extensions.yml",
            ],
        )
        self.assertEqual(
            events["workflow_dispatch"]["inputs"]["extension"]["options"],
            ["docker", "hatchet"],
        )
        self.assertEqual(
            build_job["strategy"]["matrix"]["extension"],
            ["docker", "hatchet"],
        )
        self.assertEqual(workflow["permissions"]["contents"], "read")
        self.assertNotIn("id-token", workflow["permissions"])
        expected_actions = {
            "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
            "actions/download-artifact": "634f93cb2916e3fdff6788551b99b062d0335ce0",
            "pypa/gh-action-pypi-publish": (
                "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
            ),
        }
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                action = step.get("uses")
                if action is None:
                    continue
                repository, revision = action.split("@", 1)
                self.assertEqual(revision, expected_actions[repository])
        for slug in ("docker", "hatchet"):
            publish_job = workflow["jobs"][f"publish-{slug}"]
            publish_step = next(
                step
                for step in publish_job["steps"]
                if step["name"] == "Publish extension to PyPI"
            )
            self.assertEqual(publish_job["permissions"]["id-token"], "write")
            self.assertEqual(publish_job["needs"], "build")
            self.assertEqual(publish_job["environment"], "pypi")
            self.assertIn(f"harnest-extension-{slug}-v", publish_job["if"])
            self.assertTrue(
                publish_step["uses"].startswith(
                    "pypa/gh-action-pypi-publish@dc37677"
                )
            )
            self.assertNotIn("password", publish_step.get("with", {}))
        self.assertIn('GITHUB_REF}" != "refs/heads/main', scripts)
        self.assertIn('tag_version}" != "${project_version}', scripts)
        self.assertIn(
            'git merge-base --is-ancestor "${GITHUB_SHA}" "origin/main"',
            scripts,
        )
        checkout = build_job["steps"][0]
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertIn("official-extensions/${EXTENSION}", scripts)
        self.assertIn("pip install --no-deps dist/*.whl", scripts)
        self.assertIn("distribution.files", scripts)
        self.assertIn("entries[0].load()", scripts)
        self.assertIn("official-extensions/docker/_tests/test_backend.py", scripts)
        self.assertIn("tests/python/test_hatchet_plugin_consumer.py", scripts)
        self.assertIn("check_python_complexity.py --max 10", scripts)

    def test_official_extension_wheels_expose_verified_entry_points(self):
        expected_modules = {
            "docker": {
                "lib/__init__.py",
                "lib/backend.py",
                "lib/docker_runtime.py",
                "lib/guard.py",
                "lib/socket_stream.py",
                "lib/startup.py",
            },
            "hatchet": {
                "lib/client.py",
                "lib/continuations.py",
                "lib/payloads.py",
            },
        }
        expected_requirements = {
            "docker": {"docker": "<8,>=7.1", "harnest": "<0.15,>=0.14"},
            "hatchet": {
                "harnest": "<0.15,>=0.13",
                "hatchet-sdk": "<2,>=1.38",
            },
        }
        for slug in ("docker", "hatchet"):
            with (
                self.subTest(extension=slug),
                tempfile.TemporaryDirectory() as temporary,
            ):
                temporary_root = Path(temporary)
                source = temporary_root / slug
                distribution = temporary_root / "dist"
                shutil.copytree(ROOT / "official-extensions" / slug, source)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "build",
                        "--wheel",
                        "--no-isolation",
                        "--outdir",
                        str(distribution),
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                wheel = next(distribution.glob("*.whl"))
                package = f"harnest_extension_{slug}"
                with zipfile.ZipFile(wheel) as archive:
                    names = archive.namelist()
                    entry_points_path = next(
                        name
                        for name in names
                        if name.endswith(".dist-info/entry_points.txt")
                    )
                    metadata_path = next(
                        name
                        for name in names
                        if name.endswith(".dist-info/METADATA")
                    )
                    entry_points = archive.read(entry_points_path).decode("utf-8")
                    readme = archive.read(f"{package}/README.md").decode("utf-8")
                    metadata = archive.read(metadata_path).decode("utf-8")

                self.assertIn(f"{package}/extension.py", names)
                self.assertIn(f"{package}/extension.yaml", names)
                self.assertIn(f"{package}/README.md", names)
                self.assertTrue(
                    {
                        f"{package}/{relative}"
                        for relative in expected_modules[slug]
                    }.issubset(names)
                )
                self.assertFalse(
                    any(
                        "/_tests/" in name
                        or "/_scripts/" in name
                        or "__pycache__" in name
                        for name in names
                    )
                )
                self.assertIn("Harnest Extension", readme)
                self.assertIn("not an Agent Plugin", readme)
                self.assertIn("Description-Content-Type: text/markdown", metadata)
                self.assertIn("not an Agent Plugin", metadata)
                self.assertEqual(
                    distribution_requirements(metadata),
                    expected_requirements[slug],
                )
                self.assertIn("[harnest.extensions]", entry_points)
                self.assertIn(
                    f"{slug} = {package}.extension:extension", entry_points
                )

    def test_official_extension_projects_use_packaged_readmes(self):
        for slug in ("docker", "hatchet"):
            with self.subTest(extension=slug):
                root = ROOT / "official-extensions" / slug
                project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
                readme = (root / "README.md").read_text("utf-8")

                self.assertEqual(project["project"]["readme"], "README.md")
                self.assertIn(
                    "README.md",
                    project["tool"]["setuptools"]["package-data"][
                        f"harnest_extension_{slug}"
                    ],
                )
                self.assertIn("https://docs.usefused.com/harnest", readme)
                self.assertIn("https://github.com/Usefused/harnest", readme)

    def test_ci_only_validates_source_changes(self):
        workflow = load_yaml(".github/workflows/ci.yml")
        quality_job = workflow["jobs"]["quality"]
        quality_step = next(
            step for step in quality_job["steps"] if step["name"] == "Run quality gates"
        )

        events = workflow_events(workflow)
        self.assertEqual(events["push"]["branches"], ["**"])
        for event in ("pull_request", "push"):
            self.assertEqual(
                events[event]["paths-ignore"], ["official-extensions/**"]
            )
        self.assertEqual(quality_step["run"], "make quality PYTHON=python")
        self.assertNotIn("release-tag", workflow["jobs"])
        self.assertEqual(workflow["permissions"]["contents"], "read")

    def test_release_please_runs_after_successful_main_ci(self):
        workflow = load_yaml(".github/workflows/release-please.yml")
        events = workflow_events(workflow)
        release_job = workflow["jobs"]["release-please"]
        token_step = next(
            step
            for step in release_job["steps"]
            if step["name"] == "Require release automation token"
        )
        release_step = next(
            step
            for step in release_job["steps"]
            if step["name"] == "Propose or create release"
        )
        checkout_step = next(
            step
            for step in release_job["steps"]
            if step["name"] == "Check out release proposal"
        )
        finalize_step = next(
            step
            for step in release_job["steps"]
            if step["name"] == "Mark authored changelog notes with the proposed release"
        )

        self.assertEqual(workflow["name"], "Prepare Release")
        self.assertEqual(events["workflow_run"]["workflows"], ["CI"])
        self.assertIn("workflow_run.conclusion == 'success'", release_job["if"])
        self.assertIn("workflow_run.head_branch == 'main'", release_job["if"])
        self.assertIn("RELEASE_PLEASE_TOKEN", token_step["run"])
        self.assertEqual(
            release_step["uses"], "googleapis/release-please-action@v4"
        )
        self.assertEqual(workflow["permissions"]["issues"], "write")
        self.assertEqual(
            release_step["with"]["token"],
            "${{ secrets.RELEASE_PLEASE_TOKEN }}",
        )
        self.assertEqual(
            checkout_step["with"]["ref"],
            "${{ fromJSON(steps.release.outputs.pr).headBranchName }}",
        )
        self.assertEqual(
            checkout_step["with"]["token"],
            "${{ secrets.RELEASE_PLEASE_TOKEN }}",
        )
        self.assertIn("scripts/finalize_release_changelog.py", finalize_step["run"])
        self.assertIn('git push origin "HEAD:${RELEASE_BRANCH}"', finalize_step["run"])
        self.assertEqual(tuple(workflow["jobs"]), ("release-please",))

    def test_release_please_updates_both_source_versions(self):
        config = json.loads((ROOT / "release-please-config.json").read_text())
        manifest = json.loads(
            (ROOT / ".release-please-manifest.json").read_text()
        )
        package = config["packages"]["."]
        compatibility = (ROOT / "src/harnest/compatibility.py").read_text()

        self.assertEqual(package["release-type"], "python")
        self.assertFalse(package["include-component-in-tag"])
        self.assertTrue(package["include-v-in-tag"])
        self.assertIn(
            {"type": "generic", "path": "src/harnest/compatibility.py"},
            package["extra-files"],
        )
        self.assertIn("x-release-please-version", compatibility)
        self.assertEqual(tuple(manifest), (".",))
        project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())[
            "project"
        ]["version"]
        # The bootstrap manifest may lead the stale source once; the generated
        # release PR brings both forward to the same reviewed version.
        self.assertGreaterEqual(Version(manifest["."]), Version(project_version))

    def test_packaging_verifies_release_please_tag_and_source_versions(self):
        workflow = load_yaml(".github/workflows/release.yml")
        events = workflow_events(workflow)
        release_job = workflow["jobs"]["goreleaser"]
        scripts = "\n".join(step.get("run", "") for step in release_job["steps"])

        self.assertEqual(events["release"]["types"], ["published"])
        self.assertNotIn("workflow_call", events)
        self.assertEqual(
            release_job["env"]["RELEASE_TAG"],
            "${{ github.event.release.tag_name || inputs.tag_name }}",
        )
        self.assertIn('tagged_sha=$(git rev-list -n 1 "${RELEASE_TAG}")', scripts)
        self.assertIn('pathlib.Path("pyproject.toml")', scripts)
        self.assertIn("src/harnest/compatibility.py", scripts)
        self.assertIn(
            'scripts/finalize_release_changelog.py --version "${tag_version}" --check',
            scripts,
        )
        self.assertIn('gh release view "${RELEASE_TAG}"', scripts)
        self.assertNotIn("git tag --annotate", scripts)
        self.assertNotIn("git push origin", scripts)

    def test_goreleaser_embeds_the_versioned_wheel_before_go_build(self):
        config = load_yaml(".goreleaser.yaml")
        uv_hook = config["before"]["hooks"][1]
        wheel_hook = config["before"]["hooks"][2]

        self.assertEqual(uv_hook, "python3 scripts/prepare_uv_assets.py")
        self.assertEqual(
            wheel_hook,
            "python3 scripts/build_runtime_wheel.py --version {{ .Version }} "
            "{{ if .IsSnapshot }}--allow-version-override{{ end }} "
            "--outdir internal/runtimewheel/assets",
        )
        self.assertEqual(config["release"]["mode"], "keep-existing")
        self.assertNotIn("github", config["release"])
        self.assertEqual(config["archives"][0]["ids"], ["harnest"])
        self.assertEqual(config["builds"][0]["tags"], ["harnest_release"])
        self.assertEqual(
            config["archives"][0]["files"],
            ["LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/uv-LICENSE-MIT", "README.md"],
        )
        self.assertEqual(config["snapshot"]["version_template"], "{{ incpatch .Version }}.dev0")

    def test_release_checkout_discards_stale_repository_targets(self):
        workflow = load_yaml(".github/workflows/release.yml")
        release_job = workflow["jobs"]["goreleaser"]
        target_step = next(
            step
            for step in release_job["steps"]
            if step["name"] == "Target the current repository"
        )

        self.assertEqual(
            target_step["env"]["CURRENT_REPOSITORY"],
            "${{ github.repository }}",
        )
        self.assertIn("git remote set-url origin", target_step["run"])
        self.assertIn("re.subn(", target_step["run"])

    def test_snapshot_wheel_uses_the_explicit_build_version(self):
        snapshot_version = "9.8.7.dev0"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build_runtime_wheel.py"),
                    "--version",
                    snapshot_version,
                    "--outdir",
                    temporary,
                    "--allow-version-override",
                    "--no-isolation",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheel = next(Path(temporary).glob("harnest-*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                metadata_path = next(
                    name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
                )
                metadata_text = archive.read(metadata_path).decode("utf-8")

        self.assertIn(f"Version: {snapshot_version}\n", metadata_text)

    def test_release_wheel_rejects_a_version_not_in_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build_runtime_wheel.py"),
                    "--version",
                    "9.8.7",
                    "--outdir",
                    temporary,
                    "--no-isolation",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match source", result.stderr)

    def test_goreleaser_changelog_omits_commit_identity(self):
        config = load_yaml(".goreleaser.yaml")

        # Release notes should describe changes without exposing implementation IDs.
        self.assertEqual(config["changelog"]["format"], "{{ .Message }}")

    def test_installer_bootstraps_the_runtime_from_the_binary(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("repo=${HARNEST_REPO:-Usefused/harnest}", installer)
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

    def test_base_runtime_includes_httpx_socks_transport(self):
        """Released installs support operator-provided SOCKS proxy environments."""

        dependencies = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"]

        self.assertIn("httpx[socks]>=0.28,<1", dependencies)

    def test_all_extra_includes_the_task_runtime(self):
        """Keep the documented development install capable of live task tests."""

        extras = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["optional-dependencies"]

        # Compiled bundles remain feature-selective, while `all` must include
        # every backend exercised by the repository's complete quality gate.
        self.assertEqual(extras["tasks"], ["procrastinate==3.9.0"])
        self.assertIn("procrastinate==3.9.0", extras["all"])

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
            r'^_SOURCE_VERSION = "([^"]+)"', compatibility, re.MULTILINE
        )

        self.assertIsNotNone(project_version)
        self.assertIsNotNone(source_version)
        self.assertEqual(project_version.group(1), source_version.group(1))


if __name__ == "__main__":
    unittest.main()

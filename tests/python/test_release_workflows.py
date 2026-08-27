import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def load_yaml(relative_path: str):
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def workflow_events(workflow):
    # PyYAML follows YAML 1.1 and may decode GitHub's `on` key as boolean true.
    return workflow.get("on", workflow.get(True))


class ReleaseWorkflowTests(unittest.TestCase):
    def test_ci_validates_before_owning_the_release_tag(self):
        workflow = load_yaml(".github/workflows/ci.yml")
        tag_job = workflow["jobs"]["release-tag"]

        self.assertEqual(workflow_events(workflow)["push"]["branches"], ["**"])
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
        wheel_hook = config["before"]["hooks"][1]

        self.assertEqual(
            wheel_hook,
            "env HATCH_BUILD_VERSION={{ .Version }} python3 -m build --wheel "
            "--outdir internal/runtimewheel/assets",
        )
        self.assertEqual(config["archives"][0]["ids"], ["harnest"])
        self.assertEqual(config["archives"][0]["files"], ["LICENSE", "README.md"])
        self.assertEqual(config["snapshot"]["version_template"], "{{ incpatch .Version }}.dev0")

    def test_installer_bootstraps_the_runtime_from_the_binary(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn('"${extract_directory}/harnest" runtime install', installer)
        self.assertNotIn("python/harnest-", installer)

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

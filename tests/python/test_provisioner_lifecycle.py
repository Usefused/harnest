"""Exercise journal, process, and CLI boundaries without deploying real infrastructure."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from harnest.provisioner import Provisioner, run_process
from harnest.provisioner_cli import initialize, main
from harnest.provisioner_config import MANIFEST, ProvisionError
from test_provisioner import MANIFEST as CONFIG


class FakeBackend:
    """Record real serialized requests and model exact-resource Kubernetes ownership."""

    def __init__(self):
        """Retain fake infrastructure across provisioner restarts."""

        self.calls = []
        self.objects = {}
        self.fail_rollout = False
        self.image_versions = {}

    def __call__(self, argv, payload=""):
        """Implement only API responses needed to exercise apply, readiness, and cleanup."""

        self.calls.append((argv, payload))
        if argv[0] == "docker":
            return self.docker(argv)

        if "get" in argv:
            index = argv.index("get")
            key = tuple(argv[index + 1:index + 3])
            if key[0] == "deployments":
                return '{"items":[]}'
            return json.dumps(self.objects[key]) if key in self.objects else ""
        if "apply" in argv:
            item = json.loads(payload)
            self.objects[item["kind"], item["metadata"]["name"]] = item
        if "rollout" in argv and self.fail_rollout:
            raise ProvisionError("readiness failed")
        return ""

    def docker(self, argv):
        """Resolve tags deterministically while allowing tests to model mutable tag updates."""

        import hashlib
        digest = self.image_versions.get(argv[-1], "sha256:" + hashlib.sha256(argv[-1].encode()).hexdigest())
        if argv[1:3] == ["image", "inspect"]:
            return json.dumps(digest)
        if "imagetools" in argv:
            return json.dumps({"digest": digest})
        return "[]" if "ps" in argv else ""


class ProvisionerLifecycleTests(unittest.TestCase):
    """Prove retry and deletion ownership independently of editable manifests."""

    def setUp(self):
        """Give each deployment private journal and fake backend state."""

        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        (self.root / MANIFEST).write_text(CONFIG)
        self.backend = FakeBackend()
        self.service = Provisioner(self.root, runner=self.backend)

    def test_plan_is_offline_and_missing_secrets_cannot_partially_apply(self):
        """Neither previews nor missing credentials may create a backend resource."""

        self.service.plan()
        with self.assertRaises(ProvisionError):
            self.service.apply({})
        self.assertEqual(self.backend.calls, [])

    def test_local_apply_status_stop_remove_retain_data_and_hide_credentials(self):
        """Only apply stdin contains credentials; teardown uses durable credential-free inventory."""

        with patch("harnest.provisioner._AUDIT") as audit:
            result = self.service.apply({"DATABASE_URL": "postgres://user:secret@db/app"})
            audit.info.assert_called_once()
        self.assertEqual(result["status"], "ready")
        apply = next(call for call in self.backend.calls if "up" in call[0])
        self.assertIn("--wait", apply[0])
        self.assertIn("secret", apply[1])
        journal = json.dumps(self.service._read())
        self.assertNotIn("postgres://", journal)
        self.assertNotIn("secret", journal)
        self.assertEqual((self.service.directory / "revisions.sqlite3").stat().st_mode & 0o777, 0o600)
        (self.root / MANIFEST).unlink()
        restarted = Provisioner(self.root, runner=self.backend)
        self.assertEqual(restarted.status()["last_operation_status"], "ready")
        restarted.control("stop")
        restarted.control("remove")
        self.assertEqual(restarted.status()["status"], "not-deployed")
        for argv, payload in self.backend.calls:
            if "up" in argv:
                continue
            self.assertNotIn("--volumes", argv)
            self.assertNotIn("secret", payload)
        self.assertIn("down", self.backend.calls[-1][0])

    def test_kubernetes_dependency_order_and_external_service_exclusion(self):
        """Services become ready before agents; external resources receive no API writes."""

        (self.root / MANIFEST).write_text(CONFIG.replace("name: support", "name: support\nbackend: kubernetes\ncontext: k3s\nnamespace: agents"))
        self.service.apply({"DATABASE_URL": "private"})
        applied = [json.loads(payload) for argv, payload in self.backend.calls if "apply" in argv]
        deployments = [item["metadata"]["labels"]["harnest.dev/component"] for item in applied if item["kind"] == "Deployment"]
        self.assertEqual(deployments, ["cache", "worker", "coordinator"])
        self.assertFalse(any(item["metadata"]["name"].endswith("-database") for item in applied))
        for argv, _ in self.backend.calls:
            if argv[0] == "kubectl":
                self.assertEqual(argv[argv.index("--context") + 1], "k3s")
        self.service.control("remove")
        deleted = [argv[argv.index("delete") + 1] for argv, _ in self.backend.calls if "delete" in argv]
        self.assertNotIn("PersistentVolumeClaim", deleted)
        self.assertNotIn("Namespace", deleted)

    def test_interrupted_apply_is_retryable_and_still_removable(self):
        """A failed readiness gate preserves ownership rather than falsely reporting success."""

        self.service = Provisioner(self.root, "production", runner=self.backend)
        self.backend.fail_rollout = True
        with patch("harnest.provisioner._AUDIT") as audit:
            with self.assertRaisesRegex(ProvisionError, "readiness"):
                self.service.apply({"DATABASE_URL": "private", "REDIS_URL": "private"})
            audit.warning.assert_called_once()
            audit.info.assert_not_called()
        state = json.loads(json.dumps(self.service._read()))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(set(state["resources"]), {"worker", "coordinator"})
        self.backend.fail_rollout = False
        self.assertEqual(self.service.apply({"DATABASE_URL": "new", "REDIS_URL": "new"})["status"], "ready")
        self.service.control("remove")

    def test_target_changes_cannot_redirect_cleanup(self):
        """Changing the manifest cannot silently move or delete an existing deployment."""

        self.service.apply({"DATABASE_URL": "private"})
        (self.root / MANIFEST).write_text(CONFIG.replace("name: support", "name: another"))
        with self.assertRaisesRegex(ProvisionError, "previous deployment"):
            self.service.apply({"DATABASE_URL": "private"})
        self.service.control("remove")
        self.assertEqual(self.service.apply({"DATABASE_URL": "private"})["status"], "ready")

    def test_ownership_conflict_is_not_adopted(self):
        """Preexisting unowned Kubernetes resources are never overwritten."""

        service = Provisioner(self.root, "production", runner=self.backend)
        plan = service._plan()
        self.backend.objects["Secret", plan.resource_name("worker")] = {"metadata": {"labels": {}}}
        with self.assertRaisesRegex(ProvisionError, "not owned"):
            service.apply({"DATABASE_URL": "private", "REDIS_URL": "private"})
        self.assertFalse(any("apply" in argv for argv, _ in self.backend.calls))

    def test_removed_dependency_is_pruned_but_its_volume_is_retained(self):
        """Switching provision to connect cleans up old workloads only after replacement readiness."""

        text = CONFIG.replace("name: support", "name: support\nbackend: kubernetes\ncontext: k3s\nnamespace: agents")
        (self.root / MANIFEST).write_text(text)
        self.service.apply({"DATABASE_URL": "db"})
        import yaml
        document = yaml.safe_load(text)
        document["services"]["cache"] = {"mode": "connect", "url": {"secret": "REDIS_URL"}, "variable": "REDIS_URL"}
        (self.root / MANIFEST).write_text(yaml.safe_dump(document))
        self.service.apply({"DATABASE_URL": "db", "REDIS_URL": "redis"})
        deleted = [argv for argv, _ in self.backend.calls if "delete" in argv]
        self.assertTrue(any("Deployment" in argv and any(value.endswith("-cache") for value in argv) for argv in deleted))
        self.assertFalse(any("PersistentVolumeClaim" in argv for argv in deleted))

    def test_state_links_and_parallel_operations_are_rejected(self):
        """Two callers cannot race resource ownership or redirect the private journal."""

        with self.service._locked():
            with self.assertRaisesRegex(ProvisionError, "running"):
                self.service.status()
        (self.service.directory / "revisions.sqlite3").unlink()
        (self.service.directory / "revisions.sqlite3").symlink_to(self.root / MANIFEST)
        with self.assertRaisesRegex(ProvisionError, "symbolic"):
            self.service.status()

    def test_process_errors_do_not_echo_backend_credentials(self):
        """Unexpected tool stderr must not become browser output or tracebacks."""

        failed = subprocess.CompletedProcess(["docker"], 1, "password=private", "token=private")
        with patch("harnest.provisioner.subprocess.run", return_value=failed):
            with self.assertRaises(ProvisionError) as error:
                run_process(["docker", "compose"], "credential")
        self.assertNotIn("private", str(error.exception))

    def test_cli_starter_and_plan_work_without_a_backend(self):
        """Users can create and inspect the real manifest through the CLI entry point."""

        (self.root / MANIFEST).unlink()
        initialize(self.root)
        with self.assertRaises(ProvisionError):
            initialize(self.root)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["plan", "--project", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["components"][0]["name"], "cache")
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["plan", "--project", str(self.root), "--environment", "missing"]), 1)

    def test_local_ownership_conflict_cannot_adopt_existing_containers(self):
        """A generated Compose project name does not authorize replacing unrelated containers."""

        def unowned(argv, payload=""):
            """Expose one unrelated container through the real ownership query contract."""

            if argv[1:3] == ["image", "inspect"]:
                return json.dumps("sha256:" + "1" * 64)
            if "ps" in argv:
                return "container-id\n"
            if "inspect" in argv:
                return '{}\n'
            raise AssertionError("No mutation may follow an ownership conflict")

        service = Provisioner(self.root, runner=unowned)
        with self.assertRaisesRegex(ProvisionError, "not owned"):
            service.apply({"DATABASE_URL": "private"})

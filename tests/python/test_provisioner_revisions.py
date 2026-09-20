"""Deployment history, immutable image resolution, and rollback across process restarts."""

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import yaml

from harnest.provisioner import Provisioner
from harnest.provisioner_config import MANIFEST, ProvisionError
from harnest.provisioner_history import RevisionStore
from harnest.provisioner_images import pin_image
from test_provisioner import MANIFEST as CONFIG
from test_provisioner_lifecycle import FakeBackend


class ProvisionerRevisionTests(unittest.TestCase):
    """Keep active state, attempt history, and immutable snapshots coherent through failures."""

    def setUp(self):
        """Use private storage and a fake backend with mutable image tags."""

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / MANIFEST).write_text(CONFIG)
        self.backend = FakeBackend()
        self.service = Provisioner(self.root, runner=self.backend)
        self.secrets = {"DATABASE_URL": "postgres://secret-old", "REDIS_URL": "redis://secret-old"}

    def edit(self, change):
        """Simulate editing source after an immutable revision has been recorded."""

        document = yaml.safe_load((self.root / MANIFEST).read_text())
        change(document)
        (self.root / MANIFEST).write_text(yaml.safe_dump(document))

    def test_rollback_restores_snapshot_and_pinned_images_using_current_secrets(self):
        """Retargeting an image tag or editing source cannot change the selected historical release."""

        first = self.service.apply(self.secrets)
        self.assertEqual(first["active_revision"], 1)
        self.backend.image_versions["registry.example/worker:1"] = "sha256:" + "b" * 64
        self.edit(lambda document: document.update(release="second"))
        self.service.apply(self.secrets)
        (self.root / MANIFEST).unlink()
        restarted = Provisioner(self.root, runner=self.backend)
        before = len(self.backend.calls)
        preview = restarted.plan(1)
        self.assertEqual(preview["rollback_of"], 1)
        self.assertEqual(len(self.backend.calls), before)
        result = restarted.rollback(1, {"DATABASE_URL": "postgres://rotated"})
        self.assertEqual(result["active_revision"], 3)
        document = json.loads(next(payload for argv, payload in reversed(self.backend.calls) if "up" in argv))
        expected = "sha256:" + hashlib.sha256(b"registry.example/worker:1").hexdigest()
        self.assertEqual(document["services"]["worker"]["image"], expected)
        self.assertEqual(document["services"]["worker"]["environment"]["DATABASE_URL"], "postgres://rotated")
        history = restarted.history()
        self.assertEqual([row["revision"] for row in history["revisions"]], [3, 2, 1])
        self.assertEqual(history["revisions"][0]["rollback_of"], 1)
        self.assertEqual(history["revisions"][0]["operation"], "rollback")
        self.assertEqual(history["revisions"][1]["release"], "second")
        contents = (restarted.directory / "revisions.sqlite3").read_bytes()
        self.assertNotIn(b"secret-old", contents)
        self.assertNotIn(b"postgres://rotated", contents)
        self.assertNotIn("snapshot", history["revisions"][0])

    def test_failed_apply_and_rollback_preserve_last_successful_revision(self):
        """Only completed readiness checks can advance the active revision pointer."""

        service = Provisioner(self.root, "production", runner=self.backend)
        service.apply(self.secrets)
        self.backend.fail_rollout = True
        with self.assertRaises(ProvisionError):
            service.apply(self.secrets)
        self.assertEqual(service.history()["active_revision"], 1)
        with self.assertRaisesRegex(ProvisionError, "successfully"):
            service.rollback(2, self.secrets)
        with self.assertRaises(ProvisionError):
            service.rollback(1, self.secrets)
        history = service.history()
        self.assertEqual(history["active_revision"], 1)
        self.assertEqual([row["status"] for row in history["revisions"]], ["failed", "failed", "succeeded"])
        self.assertEqual(history["revisions"][0]["rollback_of"], 1)

    def test_persistent_service_changes_block_implicit_database_downgrades(self):
        """Rollback does not revert database binaries or initialization against retained data."""

        self.service.apply(self.secrets)
        self.backend.image_versions["redis:7.4"] = "sha256:" + "c" * 64
        self.service.apply(self.secrets)
        before = len(self.backend.calls)
        with self.assertRaisesRegex(ProvisionError, "persistent service"):
            self.service.rollback(1, self.secrets)
        self.assertEqual(len(self.backend.calls), before)
        self.assertEqual(self.service.history()["active_revision"], 2)

    def test_new_workloads_are_removed_when_rolling_back(self):
        """A rollback reconciles the full snapshot, including deletion of newly added workers."""

        service = Provisioner(self.root, "production", runner=self.backend)
        service.apply(self.secrets)
        self.edit(lambda document: document["agents"].update(extra=dict(document["agents"]["worker"])))
        service.apply(self.secrets)
        before = len(self.backend.calls)
        service.rollback(1, self.secrets)
        deleted = [argv for argv, _ in self.backend.calls[before:] if "delete" in argv]
        self.assertTrue(any(any(value.endswith("-extra") for value in argv) for argv in deleted))
        self.assertFalse(any("PersistentVolumeClaim" in argv for argv in deleted))

    def test_stop_remove_and_redeploy_preserve_history_and_revision_sequence(self):
        """Control actions change deployment state without rewriting historical outcomes."""

        self.service.apply(self.secrets)
        self.service.control("stop")
        self.assertEqual(self.service.history()["revisions"][0]["status"], "succeeded")
        self.service.control("remove")
        history = self.service.history()
        self.assertIsNone(history["active_revision"])
        self.assertEqual(len(history["revisions"]), 1)
        with self.assertRaises(ProvisionError):
            self.service.rollback(1, self.secrets)
        self.assertEqual(self.service.apply(self.secrets)["active_revision"], 2)

    def test_history_is_paginated_in_database_without_loading_snapshots(self):
        """One ordered metadata query returns the selected page and a stable cursor."""

        for _ in range(3):
            self.service.apply(self.secrets)
        queries = []
        original = sqlite3.connect

        def connected(*args, **kwargs):
            """Observe executed SQL without replacing storage semantics."""

            connection = original(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch("harnest.provisioner_history.sqlite3.connect", side_effect=connected):
            page = self.service.history(limit=2)
        self.assertEqual([row["revision"] for row in page["revisions"]], [3, 2])
        self.assertEqual(page["next_before"], 2)
        selects = [sql for sql in queries if "FROM revisions" in sql]
        self.assertEqual(len(selects), 1)
        self.assertNotIn("snapshot", selects[0])
        self.assertIn("ORDER BY revision DESC LIMIT 3", selects[0])
        page = self.service.history(limit=2, before=2)
        self.assertEqual([row["revision"] for row in page["revisions"]], [1])
        self.assertIsNone(page["next_before"])

    def test_legacy_state_is_imported_without_fabricating_revision_history(self):
        """Upgrading preserves cleanup ownership but does not invent an unknown snapshot."""

        self.service.directory.mkdir(parents=True)
        previous = {"status": "ready", "resources": {}, "summary": self.service.plan()}
        (self.service.directory / "state.json").write_text(json.dumps(previous))
        self.assertEqual(self.service._read(), previous)
        self.assertEqual(self.service.history()["revisions"], [])
        self.assertEqual(self.service.apply(self.secrets)["active_revision"], 1)

    def test_crash_recovery_keeps_attempt_and_active_pointers_distinct(self):
        """A process that dies after recording intent cannot leave a falsely successful revision."""

        self.service.apply(self.secrets)
        store = RevisionStore(self.service.directory)
        with self.service._locked():
            plan = self.service._plan()
            store.begin(plan, self.service._read(), "apply", {}, None)
        history = self.service.history()
        self.assertEqual(history["revisions"][0]["status"], "interrupted")
        self.assertEqual(history["active_revision"], 1)
        with self.assertRaisesRegex(ProvisionError, "successfully"):
            self.service.rollback(2, self.secrets)

    def test_snapshot_and_revision_outcome_commit_atomically(self):
        """A failed commit cannot update the active pointer while leaving the revision pending."""

        self.service.apply(self.secrets)
        store = RevisionStore(self.service.directory)
        with store.connection() as connection, connection:
            connection.execute("""CREATE TRIGGER reject_success BEFORE UPDATE ON revisions
                WHEN NEW.revision = 2 AND NEW.status = 'succeeded'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(ProvisionError):
            self.service.apply(self.secrets)
        history = self.service.history()
        self.assertEqual(history["active_revision"], 1)
        self.assertEqual(history["revisions"][0]["status"], "failed")

    def test_modified_snapshot_is_rejected_before_backend_calls(self):
        """Stored fingerprints detect a changed snapshot before rollback can mutate resources."""

        self.service.apply(self.secrets)
        store = RevisionStore(self.service.directory)
        with store.connection() as connection, connection:
            connection.execute("UPDATE revisions SET snapshot = snapshot || ' ' WHERE revision = 1")
        before = len(self.backend.calls)
        with self.assertRaisesRegex(ProvisionError, "fingerprint"):
            self.service.rollback(1, self.secrets)
        self.assertEqual(len(self.backend.calls), before)

    def test_target_changes_and_missing_secrets_do_not_create_rollback_attempts(self):
        """Rollback never bypasses recorded target or credential checks."""

        self.service.apply(self.secrets)
        with self.assertRaisesRegex(ProvisionError, "Missing required secret"):
            self.service.rollback(1, {})
        self.assertEqual(len(self.service.history()["revisions"]), 1)
        self.service.control("remove")
        self.edit(lambda document: document.update(name="other"))
        self.service.apply(self.secrets)
        with self.assertRaisesRegex(ProvisionError, "previous deployment"):
            self.service.rollback(1, self.secrets)


class ImagePinningTests(unittest.TestCase):
    """Pinning must preserve local development and immutable registry references."""

    def test_digest_references_require_no_registry_lookup(self):
        """An explicit registry digest works even without Docker buildx installed."""

        image = "registry.test/agent@sha256:" + "a" * 64
        def forbidden(*args):
            """Fail if a pinned reference unnecessarily contacts an image backend."""
            raise AssertionError("unexpected lookup")
        self.assertEqual(pin_image(image, "kubernetes", forbidden), image)

    def test_local_missing_image_is_pulled_then_pinned(self):
        """Resolve the exact local image created by a successful pull."""

        calls = []
        def runner(argv, payload):
            """Fail the first inspect, as Docker does for an absent local image."""
            calls.append(argv)
            if len(calls) == 1:
                raise ProvisionError("missing")
            return json.dumps("sha256:" + "a" * 64)
        self.assertEqual(pin_image("redis:7.4", "local", runner), "sha256:" + "a" * 64)
        self.assertEqual(calls[1], ["docker", "pull", "redis:7.4"])

    def test_registry_resolution_keeps_multi_platform_manifest_digest(self):
        """Kubernetes receives a registry-qualified digest, not a local config ID."""

        backend = FakeBackend()
        result = pin_image("registry.test/agent:v1", "kubernetes", backend)
        self.assertTrue(result.startswith("registry.test/agent:v1@sha256:"))
        self.assertIn("imagetools", backend.calls[0][0])
        with self.assertRaises(ProvisionError):
            pin_image("sha256:" + "a" * 64, "kubernetes", backend)
        with self.assertRaises(ProvisionError):
            pin_image("registry.test/agent@sha256:invalid", "kubernetes", backend)

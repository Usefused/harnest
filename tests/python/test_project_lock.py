"""Verify exact framework pins across environment resolution, compilation, and upgrade."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from harnest.compatibility import FrameworkCompatibilityError, ResolvedFrameworkCompatibility
from harnest.project_lock import read_project_lock, resolve_project_lock, verify_framework_lock
from harnest.upgrade import _upgraded_project_lock


RESOLVED = ResolvedFrameworkCompatibility("0.11.0", "langgraph", "langgraph", "1.2.11", ">=1.2,<2")


class ProjectLockTests(unittest.TestCase):
    def test_resolution_preserves_metadata_and_enforces_exact_version(self):
        """Repeated sync is stable and future compiles cannot silently drift."""
        with tempfile.TemporaryDirectory() as directory, patch("harnest.project_lock.validate_framework_compatibility", return_value=RESOLVED):
            root = Path(directory)
            value = read_project_lock(root)
            value["annotation"] = "keep"
            (root / "harnest.lock").write_text(yaml.safe_dump(value))
            resolve_project_lock(root, "langgraph")
            contents = (root / "harnest.lock").read_text()
            resolve_project_lock(root, "langgraph", frozen=True)
            self.assertEqual(contents, (root / "harnest.lock").read_text())
            self.assertEqual(read_project_lock(root)["annotation"], "keep")
            verify_framework_lock(root, RESOLVED)
            with self.assertRaisesRegex(FrameworkCompatibilityError, "differs"):
                verify_framework_lock(root, replace(RESOLVED, distribution_version="1.2.12"))

    def test_frozen_requires_pin_and_framework_switch_is_explicit(self):
        """Frozen sync cannot create or change a pin; normal sync permits a selected backend switch."""
        with tempfile.TemporaryDirectory() as directory, patch("harnest.project_lock.validate_framework_compatibility", return_value=RESOLVED) as resolve:
            root = Path(directory)
            with self.assertRaises(FrameworkCompatibilityError):
                resolve_project_lock(root, "langgraph", frozen=True)
            self.assertFalse((root / "harnest.lock").exists())
            resolve_project_lock(root, "langgraph")
            resolve.return_value = replace(RESOLVED, framework="adk", distribution="google-adk", distribution_version="2.8.0")
            with self.assertRaises(FrameworkCompatibilityError):
                resolve_project_lock(root, "adk", frozen=True)
            resolve_project_lock(root, "adk")
            self.assertEqual(read_project_lock(root)["framework"]["name"], "adk")

    def test_upgrade_keeps_framework_pin(self):
        """Schema migration must not silently discard a framework compatibility decision."""
        with tempfile.TemporaryDirectory() as directory, patch("harnest.project_lock.validate_framework_compatibility", return_value=RESOLVED):
            root = Path(directory)
            resolve_project_lock(root, "langgraph")
            value = read_project_lock(root)
            value["projectSchema"] = 2
            path = root / "harnest.lock"
            path.write_text(yaml.safe_dump(value))
            upgraded = yaml.safe_load(_upgraded_project_lock(path))
            self.assertEqual(upgraded["projectSchema"], 3)
            self.assertEqual(upgraded["framework"], value["framework"])

    def test_compiler_checks_pin_before_importing_authored_code(self):
        """The compile boundary fails on drift before code with side effects can run."""
        from harnest.bundle import BundleImportError, compile_application

        with tempfile.TemporaryDirectory() as directory, patch("harnest.project_lock.validate_framework_compatibility", return_value=RESOLVED):
            root = Path(directory)
            resolve_project_lock(root, "langgraph")
            (root / "agent.py").write_text("raise AssertionError('authored code executed')")
            with patch("harnest.bundle.validate_framework_compatibility", return_value=replace(RESOLVED, distribution_version="1.2.12")):
                with self.assertRaisesRegex(BundleImportError, "differs from harnest.lock"):
                    compile_application(root, entrypoint="agent:root_agent", framework="langgraph")

    def test_shared_evaluator_rejects_an_unsupported_adk_version(self):
        """LangGraph evaluation cannot bypass the shared evaluator compatibility gate."""
        from harnest.evaluation import EvaluationError, eval_dependencies

        with patch("harnest.compatibility.validate_framework_compatibility", side_effect=FrameworkCompatibilityError("unsupported evaluator")):
            with self.assertRaisesRegex(EvaluationError, "unsupported evaluator"):
                eval_dependencies()

    def test_pin_rejects_specifiers_wrong_packages_and_symlinks(self):
        """Never treat lock content as arbitrary installer arguments or follow links."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = read_project_lock(root)
            for version in (">=1.2", "https://example.com", 1.2, "--index-url=other"):
                value["framework"] = {"name": "langgraph", "distribution": "langgraph", "version": version}
                (root / "harnest.lock").write_text(yaml.safe_dump(value))
                with self.subTest(version=version), self.assertRaises(FrameworkCompatibilityError):
                    verify_framework_lock(root, RESOLVED)
            (root / "harnest.lock").unlink()
            (root / "harnest.lock").symlink_to(root / "missing")
            with self.assertRaises(FrameworkCompatibilityError):
                read_project_lock(root)

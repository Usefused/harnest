from __future__ import annotations

from contextlib import contextmanager
import tempfile
from pathlib import Path
import unittest

from scripts.check_python_complexity import _violations
from scripts.check_skill_quality import _violations as _skill_violations
from scripts.run_python_tests import TestSuiteManifestError, load_manifest, validate_manifest
from _test_context import enter_context


class FixtureContextTests(unittest.TestCase):
    """Keep setup resources owned correctly on Python 3.10 and newer runners."""

    def test_failed_setup_releases_contexts_in_cleanup_order(self):
        """Fixture failure still unwinds contexts in order with ordinary callbacks."""
        events = []
        value = object()

        @contextmanager
        def resource(name):
            """Record release independently from the test body's success."""
            try:
                yield value
            finally:
                events.append(name)

        class FailingSetup(unittest.TestCase):
            def setUp(self):
                """Acquire interleaved resources before simulating later setup failure."""
                self.assertIs(enter_context(self, resource("first")), value)
                self.addCleanup(events.append, "callback")
                enter_context(self, resource("second"))
                raise RuntimeError("setup failed")

            def runTest(self):
                """The failed setup must prevent the body from running."""
                events.append("body")

        result = unittest.TestResult()
        FailingSetup().run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(events, ["second", "callback", "first"])

    def test_failed_entry_does_not_register_an_exit(self):
        """Only successfully entered resources are released during test cleanup."""
        events = []

        class FailingResource:
            def __enter__(self):
                """Simulate startup failing before the resource has been acquired."""
                raise RuntimeError("entry failed")

            def __exit__(self, *args):
                """Record an invalid release if the fixture calls this on failed entry."""
                events.append("exit")

        owner = unittest.TestCase()
        with self.assertRaisesRegex(RuntimeError, "entry failed"):
            enter_context(owner, FailingResource())
        owner.doCleanups()
        self.assertEqual(events, [])


class PythonComplexityGateTests(unittest.TestCase):
    def _source(self, contents: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "sample.py"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_reports_functions_and_methods_above_the_limit_once(self):
        cases = (
            (
                "def too_complex(value):\n{branches}\n",
                "too_complex has complexity 11 (max 10)",
                "    ",
            ),
            (
                "class Example:\n    def too_complex(self, value):\n{branches}\n",
                "Example.too_complex",
                "        ",
            ),
        )
        for template, expected, indent in cases:
            with self.subTest(expected=expected):
                branches = "\n".join(
                    f"{indent}if value == {index}: pass" for index in range(10)
                )
                path = self._source(template.format(branches=branches))

                violations = _violations([path], 10)

                self.assertEqual(len(violations), 1)
                self.assertIn(expected, violations[0])

    def test_checks_methods_instead_of_class_aggregate(self):
        path = self._source(
            "class Example:\n"
            "    def first(self, value):\n"
            "        if value: return 1\n"
            "        return 0\n"
            "    def second(self, value):\n"
            "        if value: return 1\n"
            "        return 0\n"
        )

        self.assertEqual(_violations([path], 10), [])


class SkillQualityGateTests(unittest.TestCase):
    def _skill(self, words: int) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "SKILL.md"
        path.write_text("word " * words, encoding="utf-8")
        return path

    def test_accepts_skill_at_word_limit(self):
        self.assertEqual(_skill_violations([self._skill(400)], 400), [])

    def test_reports_skill_above_word_limit(self):
        path = self._skill(401)

        self.assertEqual(
            _skill_violations([path], 400),
            [f"{path}: 401 words (max 400)"],
        )

    def test_ignores_generated_artifact_skills(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        generated = Path(directory.name) / ".harnest" / "SKILL.md"
        generated.parent.mkdir()
        generated.write_text("word " * 401, encoding="utf-8")

        self.assertEqual(_skill_violations([Path(directory.name)], 400), [])

    def test_ignores_cached_runtime_dependency_skills(self):
        """Shared runtimes must not apply this repository's word ceiling to dependencies."""
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / ".cache" / "runtime" / "dependency" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("word " * 401)
            self.assertEqual(_skill_violations([Path(directory)], 400), [])

    def test_ignores_installed_node_package_skills(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        installed = (
            Path(directory.name) / "studio" / "node_modules" / "package" / "SKILL.md"
        )
        installed.parent.mkdir(parents=True)
        installed.write_text("word " * 401, encoding="utf-8")

        self.assertEqual(_skill_violations([Path(directory.name)], 400), [])


class TestSuiteManifestTests(unittest.TestCase):
    def test_repository_manifest_classifies_every_python_test_module(self):
        assignments = validate_manifest(load_manifest())

        self.assertIn("test_quality_gate", assignments)
        self.assertEqual(assignments["test_neutral_runtime"], "e2e")

    def test_manifest_rejects_duplicate_modules(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "test_one.py").write_text("", encoding="utf-8")
        (root / "test_two.py").write_text("", encoding="utf-8")
        manifest = {
            "modules": {
                "unit": ["test_one"],
                "integration": ["test_one"],
                "e2e": [],
                "live": [],
            }
        }

        with self.assertRaisesRegex(TestSuiteManifestError, "multiple tiers"):
            validate_manifest(manifest, root)

    def test_manifest_rejects_unclassified_modules(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "test_one.py").write_text("", encoding="utf-8")
        manifest = {
            "modules": {
                "unit": [],
                "integration": [],
                "e2e": [],
                "live": [],
            }
        }

        with self.assertRaisesRegex(TestSuiteManifestError, "unclassified: test_one"):
            validate_manifest(manifest, root)

if __name__ == "__main__":
    unittest.main()

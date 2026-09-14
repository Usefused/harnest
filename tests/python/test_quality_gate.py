from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scripts.check_python_complexity import _violations
from scripts.check_skill_quality import _violations as _skill_violations


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

if __name__ == "__main__":
    unittest.main()

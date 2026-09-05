from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scripts.check_python_complexity import _violations


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

if __name__ == "__main__":
    unittest.main()

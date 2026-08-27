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

    def test_reports_a_function_above_the_limit(self):
        branches = "\n".join(f"    if value == {index}: pass" for index in range(10))
        path = self._source(f"def too_complex(value):\n{branches}\n")

        violations = _violations([path], 10)

        self.assertEqual(len(violations), 1)
        self.assertIn("too_complex has complexity 11 (max 10)", violations[0])

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

    def test_reports_a_complex_method_once(self):
        branches = "\n".join(
            f"        if value == {index}: pass" for index in range(10)
        )
        path = self._source(
            f"class Example:\n    def too_complex(self, value):\n{branches}\n"
        )

        violations = _violations([path], 10)

        self.assertEqual(len(violations), 1)
        self.assertIn("Example.too_complex", violations[0])


if __name__ == "__main__":
    unittest.main()

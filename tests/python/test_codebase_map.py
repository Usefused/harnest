"""The generated file map in docs/codebase/files.md stays complete and current."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codebase_map.py"


def _load():
    spec = importlib.util.spec_from_file_location("codebase_map", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


class CodebaseMapTests(unittest.TestCase):
    def test_every_source_file_is_described_and_the_map_is_current(self):
        # Fix by adding a module docstring (Python) or a comment above the
        # package clause (Go), then run: python scripts/codebase_map.py
        self.assertEqual(_load().problems(), [])

    def test_archive_discovery_includes_nested_sources(self):
        codebase_map = _load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "src/harnest/frameworks/example.py"
            nested.parent.mkdir(parents=True)
            nested.write_text('"""Example adapter."""\n')
            with patch.object(codebase_map, "ROOT", root), patch.object(
                codebase_map.subprocess, "run", side_effect=OSError("git unavailable")
            ):
                self.assertEqual(codebase_map.tracked_files(["src/harnest/*.py"]),
                                 ["src/harnest/frameworks/example.py"])

    def test_deps_resolves_relative_and_lazy_imports(self):
        graph = _load().import_graph()
        self.assertIn("harnest.frameworks.base", graph["harnest.frameworks.registry"])
        self.assertIn("harnest.frameworks.registry", graph["harnest.bundle"])


if __name__ == "__main__":
    unittest.main()

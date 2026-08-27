from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scripts.check_skill_quality import _violations


class SkillQualityGateTests(unittest.TestCase):
    def _skill(self, words: int) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "SKILL.md"
        path.write_text("word " * words, encoding="utf-8")
        return path

    def test_accepts_skill_at_word_limit(self):
        self.assertEqual(_violations([self._skill(400)], 400), [])

    def test_reports_skill_above_word_limit(self):
        path = self._skill(401)

        self.assertEqual(
            _violations([path], 400),
            [f"{path}: 401 words (max 400)"],
        )

    def test_ignores_generated_artifact_skills(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        generated = Path(directory.name) / ".harnest" / "SKILL.md"
        generated.parent.mkdir()
        generated.write_text("word " * 401, encoding="utf-8")

        self.assertEqual(_violations([Path(directory.name)], 400), [])


if __name__ == "__main__":
    unittest.main()

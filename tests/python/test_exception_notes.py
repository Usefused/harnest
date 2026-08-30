from __future__ import annotations

import unittest

from harnest._exception_notes import add_exception_note


class ExceptionNoteTests(unittest.TestCase):
    def test_notes_are_inspectable_on_every_supported_python(self) -> None:
        """Preserve cleanup diagnostics even when add_note is unavailable."""

        failure = RuntimeError("primary")

        add_exception_note(failure, "first cleanup failed")
        add_exception_note(failure, "second cleanup failed")

        self.assertEqual(
            getattr(failure, "__notes__", None),
            ["first cleanup failed", "second cleanup failed"],
        )


if __name__ == "__main__":
    unittest.main()

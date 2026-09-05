import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.finalize_release_changelog import (
    ChangelogError,
    finalize_changelog,
    main,
    validate_release_changelog,
)


class ReleaseChangelogTests(unittest.TestCase):
    def test_authored_notes_move_ahead_of_generated_notes(self):
        source = textwrap.dedent(
            """\
            # Changelog

            ## Unreleased

            ### Runtime architecture

            * Added portable lifecycle hooks.

            ## [0.5.0](https://github.com/Usefused/harnest/compare/v0.4.1...v0.5.0) (2026-09-01)

            ### Features

            * add runtime plugins

            ## [0.4.1](https://example.test/0.4.1) (2026-08-29)
            """
        )

        updated = finalize_changelog(source, "0.5.0")

        self.assertNotIn("## Unreleased", updated)
        self.assertLess(
            updated.index("### Runtime architecture"),
            updated.index("### Features"),
        )
        self.assertEqual(updated.count("## [0.5.0]"), 1)
        validate_release_changelog(updated, "0.5.0")

    def test_empty_unreleased_placeholder_is_removed(self):
        source = "# Changelog\n\n## Unreleased\n\n## [1.2.3](https://example.test)\n\n* Fix\n"

        self.assertEqual(
            finalize_changelog(source, "1.2.3"),
            "# Changelog\n\n## [1.2.3](https://example.test)\n\n* Fix\n",
        )

    def test_changelog_without_authored_notes_is_unchanged(self):
        source = "# Changelog\n\n## [1.2.3](https://example.test)\n"

        self.assertEqual(finalize_changelog(source, "1.2.3"), source)

    def test_unreleased_cannot_cross_published_history(self):
        source = (
            "# Changelog\n\n## Unreleased\n\n* New\n\n"
            "## [1.2.2](https://example.test)\n\n* Old\n\n"
            "## [1.2.3](https://example.test)\n"
        )

        with self.assertRaisesRegex(ChangelogError, "immediately followed"):
            finalize_changelog(source, "1.2.3")

    def test_release_validation_rejects_invalid_version_headings(self):
        cases = (
            (
                "# Changelog\n\n## Unreleased\n\n"
                "## [1.2.3](https://example.test)\n",
                "must match tag version",
            ),
            (
                "# Changelog\n\n## [1.2.3](https://example.test/new)\n\n"
                "## [1.2.3](https://example.test/duplicate)\n",
                "exactly one",
            ),
        )
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ChangelogError, message
            ):
                validate_release_changelog(source, "1.2.3")

    def test_command_derives_release_please_version_and_writes_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changelog = root / "CHANGELOG.md"
            pyproject = root / "pyproject.toml"
            changelog.write_text(
                "# Changelog\n\n## Unreleased\n\n* New\n\n## [2.0.0](https://example.test)\n",
                encoding="utf-8",
            )
            pyproject.write_text('[project]\nname = "harnest"\nversion = "2.0.0"\n')

            result = main(
                ["--changelog", str(changelog), "--pyproject", str(pyproject)]
            )

            self.assertEqual(result, 0)
            self.assertNotIn("Unreleased", changelog.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

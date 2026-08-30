#!/usr/bin/env python3
"""Fold authored Unreleased notes into a Release Please version section."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the maintained compatibility package.
    import tomli as tomllib


H2_PATTERN = re.compile(r"^## .+$", re.MULTILINE)
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?$")


class ChangelogError(ValueError):
    """The changelog cannot be finalized without risking release history."""


def _unreleased_index(headings: list[re.Match[str]]) -> int | None:
    """Return the unique Unreleased heading index, if authored notes exist."""
    matches = [
        index
        for index, heading in enumerate(headings)
        if heading.group(0) == "## Unreleased"
    ]
    if len(matches) > 1:
        raise ChangelogError("CHANGELOG.md contains more than one Unreleased section")
    return matches[0] if matches else None


def _matches_version_heading(heading: str, version: str) -> bool:
    """Recognize Release Please's linked heading and a plain compatible form."""
    if heading.startswith(f"## [{version}]"):
        return True
    return re.match(rf"^## {re.escape(version)}(?:\s|\(|$)", heading) is not None


def _version_index(headings: list[re.Match[str]], version: str) -> int:
    """Return one exact release heading so duplicate history fails closed."""
    matches = [
        index
        for index, heading in enumerate(headings)
        if _matches_version_heading(heading.group(0), version)
    ]
    if len(matches) != 1:
        raise ChangelogError(
            f"expected exactly one changelog heading for release {version}, found {len(matches)}"
        )
    return matches[0]


def finalize_changelog(source: str, version: str) -> str:
    """Move authored notes into the adjacent generated release section."""
    headings = list(H2_PATTERN.finditer(source))
    unreleased = _unreleased_index(headings)
    if unreleased is None:
        return source

    release = _version_index(headings, version)
    # Only an adjacent generated release may consume these notes; crossing an
    # older release boundary would silently rewrite published history.
    if release != unreleased + 1:
        raise ChangelogError(
            f"Unreleased must be immediately followed by release {version}"
        )

    unreleased_heading = headings[unreleased]
    release_heading = headings[release]
    authored = source[unreleased_heading.end() : release_heading.start()].strip()
    prefix = source[: unreleased_heading.start()].rstrip()
    release_body = source[release_heading.end() :].lstrip("\n")

    sections = [prefix, release_heading.group(0)]
    if authored:
        sections.append(authored)
    if release_body:
        sections.append(release_body.rstrip())
    return "\n\n".join(sections) + "\n"


def validate_release_changelog(source: str, version: str) -> None:
    """Require the tagged version to be the first changelog release."""
    headings = list(H2_PATTERN.finditer(source))
    release = _version_index(headings, version)
    if release != 0 or _unreleased_index(headings) is not None:
        raise ChangelogError(
            f"the first changelog release must match tag version {version}"
        )


def _project_version(path: Path) -> str:
    """Read the proposed version already written by Release Please."""
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    version = str(project["version"])
    if not SEMVER_PATTERN.fullmatch(version):
        raise ChangelogError(
            f"project version {version!r} is not supported semantic versioning"
        )
    return version


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    """Parse paths explicitly so the script is testable outside repository root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--version")
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Finalize a proposal or validate that an immutable tag is safe to package."""
    arguments = _arguments(argv)
    try:
        version = arguments.version or _project_version(arguments.pyproject)
        source = arguments.changelog.read_text(encoding="utf-8")
        if arguments.check:
            validate_release_changelog(source, version)
            return 0
        updated = finalize_changelog(source, version)
        if updated != source:
            arguments.changelog.write_text(updated, encoding="utf-8")
        return 0
    except (ChangelogError, KeyError, OSError, tomllib.TOMLDecodeError) as error:
        print(f"release changelog error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

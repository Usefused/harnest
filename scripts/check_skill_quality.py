#!/usr/bin/env python3
"""Enforce concise SKILL.md entrypoints across the repository."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from pathlib import Path


_IGNORED_DIRECTORIES = {
    ".git",
    ".harnest",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


def _skill_files(paths: Iterable[Path]) -> Iterator[Path]:
    for candidate in paths:
        if candidate.is_file() and candidate.name == "SKILL.md":
            yield candidate
        elif candidate.is_dir():
            yield from (
                skill
                for skill in sorted(candidate.rglob("SKILL.md"))
                if not set(skill.parts) & _IGNORED_DIRECTORIES
            )


def _violations(paths: Iterable[Path], limit: int) -> list[str]:
    violations = []
    for skill in _skill_files(paths):
        words = len(skill.read_text(encoding="utf-8").split())
        if words > limit:
            violations.append(f"{skill}: {words} words (max {limit})")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--max-words", type=int, default=400)
    args = parser.parse_args()
    if args.max_words < 1:
        parser.error("--max-words must be at least one")
    violations = _violations(args.paths, args.max_words)
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

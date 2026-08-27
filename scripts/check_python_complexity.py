#!/usr/bin/env python3
"""Fail when a Python function or method exceeds the complexity budget."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit


def _python_files(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.py"))


def _functions(blocks: Iterable[Any]) -> Iterator[Any]:
    for block in blocks:
        methods = getattr(block, "methods", ())
        if methods:
            yield from _functions(methods)
            continue
        yield block
        yield from _functions(getattr(block, "closures", ()))


def _violations(paths: Iterable[Path], limit: int) -> list[str]:
    violations: list[str] = []
    for path in _python_files(paths):
        blocks = cc_visit(path.read_text(encoding="utf-8"))
        seen: set[tuple[int, str]] = set()
        for block in _functions(blocks):
            identity = (block.lineno, block.fullname)
            if identity in seen:
                continue
            seen.add(identity)
            if block.complexity > limit:
                violations.append(
                    f"{path}:{block.lineno}: {block.fullname} "
                    f"has complexity {block.complexity} (max {limit})"
                )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--max", type=int, default=10, dest="limit")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--max must be at least one")
    violations = _violations(args.paths, args.limit)
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared policy for authored files excluded from compiled source artifacts."""

from __future__ import annotations

from pathlib import Path


IGNORED_SOURCE_DIRECTORIES = frozenset(
    {
        ".adk",
        ".git",
        ".harnest",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "venv",
    }
)


def ignored_source_path(relative: Path) -> bool:
    """Exclude generated state and local secrets from every source identity."""

    if any(part in IGNORED_SOURCE_DIRECTORIES for part in relative.parts):
        return True
    return relative.name == ".env" or relative.name.startswith(".env.")


__all__ = ["IGNORED_SOURCE_DIRECTORIES", "ignored_source_path"]

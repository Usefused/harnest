"""Shared support for sanitized exception diagnostics."""

from __future__ import annotations


def add_exception_note(error: BaseException, note: str) -> None:
    """Attach a diagnostic note on every supported Python version."""

    error.add_note(note)


__all__ = ["add_exception_note"]

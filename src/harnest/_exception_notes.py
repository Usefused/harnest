"""Shared support for sanitized exception diagnostics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


def add_exception_note(error: BaseException, note: str) -> None:
    """Attach a diagnostic note on every supported Python version."""

    error.add_note(note)


async def capture_cleanup_failure(callback: Callable[[], Awaitable[Any]]) -> BaseException | None:
    """Capture cancellation and cleanup errors so remaining owners still close."""
    try:
        await callback()
    except BaseException as error:
        return error
    return None


def merge_cleanup_failure(
    primary: BaseException | None, cleanup: BaseException | None, *, label: str
) -> BaseException | None:
    """Retain the first failure and attach only the later exception's type."""
    if cleanup is None:
        return primary
    if primary is None:
        return cleanup
    add_exception_note(primary, f"{label} cleanup also failed with {type(cleanup).__name__}")
    return primary


__all__ = ["add_exception_note"]

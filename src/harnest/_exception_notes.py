"""Cross-version support for sanitized exception diagnostics."""

from __future__ import annotations


def add_exception_note(error: BaseException, note: str) -> None:
    """Attach a diagnostic note on every supported Python version."""

    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)
        return
    # Python 3.10 has no BaseException.add_note, but retaining the conventional
    # attribute keeps diagnostics inspectable without changing the exception.
    notes = getattr(error, "__notes__", None)
    if notes is None:
        error.__notes__ = [note]
    else:
        notes.append(note)


__all__ = ["add_exception_note"]

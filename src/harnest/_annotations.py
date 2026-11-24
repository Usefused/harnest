"""PEP 649-safe annotation helpers for Harnest tool and runtime wrappers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

try:
    from annotationlib import Format as _AnnotationFormat
except ImportError:  # pragma: no cover - defensive fallback before Python 3.14
    _AnnotationFormat = None


def set_callable_annotations(
    target: Callable[..., Any],
    annotations: Mapping[str, Any],
) -> None:
    """Attach explicit annotations via __annotate__ so functools.wraps keeps them."""

    copied = dict(annotations)
    # Before PEP 649, functools.wraps copies __annotations__ directly.
    if _AnnotationFormat is None:
        target.__annotations__ = copied
        return
    supported = (_AnnotationFormat.VALUE, _AnnotationFormat.FORWARDREF)

    def _annotate(fmt: int) -> dict[str, Any]:
        """Return explicit annotations for value and forward-reference formats."""

        # Raising NotImplementedError for STRING lets annotationlib derive
        # stringified annotations from VALUE without duplicating formatter logic.
        if fmt in supported:
            return dict(copied)
        raise NotImplementedError

    # On Python 3.14 (PEP 649), functools.wraps copies __annotate__ instead of
    # __annotations__, so assigning __annotations__ directly drops types on
    # outer wrappers.
    target.__annotate__ = _annotate  # type: ignore[attr-defined]


def preserve_wrapped_annotations(
    wrapped: Callable[..., Any],
    original: Callable[..., Any],
) -> None:
    """Preserve explicit __annotations__ when the wrapped callable cleared __annotate__."""

    # If the wrapped callable assigned __annotations__ directly (setting
    # __annotate__ to None), functools.wraps copied None; re-publish its
    # annotations through __annotate__ so type inspection stays intact.
    if getattr(original, "__annotate__", None) is None:
        existing = getattr(original, "__annotations__", None)
        if existing:
            set_callable_annotations(wrapped, existing)

"""Compiler-owned framework selection for argument-free authoring helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .base import FrameworkAdapter
from .registry import get_framework


@dataclass(slots=True)
class _Selection:
    """Revoke copied contexts when the compiler that owns them has finished."""

    adapter: FrameworkAdapter
    active: bool = True


_SELECTED: ContextVar[_Selection | None] = ContextVar(
    "harnest_selected_framework", default=None
)


@contextmanager
def framework_scope(framework: str) -> Iterator[FrameworkAdapter]:
    """Bind compiler selection locally and restore it after nested work or failure."""

    adapter = get_framework(framework)
    selection = _Selection(adapter)
    token = _SELECTED.set(selection)
    try:
        yield adapter
    finally:
        selection.active = False
        _SELECTED.reset(token)


def current_framework() -> FrameworkAdapter:
    """Resolve Harnest's compile or invocation selection without guessing a default."""

    selection = _SELECTED.get()
    if selection is not None and selection.active:
        return selection.adapter
    from ..context import optional_active_context

    invocation = optional_active_context()
    if invocation is not None:
        return get_framework(invocation.framework)
    raise RuntimeError(
        "Framework construction requires Harnest compilation or an active invocation. "
        "Define agents, graphs, and model connectors in your project and let Harnest "
        "build them using the framework selected in config.yaml."
    )

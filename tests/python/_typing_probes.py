"""Generate static consumer probes from the reviewed public API contract."""

from __future__ import annotations

from collections.abc import Callable
import importlib
import inspect
from typing import Any, get_origin


_SPECIAL_METHODS = {"__call__", "__getitem__", "__enter__", "__exit__", "__iter__", "__aiter__", "__aenter__", "__aexit__"}


def _visible_method(name: str, descriptor: Any) -> bool:
    """Include useful Python protocols as well as named authoring methods."""
    visible = not name.startswith("_") or name in _SPECIAL_METHODS
    return visible and (inspect.isfunction(descriptor) or isinstance(descriptor, (property, classmethod, staticmethod)))


def _members(cls: type) -> list[str]:
    """Follow Harnest inheritance without imposing contracts on external SDKs."""
    members: set[str] = set()
    for base in cls.__mro__:
        if not base.__module__.startswith("harnest"):
            continue
        members.update(name for name in getattr(base, "__annotations__", {}) if not name.startswith("_"))
        members.update(name for name, value in base.__dict__.items() if _visible_method(name, value))
    return sorted(members)


def _specialization(value: Any) -> str:
    """Use an explicit callable for callable-bound generic parameters."""
    parameters = getattr(value, "__parameters__", ()) if inspect.isclass(value) else ()
    arguments = ["Callable[..., Any]" if get_origin(getattr(parameter, "__bound__", None)) is Callable
                 else "Any" for parameter in parameters]
    return "[" + ", ".join(arguments) + "]" if arguments else ""


def _instance_probes(symbol: str, value: Any, index: int) -> list[str]:
    """Specialize generic contracts so unconstrained parameters do not mask gaps."""
    cls = value if inspect.isclass(value) else type(value)
    members = _members(cls)
    if not members or inspect.isfunction(value):
        return []
    if inspect.isclass(value):
        annotation = symbol + _specialization(value)
        header, target = f"def probe_{index}(value: {annotation}) -> None:", "value"
    else:
        header, target = f"def probe_{index}() -> None:", symbol
    lines = [header]
    for name in members:
        descriptor = inspect.getattr_static(cls, name, None)
        marker = "  # callable-contract" if inspect.isroutine(descriptor) or isinstance(descriptor, (staticmethod, classmethod)) else ""
        lines.append(f"    reveal_type({target}.{name}){marker}")
    return lines


def public_api_probes(snapshot: dict[str, list[str]]) -> str:
    """Exercise every public import and Harnest-authored class member statically."""
    lines = ["from typing import Any, Callable, reveal_type"]
    for index, (name, exports) in enumerate(snapshot.items()):
        module = importlib.import_module(name)
        alias = f"m{index}"
        lines.append(f"import {name} as {alias}")
        for export in exports:
            value = getattr(module, export)
            symbol = f"{alias}.{export}"
            specialization = _specialization(value)
            marker = "  # callable-contract" if inspect.isfunction(value) or inspect.isclass(value) else ""
            lines.append(f"reveal_type({symbol}{specialization}){marker}")
            if getattr(value, "__module__", "").startswith("harnest"):
                lines.extend(_instance_probes(symbol, value, len(lines)))
    return "\n".join(lines) + "\n"

"""Shared strict tool-argument policy for managed framework adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import inspect
from typing import Any


def callable_argument_names(
    function: Any, *, ignored: Iterable[str] = ()
) -> frozenset[str]:
    """Return keyword names a framework may pass to one callable."""

    omitted = frozenset(ignored)
    variadic = {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    return frozenset(
        name
        for name, parameter in inspect.signature(function).parameters.items()
        if name not in omitted and parameter.kind not in variadic
    )


def unknown_argument_error(
    tool_name: str,
    arguments: Mapping[str, Any] | Iterable[str],
    allowed: frozenset[str] | None,
) -> str | None:
    """Reject undeclared names without retaining or echoing argument values."""

    if allowed is None:
        return None
    names = arguments.keys() if isinstance(arguments, Mapping) else arguments
    unknown = sorted(str(name) for name in names if name not in allowed)
    if not unknown:
        return None
    provided = ", ".join(unknown)
    expected = (
        "Allowed parameters: " + ", ".join(sorted(allowed)) + "."
        if allowed
        else "This tool accepts no parameters."
    )
    return (
        f"Invoking `{tool_name}()` rejected unknown input parameters: "
        f"{provided}. {expected} Retry using only declared parameters."
    )


def invalid_argument_error(tool_name: str, allowed: frozenset[str]) -> str:
    """Return privacy-safe repair guidance for non-extra validation failures."""

    expected = ", ".join(sorted(allowed)) or "none"
    return (
        f"Invoking `{tool_name}()` rejected invalid input. "
        f"Allowed parameters: {expected}. Retry with values matching the schema."
    )


__all__ = [
    "callable_argument_names",
    "invalid_argument_error",
    "unknown_argument_error",
]

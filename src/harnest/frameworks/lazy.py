"""Lazy public exports for framework packages, so importing one never imports its SDK."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import Any


def lazy_exports(
    package: str, exports: Mapping[str, str]
) -> tuple[Callable[[str], Any], Callable[[], list[str]]]:
    """Return a package's PEP 562 ``__getattr__`` and ``__dir__``.

    ``exports`` maps each public name to the relative module that defines it.
    A loaded name is cached on the package, so each loads once.
    """

    def __getattr__(name: str) -> Any:
        """Import and cache a requested public export without loading other SDKs."""
        module = exports.get(name)
        if module is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        value = getattr(import_module(module, package), name)
        setattr(sys.modules[package], name, value)
        return value

    def __dir__() -> list[str]:
        """Include unloaded exports in package introspection."""
        return sorted(set(vars(sys.modules[package])) | set(exports))

    return __getattr__, __dir__

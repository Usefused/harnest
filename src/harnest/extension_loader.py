"""Filesystem discovery for runtime extensions.

An extension is a directory, not an aggregation object. ``lifecycle.py`` is
portable. ``adk.py`` and ``langgraph.py`` are optional native integrations and
only the selected backend's module is imported.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any
import uuid

from .extension import Extension


_FILES = frozenset({"lifecycle.py", "adk.py", "langgraph.py"})
_IGNORED = frozenset({"__init__.py", ".DS_Store"})


class ExtensionDiscoveryError(ValueError):
    """An extension directory or export does not follow the contract."""


@dataclass(frozen=True, slots=True)
class DiscoveredExtension:
    """Portable and selected native behavior from one extension directory."""

    name: str
    directory: Path
    portable: Extension | None = None
    native: Any | None = None


def discover_extensions(
    directory: str | Path, *, framework: str
) -> tuple[DiscoveredExtension, ...]:
    """Discover extensions in deterministic directory-name order."""

    if framework not in {"adk", "langgraph"}:
        raise ExtensionDiscoveryError(f"unsupported extension framework: {framework}")
    root = Path(directory)
    _optional_directory(root, "extensions")
    if not root.exists():
        return ()

    result: list[DiscoveredExtension] = []
    for child in sorted(root.iterdir(), key=lambda value: value.name):
        if child.is_symlink():
            raise ExtensionDiscoveryError(
                f"extension directory cannot be a symlink: {child}"
            )
        if _ignored(child):
            continue
        if not child.is_dir() or not child.name.isidentifier():
            raise ExtensionDiscoveryError(
                f"each extension must be a Python-identifier directory: {child}"
            )
        files = _extension_files(child)
        if not files:
            continue
        portable_path = child / "lifecycle.py"
        native_path = child / f"{framework}.py"
        portable = (
            _load_portable(portable_path, expected_name=child.name)
            if portable_path in files
            else None
        )
        native = (
            _load_native(native_path, framework=framework, expected_name=child.name)
            if native_path in files
            else None
        )
        if portable is None and native is None:
            raise ExtensionDiscoveryError(
                f"extension {child.name!r} has no lifecycle.py or {framework}.py "
                f"for the selected {framework} backend"
            )
        result.append(
            DiscoveredExtension(child.name, child, portable=portable, native=native)
        )
    return tuple(result)


def _extension_files(directory: Path) -> set[Path]:
    files: set[Path] = set()
    for path in directory.iterdir():
        if path.is_symlink():
            raise ExtensionDiscoveryError(
                f"extension resource cannot be a symlink: {path}"
            )
        if _ignored(path):
            continue
        if not path.is_file() or path.name not in _FILES:
            raise ExtensionDiscoveryError(
                f"unexpected extension resource {path}; expected lifecycle.py, "
                "adk.py, and/or langgraph.py"
            )
        files.add(path)
    return files


def _load_portable(path: Path, *, expected_name: str) -> Extension:
    value = _load_export(path)
    if not isinstance(value, Extension):
        raise ExtensionDiscoveryError(
            f"{path} must export Extension named 'extension'; "
            f"got {type(value).__name__}"
        )
    if value.name != expected_name:
        raise ExtensionDiscoveryError(
            f"portable extension in {path} must be named {expected_name!r}; "
            f"got {value.name!r}"
        )
    return value


def _load_native(path: Path, *, framework: str, expected_name: str) -> Any:
    value = _load_export(path)
    if framework == "adk":
        try:
            from google.adk.plugins.base_plugin import BasePlugin
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ExtensionDiscoveryError(
                "ADK-native extensions require google-adk"
            ) from exc
        if not isinstance(value, BasePlugin):
            raise ExtensionDiscoveryError(
                f"{path} must export an ADK BasePlugin named 'extension'; "
                f"got {type(value).__name__}"
            )
        if getattr(value, "name", None) != expected_name:
            raise ExtensionDiscoveryError(
                f"ADK extension in {path} must be named {expected_name!r}; "
                f"got {getattr(value, 'name', None)!r}"
            )
        return value

    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ExtensionDiscoveryError(
            "LangGraph-native extensions require langchain"
        ) from exc
    if not isinstance(value, AgentMiddleware):
        raise ExtensionDiscoveryError(
            f"{path} must export LangChain AgentMiddleware named 'extension'; "
            f"got {type(value).__name__}"
        )
    return value


def _load_export(path: Path) -> Any:
    module_name = f"_harnest_extension_{path.parent.name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionDiscoveryError(f"cannot import extension module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ExtensionDiscoveryError(
            f"failed to import extension {path}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        sys.modules.pop(module_name, None)
    if not hasattr(module, "extension"):
        raise ExtensionDiscoveryError(
            f"{path} must export a value named 'extension'"
        )
    return module.extension


def _optional_directory(path: Path, kind: str) -> None:
    if path.is_symlink():
        raise ExtensionDiscoveryError(f"{kind} directory cannot be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise ExtensionDiscoveryError(f"{kind} path must be a directory: {path}")


def _ignored(path: Path) -> bool:
    return (
        path.name in _IGNORED
        or path.name.startswith(".")
        or (path.name.startswith("_") and path.name != "__init__.py")
        or path.name == "__pycache__"
    )


__all__ = [
    "DiscoveredExtension",
    "ExtensionDiscoveryError",
    "discover_extensions",
]

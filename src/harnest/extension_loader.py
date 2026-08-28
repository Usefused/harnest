"""Strict discovery for decorator-driven application lifecycle modules."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any
import uuid

from .lifecycle import LifecycleListener, registration_for
from .session import SessionStore


_FRAMEWORKS = frozenset({"adk", "langgraph"})
_IGNORED_NAMES = frozenset({"__init__.py", ".DS_Store", "__pycache__"})


class ExtensionDiscoveryError(ValueError):
    """An extension resource does not follow the decorator contract."""


@dataclass(frozen=True, slots=True)
class DiscoveredExtensions:
    """Ordered portable listeners and instantiated native integrations."""

    listeners: tuple[LifecycleListener, ...] = ()
    native: tuple[Any, ...] = ()
    session_store: SessionStore | None = None


def discover_extensions(
    directory: str | Path, *, framework: str
) -> DiscoveredExtensions:
    """Discover all public Python modules below root ``extensions/``."""

    if framework not in _FRAMEWORKS:
        raise ExtensionDiscoveryError(f"unsupported extension framework: {framework}")
    root = Path(directory)
    _validate_root(root)
    ordered = _ordered_listeners(root)
    session_store, remaining = _create_session_store(ordered)
    portable, native_listeners = _partition_listeners(remaining, framework)
    native = tuple(_create_native(item, framework) for item in native_listeners)
    _validate_native_duplicates(native, framework)
    return DiscoveredExtensions(portable, native, session_store)


def _create_session_store(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[SessionStore, tuple[LifecycleListener, ...]]:
    factories = tuple(item for item in listeners if item.phase == "session_store")
    if len(factories) != 1:
        raise ExtensionDiscoveryError(
            "root extensions must declare exactly one @lifecycle.session_store factory; "
            f"found {len(factories)}"
        )
    factory = factories[0]
    value = _call_factory(factory, label="session store")
    if not isinstance(value, SessionStore):
        raise ExtensionDiscoveryError(
            f"session store lifecycle factory {factory.identity} must return "
            f"SessionStore; got {type(value).__name__}"
        )
    remaining = tuple(item for item in listeners if item.phase != "session_store")
    return value, remaining


def _ordered_listeners(root: Path) -> tuple[LifecycleListener, ...]:
    groups = tuple(_load_listeners(path, root) for path in _extension_files(root))
    flattened = tuple(item for group in groups for item in group)
    return tuple(sorted(flattened, key=_listener_order))


def _partition_listeners(
    ordered: tuple[LifecycleListener, ...], framework: str
) -> tuple[tuple[LifecycleListener, ...], tuple[LifecycleListener, ...]]:
    native_phases = frozenset({"adk_plugin", "langgraph_middleware"})
    selected = "adk_plugin" if framework == "adk" else "langgraph_middleware"
    wrong = tuple(
        item
        for item in ordered
        if item.phase in native_phases and item.phase != selected
    )
    if wrong:
        raise ExtensionDiscoveryError(
            f"native lifecycle factory {wrong[0].identity} targets "
            f"{wrong[0].framework}, but this agent uses {framework}"
        )
    portable = tuple(item for item in ordered if item.phase not in native_phases)
    native = tuple(item for item in ordered if item.phase == selected)
    return portable, native


def _extension_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise ExtensionDiscoveryError(f"extension resource cannot be a symlink: {path}")
        relative = path.relative_to(root)
        if _ignored(relative):
            continue
        if path.is_dir():
            if not path.name.isidentifier():
                raise ExtensionDiscoveryError(
                    f"extension directory must be a Python identifier: {path}"
                )
            continue
        if not path.is_file() or path.suffix != ".py" or not path.stem.isidentifier():
            raise ExtensionDiscoveryError(
                f"public extension resources must be Python files: {path}"
            )
        files.append(path)
    return tuple(files)


def _load_listeners(path: Path, root: Path) -> tuple[LifecycleListener, ...]:
    module = _load_module(path)
    relative = path.relative_to(root).as_posix()
    found: list[LifecycleListener] = []
    seen: set[int] = set()
    for name, value in vars(module).items():
        registration = registration_for(value)
        if registration is None or getattr(value, "__module__", None) != module.__name__:
            continue
        if not inspect.isfunction(value):
            raise ExtensionDiscoveryError(
                f"decorated extension {relative}:{name} must be a function"
            )
        if id(value) in seen:
            raise ExtensionDiscoveryError(
                f"decorated extension function {relative}:{value.__name__} "
                "cannot be exported under multiple names"
            )
        seen.add(id(value))
        _validate_listener_signature(relative, name, value, registration.phase)
        found.append(
            LifecycleListener(
                phase=registration.phase,
                callback=value,
                order=registration.order,
                relative_path=relative,
                line=inspect.getsourcelines(value)[1],
                function_name=name,
                framework=registration.framework,
            )
        )
    return tuple(found)


def _validate_listener_signature(
    relative: str, name: str, function: Any, phase: str
) -> None:
    if phase in {"adk_plugin", "langgraph_middleware", "session_store"}:
        if inspect.signature(function).parameters:
            raise ExtensionDiscoveryError(
                f"lifecycle factory {relative}:{name} must accept no arguments"
            )
        return
    parameters = tuple(inspect.signature(function).parameters.values())
    positional = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }
    if len(parameters) != 2 or any(item.kind not in positional for item in parameters):
        raise ExtensionDiscoveryError(
            f"lifecycle listener {relative}:{name} must accept exactly two arguments"
        )


def _load_module(path: Path) -> Any:
    module_name = f"_harnest_lifecycle_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionDiscoveryError(f"cannot import extension module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ExtensionDiscoveryError(
            f"failed to import extension {path} with {type(exc).__name__}"
        ) from exc
    finally:
        sys.modules.pop(module_name, None)
    return module


def _create_native(listener: LifecycleListener, framework: str) -> Any:
    value = _call_factory(listener, label="native lifecycle")
    _validate_native_value(value, framework, listener.identity)
    return value


def _call_factory(listener: LifecycleListener, *, label: str) -> Any:
    try:
        value = listener.callback()
    except Exception as exc:
        raise ExtensionDiscoveryError(
            f"{label} factory {listener.identity} failed with "
            f"{type(exc).__name__}"
        ) from exc
    if inspect.isawaitable(value):
        closer = getattr(value, "close", None)
        if callable(closer):
            closer()
        raise ExtensionDiscoveryError(
            f"{label} factory {listener.identity} must be synchronous"
        )
    return value


def _validate_native_value(value: Any, framework: str, identity: str) -> None:
    expected = _native_type(framework)
    if not isinstance(value, expected):
        raise ExtensionDiscoveryError(
            f"native lifecycle factory {identity} must return {_native_label(framework)}; "
            f"got {type(value).__name__}"
        )


def _native_type(framework: str) -> type[Any]:
    try:
        if framework == "adk":
            from google.adk.plugins.base_plugin import BasePlugin

            return BasePlugin
        from langchain.agents.middleware import AgentMiddleware

        return AgentMiddleware
    except ImportError as exc:
        raise ExtensionDiscoveryError(
            f"{framework}-native lifecycle factories require the {framework} dependencies"
        ) from exc


def _native_label(framework: str) -> str:
    return "ADK BasePlugin" if framework == "adk" else "LangChain AgentMiddleware"


def _validate_native_duplicates(values: tuple[Any, ...], framework: str) -> None:
    if framework != "adk":
        return
    names = [getattr(value, "name", None) for value in values]
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    if duplicates:
        raise ExtensionDiscoveryError(
            "duplicate ADK native lifecycle plugin names: " + ", ".join(duplicates)
        )


def _listener_order(listener: LifecycleListener) -> tuple[int, str, int, str]:
    # Source order is the stable tie-breaker when teams intentionally share a phase.
    return (listener.order, listener.relative_path, listener.line, listener.function_name)


def _validate_root(root: Path) -> None:
    if root.is_symlink():
        raise ExtensionDiscoveryError(f"extensions directory cannot be a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise ExtensionDiscoveryError(f"extensions path must be a directory: {root}")


def _ignored(relative: Path) -> bool:
    return any(
        part in _IGNORED_NAMES or part.startswith(".") or part.startswith("_")
        for part in relative.parts
    )


__all__ = ["DiscoveredExtensions", "ExtensionDiscoveryError", "discover_extensions"]

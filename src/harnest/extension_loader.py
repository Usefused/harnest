"""Strict discovery for decorator-driven application lifecycle modules."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any
import uuid

from .checkpoint import (
    ADKStore,
    CheckpointAuthority,
    CheckpointStore,
    HarnestStore,
)
from .context import ContextValue, registration_for as context_registration_for
from .lifecycle import LifecycleListener, registration_for
from .output import OutputPolicy
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
    session_store: SessionStore | ADKStore | None = None
    checkpointer: CheckpointAuthority | None = None
    output_policy: OutputPolicy = OutputPolicy()
    context_values: tuple[ContextValue, ...] = ()


def discover_extensions(
    directory: str | Path, *, framework: str
) -> DiscoveredExtensions:
    """Discover hooks and establish one session/checkpoint authority."""

    if framework not in _FRAMEWORKS:
        raise ExtensionDiscoveryError(f"unsupported extension framework: {framework}")
    root = Path(directory)
    _validate_root(root)
    ordered = _ordered_listeners(root)
    _validate_context_names(ordered)
    session_store, session_listener, remaining = _create_session_store(
        ordered, framework
    )
    checkpointer, checkpoint_listener, remaining = _create_checkpointer(
        remaining, framework
    )
    output_policy, remaining = _create_output_policy(remaining)
    _validate_storage_ownership(session_store, checkpointer)
    portable, native_listeners = _partition_listeners(remaining, framework)
    native = tuple(_create_native(item, framework) for item in native_listeners)
    _validate_native_duplicates(native, framework)
    context_values = _factory_context_values(
        (session_listener, session_store),
        (checkpoint_listener, checkpointer),
    )
    return DiscoveredExtensions(
        listeners=portable,
        native=native,
        session_store=session_store,
        checkpointer=checkpointer,
        output_policy=output_policy,
        context_values=context_values,
    )


def _create_output_policy(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[OutputPolicy, tuple[LifecycleListener, ...]]:
    """Instantiate the optional root policy or retain safe output defaults."""

    factories = tuple(item for item in listeners if item.phase == "output_policy")
    if len(factories) > 1:
        raise ExtensionDiscoveryError(
            "root extensions may declare at most one @lifecycle.output_policy "
            f"factory; found {len(factories)}"
        )
    remaining = tuple(item for item in listeners if item.phase != "output_policy")
    if not factories:
        return OutputPolicy(), remaining
    factory = factories[0]
    value = _call_factory(factory, label="output policy")
    if not isinstance(value, OutputPolicy):
        raise ExtensionDiscoveryError(
            f"output policy lifecycle factory {factory.identity} must return "
            f"OutputPolicy; got {type(value).__name__}"
        )
    return value, remaining


def _create_checkpointer(
    listeners: tuple[LifecycleListener, ...], framework: str
) -> tuple[
    CheckpointAuthority,
    LifecycleListener,
    tuple[LifecycleListener, ...],
]:
    """Instantiate the single checkpoint authority required by every root."""

    factories = tuple(item for item in listeners if item.phase == "checkpointer")
    if len(factories) != 1:
        raise ExtensionDiscoveryError(
            "root extensions must declare exactly one @lifecycle.checkpointer "
            f"factory; found {len(factories)}"
        )
    factory = factories[0]
    value = _call_factory(factory, label="checkpointer")
    _validate_checkpoint_authority(value, factory, framework)
    remaining = tuple(item for item in listeners if item.phase != "checkpointer")
    return value, factory, remaining


def _validate_checkpoint_authority(
    value: Any, factory: LifecycleListener, framework: str
) -> None:
    """Reject incomplete or wrong-framework checkpoint ownership at compilation."""

    if not isinstance(value, CheckpointAuthority):
        raise ExtensionDiscoveryError(
            f"checkpointer lifecycle factory {factory.identity} must return a "
            f"checkpoint authority; got {type(value).__name__}"
        )
    if isinstance(value, HarnestStore) and not isinstance(value, CheckpointStore):
        raise ExtensionDiscoveryError(
            f"checkpointer lifecycle factory {factory.identity} returned "
            f"{type(value).__name__}, which does not implement CheckpointStore"
        )
    if value.framework is not None and value.framework != framework:
        raise ExtensionDiscoveryError(
            f"checkpointer lifecycle factory {factory.identity} targets "
            f"{value.framework}, but this agent uses {framework}"
        )


def _create_session_store(
    listeners: tuple[LifecycleListener, ...],
    framework: str,
) -> tuple[
    SessionStore | ADKStore,
    LifecycleListener,
    tuple[LifecycleListener, ...],
]:
    """Instantiate the single store that owns committed conversation state."""

    factories = tuple(item for item in listeners if item.phase == "session_store")
    if len(factories) != 1:
        raise ExtensionDiscoveryError(
            "root extensions must declare exactly one @lifecycle.session_store factory; "
            f"found {len(factories)}"
        )
    factory = factories[0]
    value = _call_factory(factory, label="session store")
    # Native ADK storage implements ADK's service contracts rather than the
    # portable protocol, so it is valid only inside that framework boundary.
    native_adk_store = framework == "adk" and isinstance(value, ADKStore)
    if not isinstance(value, SessionStore) and not native_adk_store:
        raise ExtensionDiscoveryError(
            f"session store lifecycle factory {factory.identity} must return "
            f"SessionStore"
            f"{' or ADKStore' if framework == 'adk' else ''}; "
            f"got {type(value).__name__}"
        )
    remaining = tuple(item for item in listeners if item.phase != "session_store")
    return value, factory, remaining


def _factory_context_values(
    *factories: tuple[LifecycleListener, Any],
) -> tuple[ContextValue, ...]:
    """Expose only storage factories explicitly marked for agent context."""

    return tuple(
        ContextValue(listener.context_name, value, listener.identity)
        for listener, value in factories
        if listener.context_name is not None
    )


def _validate_context_names(listeners: tuple[LifecycleListener, ...]) -> None:
    """Reject ambiguous resource lookup before any factory is invoked."""

    names = [item.context_name for item in listeners if item.context_name is not None]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ExtensionDiscoveryError(
            "duplicate context resource names: " + ", ".join(duplicates)
        )


def _validate_storage_ownership(
    session_store: SessionStore | ADKStore,
    checkpointer: CheckpointAuthority,
) -> None:
    """Prevent native ADK from silently ignoring a competing session store."""

    if isinstance(checkpointer, ADKStore) and session_store is not checkpointer:
        raise ExtensionDiscoveryError(
            "native ADK storage requires @lifecycle.session_store and "
            "@lifecycle.checkpointer to return the same ADKStore object"
        )


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
    """Load only decorated functions and reject aliased listener identities."""

    module = _load_module(path)
    relative = path.relative_to(root).as_posix()
    found: list[LifecycleListener] = []
    seen: set[int] = set()
    for name, value in vars(module).items():
        listener = _listener_from_export(module.__name__, relative, name, value, seen)
        if listener is not None:
            found.append(listener)
    return tuple(found)


def _listener_from_export(
    module_name: str,
    relative: str,
    name: str,
    value: Any,
    seen: set[int],
) -> LifecycleListener | None:
    """Normalize one locally declared extension without following imports."""

    registration = registration_for(value)
    context_registration = context_registration_for(value)
    if registration is None and context_registration is None:
        return None
    if getattr(value, "__module__", None) != module_name:
        return None
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
    phase = registration.phase if registration is not None else "context"
    _validate_context_provider(relative, name, value, phase, context_registration)
    _validate_listener_signature(relative, name, value, phase)
    order = (
        registration.order if registration is not None else context_registration.order
    )
    return LifecycleListener(
        phase=phase,
        callback=value,
        order=order,
        relative_path=relative,
        line=inspect.getsourcelines(value)[1],
        function_name=name,
        framework=registration.framework if registration is not None else None,
        context_name=(
            context_registration.name if context_registration is not None else None
        ),
    )


def _validate_context_provider(
    relative: str,
    name: str,
    function: Any,
    phase: str,
    registration: Any,
) -> None:
    """Keep public context binding separate from executable agent capabilities."""

    if registration is None:
        return
    if getattr(function, "__harnest_tool__", False):
        raise ExtensionDiscoveryError(
            f"context provider {relative}:{name} cannot also be a tool"
        )
    allowed = {"context", "resource", "session_store", "checkpointer"}
    if phase not in allowed:
        raise ExtensionDiscoveryError(
            f"context provider {relative}:{name} cannot also use "
            f"@lifecycle.{phase}"
        )


def _validate_listener_signature(
    relative: str, name: str, function: Any, phase: str
) -> None:
    """Keep factories declarative while giving event hooks one context pair."""

    factory_phases = {
        "adk_plugin",
        "langgraph_middleware",
        "session_store",
        "checkpointer",
        "output_policy",
        "resource",
        "context",
    }
    if phase in factory_phases:
        _validate_factory_signature(relative, name, function, phase)
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


def _validate_factory_signature(
    relative: str, name: str, function: Any, phase: str
) -> None:
    """Reject factories whose calling or ownership contract is ambiguous."""

    if inspect.signature(function).parameters:
        raise ExtensionDiscoveryError(
            f"lifecycle factory {relative}:{name} must accept no arguments"
        )
    if phase == "resource" and inspect.iscoroutinefunction(function):
        raise ExtensionDiscoveryError(
            f"runtime resource factory {relative}:{name} must be synchronous "
            "and return a context manager"
        )
    managed_context = (
        inspect.isgeneratorfunction(function)
        or inspect.isasyncgenfunction(function)
    )
    if phase == "context" and managed_context:
        raise ExtensionDiscoveryError(
            f"context provider {relative}:{name} must return a value; "
            "combine it with @lifecycle.resource when cleanup is required"
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

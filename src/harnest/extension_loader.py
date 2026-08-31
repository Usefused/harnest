"""Strict discovery for decorator-driven application lifecycle modules."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import inspect
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import uuid

from .assets import AssetStore
from .checkpoint import (
    ADKStore,
    CheckpointAuthority,
    CheckpointStore,
    HarnestStore,
)
from .context import ContextValue, registration_for as context_registration_for
from .credentials import CredentialProvider
from .http_routes import (
    HTTPRouteError,
    HTTPRouteExtension,
    create_http_route_extension,
    validate_http_route_extensions,
)
from .lifecycle import LifecycleListener, registrations_for
from .output import OutputPolicy
from .session import SessionStore
from .skills import SkillSource
from .storage_registry import CustomStorage, StorageRegistry


_FRAMEWORKS = frozenset({"adk", "langgraph"})
_IGNORED_NAMES = frozenset({"__init__.py", ".DS_Store", "__pycache__"})
_STORAGE_PHASES = frozenset(
    {"session_store", "checkpointer", "asset_store", "custom_store"}
)
_ROOT_EXTENSION_ORIGIN = "root/extensions"
_PLUGIN_EXTENSION_ORIGIN = re.compile(
    r"^plugins/[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?/extensions$"
)


class ExtensionDiscoveryError(ValueError):
    """An extension resource does not follow the decorator contract."""


@dataclass(frozen=True, slots=True)
class ExtensionSource:
    """One extension tree and its stable application-relative identity prefix."""

    directory: Path
    origin: str

    def __post_init__(self) -> None:
        """Normalize the filesystem input and reject ambiguous public origins."""

        try:
            directory = Path(self.directory)
        except TypeError as exc:
            raise TypeError("extension source directory must be path-like") from exc
        if not isinstance(self.origin, str) or not _valid_extension_origin(self.origin):
            raise ValueError(
                "extension source origin must be 'root/extensions' or "
                "'plugins/<name>/extensions'"
            )
        object.__setattr__(self, "directory", directory)


@dataclass(frozen=True, slots=True)
class DiscoveredExtensions:
    """Ordered portable listeners and instantiated native integrations."""

    listeners: tuple[LifecycleListener, ...] = ()
    native: tuple[Any, ...] = ()
    session_store: SessionStore | ADKStore | None = None
    checkpointer: CheckpointAuthority | None = None
    asset_store: AssetStore | None = field(default=None, repr=False)
    asset_stores: Mapping[str, AssetStore] = field(default_factory=dict, repr=False)
    credential_provider: CredentialProvider | None = field(default=None, repr=False)
    http_routes: tuple[HTTPRouteExtension, ...] = field(default=(), repr=False)
    output_policy: OutputPolicy = OutputPolicy()
    telemetry_exporters: tuple[LifecycleListener, ...] = ()
    context_values: tuple[ContextValue, ...] = ()
    skill_sources: Mapping[str, SkillSource] = field(default_factory=dict, repr=False)
    storage_registry: StorageRegistry = field(default_factory=StorageRegistry)
    declared_listeners: tuple[LifecycleListener, ...] = field(
        default=(), repr=False
    )


def discover_extensions(
    directory: str | Path, *, framework: str
) -> DiscoveredExtensions:
    """Discover one root extension tree through the multi-source compiler."""

    return discover_extension_sources(
        (ExtensionSource(Path(directory), _ROOT_EXTENSION_ORIGIN),),
        framework=framework,
    )


def discover_extension_sources(
    sources: Sequence[ExtensionSource], *, framework: str
) -> DiscoveredExtensions:
    """Compile ordered extension trees into one globally validated registry."""

    if framework not in _FRAMEWORKS:
        raise ExtensionDiscoveryError(f"unsupported extension framework: {framework}")
    ordered = _ordered_source_listeners(_validated_sources(sources))
    _validate_context_names(ordered)
    storage, storage_listeners, remaining = _create_storage_registry(
        ordered, framework
    )
    credential_provider, remaining = _create_credential_provider(remaining)
    skill_sources, remaining = _create_skill_sources(remaining)
    http_routes, remaining = _create_http_routes(remaining)
    output_policy, remaining = _create_output_policy(remaining)
    telemetry_exporters, remaining = _extract_telemetry_exporters(remaining)
    portable, native_listeners = _partition_listeners(remaining, framework)
    native = tuple(_create_native(item, framework) for item in native_listeners)
    _validate_native_duplicates(native, framework)
    context_values = _factory_context_values(
        *((listener, _storage_value_for_listener(storage, listener))
          for listener in storage_listeners),
    )
    return DiscoveredExtensions(
        listeners=portable,
        native=native,
        session_store=storage.sessions,
        checkpointer=storage.checkpoints,
        asset_store=storage.default_assets,
        asset_stores=storage.assets,
        credential_provider=credential_provider,
        http_routes=http_routes,
        output_policy=output_policy,
        telemetry_exporters=telemetry_exporters,
        context_values=context_values,
        skill_sources=skill_sources,
        storage_registry=storage,
        declared_listeners=ordered,
    )


def _create_skill_sources(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[dict[str, SkillSource], tuple[LifecycleListener, ...]]:
    """Materialize named source adapters without querying their remote catalogs."""

    factories = tuple(item for item in listeners if item.phase == "skill_source")
    remaining = tuple(item for item in listeners if item.phase != "skill_source")
    _validate_named_storage(factories, kind="skill source")
    sources: dict[str, SkillSource] = {}
    for listener in factories:
        value = _call_factory(listener, label="skill source")
        if not isinstance(value, SkillSource):
            raise ExtensionDiscoveryError(
                f"skill source lifecycle factory {listener.identity} must return "
                f"SkillSource; got {type(value).__name__}"
            )
        sources[listener.registration_name or "default"] = value
    return sources, remaining


def _create_storage_registry(
    listeners: tuple[LifecycleListener, ...], framework: str
) -> tuple[StorageRegistry, tuple[LifecycleListener, ...], tuple[LifecycleListener, ...]]:
    """Assemble distributed storage roles while invoking shared factories once."""

    storage = tuple(item for item in listeners if item.phase in _STORAGE_PHASES)
    remaining = tuple(item for item in listeners if item.phase not in _STORAGE_PHASES)
    sessions = _single_storage_listener(storage, "session_store")
    values: dict[int, Any] = {}
    session_store = _storage_factory_value(sessions, values)
    _validate_session_authority(session_store, sessions, framework)
    checkpoints = _single_storage_listener(storage, "checkpointer")
    checkpoint_store = _storage_factory_value(checkpoints, values)
    _validate_checkpoint_authority(checkpoint_store, checkpoints, framework)
    assets = tuple(item for item in storage if item.phase == "asset_store")
    custom = tuple(item for item in storage if item.phase == "custom_store")
    _validate_named_storage(assets, kind="asset store")
    _validate_named_storage(custom, kind="custom storage")
    for listener in assets + custom:
        _storage_factory_value(listener, values)
    _validate_storage_ownership(session_store, checkpoint_store)
    registry = StorageRegistry(
        sessions=session_store,
        checkpoints=checkpoint_store,
        assets=_named_storage_values(assets, values),
        custom=_named_storage_values(custom, values),
    )
    return registry, storage, remaining


def _single_storage_listener(
    listeners: tuple[LifecycleListener, ...], phase: str
) -> LifecycleListener:
    """Resolve one exclusive storage role with a compatibility-oriented error."""

    factories = tuple(item for item in listeners if item.phase == phase)
    if len(factories) != 1:
        decorator = "session_store" if phase == "session_store" else "checkpointer"
        raise ExtensionDiscoveryError(
            f"application extensions must declare exactly one @lifecycle.{decorator} "
            f"factory; found {len(factories)}"
        )
    return factories[0]


def _validate_named_storage(
    listeners: tuple[LifecycleListener, ...], *, kind: str
) -> None:
    """Reject ambiguous named routes before constructing external resources."""

    names = tuple(item.registration_name or "default" for item in listeners)
    duplicates = _duplicates(names)
    if duplicates:
        raise ExtensionDiscoveryError(
            f"duplicate {kind} names: " + ", ".join(duplicates)
        )


def _storage_factory_value(
    listener: LifecycleListener, values: dict[int, Any]
) -> Any:
    """Materialize one factory once and validate every role attached to it."""

    key = id(listener.callback)
    if key not in values:
        values[key] = _call_factory(listener, label="storage")
    value = values[key]
    _validate_storage_value(listener, value)
    return value


def _validate_storage_value(listener: LifecycleListener, value: Any) -> None:
    """Validate repeatable storage roles without duplicating instantiation logic."""

    if listener.phase == "asset_store" and not isinstance(value, AssetStore):
        raise ExtensionDiscoveryError(
            f"asset store lifecycle factory {listener.identity} must return "
            f"AssetStore; got {type(value).__name__}"
        )
    if listener.phase == "custom_store" and not isinstance(value, CustomStorage):
        raise ExtensionDiscoveryError(
            f"custom storage lifecycle factory {listener.identity} must return "
            "an object with async start() and close() methods"
        )


def _named_storage_values(
    listeners: tuple[LifecycleListener, ...], values: Mapping[int, Any]
) -> dict[str, Any]:
    """Bind validated names to the one value created for each source factory."""

    return {
        listener.registration_name or "default": values[id(listener.callback)]
        for listener in listeners
    }


def _storage_value_for_listener(
    registry: StorageRegistry, listener: LifecycleListener
) -> Any:
    """Resolve explicit legacy context exports from the normalized registry."""

    if listener.phase == "session_store":
        return registry.sessions
    if listener.phase == "checkpointer":
        return registry.checkpoints
    mapping = registry.assets if listener.phase == "asset_store" else registry.custom
    return mapping[listener.registration_name or "default"]


def _create_http_routes(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[tuple[HTTPRouteExtension, ...], tuple[LifecycleListener, ...]]:
    """Materialize repeatable root routers while invocation remains deferred."""

    factories = tuple(item for item in listeners if item.phase == "http_routes")
    remaining = tuple(item for item in listeners if item.phase != "http_routes")
    try:
        extensions = tuple(
            create_http_route_extension(item.callback, identity=item.identity)
            for item in factories
        )
        validate_http_route_extensions(extensions)
    except HTTPRouteError as error:
        message = str(error)
    else:
        return extensions, remaining
    # Convert outside the route-validation handler so authored failures do not
    # remain reachable from the compiler's public exception context.
    raise ExtensionDiscoveryError(message)


def _duplicates(values: tuple[str, ...]) -> list[str]:
    """Return deterministic duplicate names for authoring diagnostics."""

    return sorted({value for value in values if values.count(value) > 1})


def _extract_telemetry_exporters(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[tuple[LifecycleListener, ...], tuple[LifecycleListener, ...]]:
    """Retain repeatable exporter factories without invoking them at compile time."""

    exporters = tuple(
        item for item in listeners if item.phase == "telemetry_exporter"
    )
    remaining = tuple(
        item for item in listeners if item.phase != "telemetry_exporter"
    )
    return exporters, remaining


def _create_credential_provider(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[CredentialProvider | None, tuple[LifecycleListener, ...]]:
    """Instantiate at most one private invocation credential authority."""

    factories = tuple(
        item for item in listeners if item.phase == "credential_provider"
    )
    if len(factories) > 1:
        raise ExtensionDiscoveryError(
            "application extensions may declare at most one "
            "@lifecycle.credential_provider factory; "
            f"found {len(factories)}"
        )
    remaining = tuple(
        item for item in listeners if item.phase != "credential_provider"
    )
    if not factories:
        return None, remaining
    factory = factories[0]
    value = _call_factory(factory, label="credential provider")
    if not isinstance(value, CredentialProvider):
        raise ExtensionDiscoveryError(
            f"credential provider lifecycle factory {factory.identity} must "
            f"return CredentialProvider; got {type(value).__name__}"
        )
    return value, remaining


def _create_output_policy(
    listeners: tuple[LifecycleListener, ...],
) -> tuple[OutputPolicy, tuple[LifecycleListener, ...]]:
    """Instantiate the optional root policy or retain safe output defaults."""

    factories = tuple(item for item in listeners if item.phase == "output_policy")
    if len(factories) > 1:
        raise ExtensionDiscoveryError(
            "application extensions may declare at most one @lifecycle.output_policy "
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


def _validate_session_authority(
    value: Any, factory: LifecycleListener, framework: str
) -> None:
    """Accept portable sessions and the explicit advanced ADK wrapper."""

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


def _factory_context_values(
    *factories: tuple[LifecycleListener | None, Any],
) -> tuple[ContextValue, ...]:
    """Expose only storage factories explicitly marked for agent context."""

    values: dict[str, ContextValue] = {}
    for listener, value in factories:
        if listener is None or listener.context_name is None:
            continue
        # Stacked storage roles share one explicit context export rather than
        # publishing duplicate aliases for the same authored function.
        values.setdefault(
            listener.context_name,
            ContextValue(listener.context_name, value, listener.identity),
        )
    return tuple(values.values())


def _validate_context_names(listeners: tuple[LifecycleListener, ...]) -> None:
    """Reject ambiguous resource lookup before any factory is invoked."""

    owners: dict[str, int] = {}
    duplicates: set[str] = set()
    for listener in listeners:
        if listener.context_name is None:
            continue
        owner = owners.setdefault(listener.context_name, id(listener.callback))
        if owner != id(listener.callback):
            duplicates.add(listener.context_name)
    if duplicates:
        raise ExtensionDiscoveryError(
            "duplicate context resource names: " + ", ".join(sorted(duplicates))
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


def _validated_sources(
    sources: Sequence[ExtensionSource],
) -> tuple[ExtensionSource, ...]:
    """Freeze ordered roots and reject loading one origin or tree twice."""

    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("extension sources must be a sequence of ExtensionSource values")
    normalized = tuple(sources)
    if any(not isinstance(source, ExtensionSource) for source in normalized):
        raise TypeError("extension sources must contain only ExtensionSource values")
    origins = tuple(source.origin for source in normalized)
    if duplicates := _duplicates(origins):
        raise ExtensionDiscoveryError(
            "duplicate extension source origins: " + ", ".join(duplicates)
        )
    roots: dict[Path, str] = {}
    for source in normalized:
        _validate_root(source.directory)
        canonical = source.directory.resolve()
        if previous := roots.get(canonical):
            raise ExtensionDiscoveryError(
                f"extension directory {source.directory} is already registered as {previous}"
            )
        roots[canonical] = source.origin
    return normalized


def _ordered_source_listeners(
    sources: Sequence[ExtensionSource],
) -> tuple[LifecycleListener, ...]:
    """Flatten all roots before sorting by lifecycle order and source rank."""

    ranked = tuple(
        (rank, listener)
        for rank, source in enumerate(sources)
        for path in _extension_files(source.directory)
        for listener in _load_listeners(path, source.directory, source.origin)
    )
    # Caller order carries dependency order; source paths remain the stable
    # diagnostic and within-root tie-break without encoding rank into identity.
    ordered = sorted(ranked, key=_ranked_listener_order)
    return tuple(listener for _rank, listener in ordered)


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


def _load_listeners(
    path: Path, root: Path, origin: str
) -> tuple[LifecycleListener, ...]:
    """Load only decorated functions and reject aliased listener identities."""

    module = _load_module(path)
    relative = f"{origin}/{path.relative_to(root).as_posix()}"
    found: list[LifecycleListener] = []
    seen: set[int] = set()
    for name, value in vars(module).items():
        found.extend(
            _listeners_from_export(module.__name__, relative, name, value, seen)
        )
    return tuple(found)


def _listeners_from_export(
    module_name: str,
    relative: str,
    name: str,
    value: Any,
    seen: set[int],
) -> tuple[LifecycleListener, ...]:
    """Normalize every role on one local factory without following imports."""

    registrations = registrations_for(value)
    context_registration = context_registration_for(value)
    if not registrations and context_registration is None:
        return ()
    if getattr(value, "__module__", None) != module_name:
        return ()
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
    if not registrations:
        return (
            _listener_for_registration(
                relative, name, value, None, context_registration
            ),
        )
    return tuple(
        _listener_for_registration(
            relative, name, value, registration, context_registration
        )
        for registration in registrations
    )


def _listener_for_registration(
    relative: str,
    name: str,
    value: Any,
    registration: Any,
    context_registration: Any,
) -> LifecycleListener:
    """Create one deterministic listener view over a possibly shared factory."""

    phase = registration.phase if registration is not None else "context"
    _validate_context_provider(relative, name, value, phase, context_registration)
    _validate_listener_signature(relative, name, value, phase)
    order = registration.order if registration is not None else context_registration.order
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
        registration_name=_registration_name(registration),
    )


def _registration_name(registration: Any) -> str | None:
    """Read optional factory naming metadata without burdening discovery flow."""

    return registration.name if registration is not None else None


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
    allowed = {
        "context",
        "resource",
        "session_store",
        "checkpointer",
        "asset_store",
        "custom_store",
    }
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
        "asset_store",
        "custom_store",
        "credential_provider",
        "http_routes",
        "output_policy",
        "telemetry_exporter",
        "resource",
        "skill_source",
        "context",
    }
    if phase == "http_routes":
        _validate_http_route_factory_signature(relative, name, function)
        return
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


def _validate_http_route_factory_signature(
    relative: str, name: str, function: Any
) -> None:
    """Require one injected invoker without accepting ambiguous parameters."""

    parameters = tuple(inspect.signature(function).parameters.values())
    positional = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }
    if len(parameters) != 1 or parameters[0].kind not in positional:
        raise ExtensionDiscoveryError(
            f"HTTP route factory {relative}:{name} must accept exactly one "
            "AgentInvoker argument"
        )
    if inspect.iscoroutinefunction(function):
        raise ExtensionDiscoveryError(
            f"HTTP route factory {relative}:{name} must be synchronous"
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
    failure: ExtensionDiscoveryError | None = None
    try:
        value = listener.callback()
    except Exception as error:
        failure = ExtensionDiscoveryError(
            f"{label} factory {listener.identity} failed with "
            f"{type(error).__name__}"
        )
    if failure is not None:
        # Raise outside the authored exception handler so secret-bearing
        # constructor failures are not retained through __context__.
        raise failure
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


def _ranked_listener_order(
    ranked: tuple[int, LifecycleListener],
) -> tuple[int, int, str, int, str]:
    """Keep explicit hook order primary and dependency-aware source order stable."""

    rank, listener = ranked
    return (
        listener.order,
        rank,
        listener.relative_path,
        listener.line,
        listener.function_name,
    )


def _valid_extension_origin(origin: str) -> bool:
    """Accept only identities the artifact layout can reproduce exactly."""

    return origin == _ROOT_EXTENSION_ORIGIN or bool(
        _PLUGIN_EXTENSION_ORIGIN.fullmatch(origin)
    )


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


__all__ = [
    "DiscoveredExtensions",
    "ExtensionDiscoveryError",
    "ExtensionSource",
    "discover_extension_sources",
    "discover_extensions",
]

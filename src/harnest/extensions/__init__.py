"""Public APIs and controlled namespaces for Harnest Extensions."""

from __future__ import annotations

import importlib.util
import inspect
import keyword
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from types import ModuleType
from typing import Any, Generic, Iterator, Sequence, TypeVar, cast

from harnest.extension_runtime_context import ExtensionStartContext, extension_mutation
from harnest.extension_descriptors import (
    ExtensionDescriptor,
    verify_extension,
)


class ExtensionContextUnavailableError(RuntimeError):
    """An extension context was used outside its managed invocation."""


class ExtensionNamespaceError(RuntimeError):
    """An extension namespace cannot be activated or safely released."""


class ExtensionImportError(RuntimeError):
    """An authored extension module violates its export contract."""


@dataclass(slots=True)
class _ExtensionContextLifetime:
    """Share revocation with child tasks that copied task-local extension state."""

    active: bool = True


class ExtensionContext:
    """Revocable invocation base that extension-specific contexts may extend."""

    __slots__ = ("_continuations", "_extension_name", "_lifetime")

    def __init__(self, extension_name: str) -> None:
        """Bind one validated extension identity to the invocation view."""

        if (
            not isinstance(extension_name, str)
            or not extension_name.isidentifier()
            or keyword.iskeyword(extension_name)
        ):
            raise ValueError(
                "extension context name must be a non-keyword Python identifier"
            )
        self._extension_name = extension_name
        self._lifetime = _ExtensionContextLifetime()
        self._continuations = None

    @property
    def extension_name(self) -> str:
        """Return the extension whose invocation authority this context carries."""

        return self._extension_name

    @property
    def active(self) -> bool:
        """Report whether the owning managed invocation is still active."""

        return self._lifetime.active

    @property
    def continuations(self) -> Any:
        """Return provider-bound suspension authority for this invocation."""

        self._require_active()
        if self._continuations is None:
            raise ExtensionContextUnavailableError(
                f"extension {self._extension_name!r} did not declare "
                "context.continuations"
            )
        return self._continuations

    def _bind_continuations(self, value: object) -> None:
        """Install only the host-created port after authored context adaptation."""

        self._require_active()
        if self._continuations is not None:
            raise ExtensionNamespaceError("extension continuation authority is already bound")
        self._continuations = value

    def _require_active(self) -> None:
        """Prevent retained child tasks from using authority after invocation exit."""

        if not self._lifetime.active:
            raise ExtensionContextUnavailableError(
                f"extension {self._extension_name!r} context is no longer active"
            )

    def _revoke(self) -> None:
        """Revoke this context and every task that inherited the same object."""

        self._lifetime.active = False


ContextT = TypeVar("ContextT", bound=ExtensionContext)


class Extension(Generic[ContextT]):
    """Application-owned extension with task-local invocation context."""

    __slots__ = ("_harnest_context", "_harnest_name")

    def __new__(cls, *args: object, **kwargs: object) -> "Extension[ContextT]":
        """Install private state even when a subclass omits ``super().__init__``."""

        instance = cast("Extension[ContextT]", super().__new__(cls))
        object.__setattr__(
            instance,
            "_harnest_context",
            ContextVar(
                f"harnest_extension_context_{cls.__module__}_{cls.__qualname__}",
                default=None,
            ),
        )
        object.__setattr__(instance, "_harnest_name", None)
        return instance

    async def start(self, context: "ExtensionStartContext") -> None:
        """Initialize application-scoped resources after dependencies start."""

        return None

    async def stop(self) -> None:
        """Release application-scoped resources before dependencies stop."""

        return None

    def create_context(self, context: ExtensionContext) -> ContextT:
        """Adapt the managed invocation base to an extension-specific context."""

        if not isinstance(context, ExtensionContext):
            raise TypeError("extension context must extend ExtensionContext")
        self._require_identity(context.extension_name)
        return cast(ContextT, context)

    @property
    def context(self) -> ContextT:
        """Return the task-local extension context for the active invocation."""

        active = self._harnest_context.get()
        if active is None:
            name = self._harnest_name or type(self).__name__
            raise ExtensionContextUnavailableError(
                f"extension {name!r} context is available only during an invocation"
            )
        # Calling the base method directly keeps an authored context subclass
        # from weakening host-owned revocation policy.
        ExtensionContext._require_active(active)
        return cast(ContextT, active)

    def _bind_context(self, context: ExtensionContext) -> Token[ExtensionContext | None]:
        """Bind one manager-created context to the current async execution."""

        if not isinstance(context, ExtensionContext):
            raise TypeError("extension context must extend ExtensionContext")
        Extension._require_identity(self, context.extension_name)
        ExtensionContext._require_active(context)
        return self._harnest_context.set(context)

    def _reset_context(self, token: Token[ExtensionContext | None]) -> None:
        """Restore the previous task-local context after invocation teardown."""

        self._harnest_context.reset(token)

    def _bind_identity(self, name: str) -> None:
        """Bind the authored singleton to its descriptor-owned public name."""

        if self._harnest_name not in {None, name}:
            raise ExtensionNamespaceError(
                f"extension instance is already bound as {self._harnest_name!r}"
            )
        self._harnest_name = name

    def _clear_identity(self, name: str) -> None:
        """Release only the descriptor identity owned by this activation."""

        if self._harnest_name == name:
            self._harnest_name = None

    def _require_identity(self, name: str) -> None:
        """Keep one extension singleton from crossing descriptor boundaries."""

        if self._harnest_name is None:
            raise ExtensionNamespaceError("extension instance is not activated")
        if self._harnest_name != name:
            raise ExtensionNamespaceError(
                f"extension {self._harnest_name!r} cannot bind context for {name!r}"
            )


@dataclass(frozen=True, slots=True)
class ActivatedExtension:
    """One validated namespace module and its authored singleton."""

    descriptor: ExtensionDescriptor
    module: ModuleType
    extension: Extension[ExtensionContext]


@dataclass(slots=True)
class _ActivationState:
    """Reference-count one process-wide extension namespace transaction."""

    key: tuple[tuple[str, str, str], ...]
    extensions: tuple[ActivatedExtension, ...]
    references: int = 1


_activation_lock = RLock()
_active_state: _ActivationState | None = None


def activate_extensions(
    descriptors: Sequence[ExtensionDescriptor],
) -> tuple[ActivatedExtension, ...]:
    """Activate a dependency-ordered extension set as one namespace transaction."""

    resolved = tuple(descriptors)
    if not resolved:
        return ()
    _validate_activation_order(resolved)
    for descriptor in resolved:
        verify_extension(descriptor)
    key = _activation_key(resolved)
    global _active_state
    with _activation_lock:
        if _active_state is not None:
            if _active_state.key != key:
                raise ExtensionNamespaceError(
                    "harnest.extensions is already bound to another compiled agent"
                )
            _active_state.references += 1
            return _active_state.extensions
        activated: list[ActivatedExtension] = []
        try:
            for descriptor in resolved:
                activated.append(_activate_extension(descriptor))
        except Exception:
            _release_activated(tuple(reversed(activated)))
            raise
        _active_state = _ActivationState(key, tuple(activated))
        return _active_state.extensions


def release_extensions(
    descriptors: Sequence[ExtensionDescriptor],
) -> None:
    """Release one acquisition without disturbing a competing extension set."""

    resolved = tuple(descriptors)
    if not resolved:
        return
    key = _activation_key(resolved)
    global _active_state
    with _activation_lock:
        if _active_state is None:
            return
        if _active_state.key != key:
            raise ExtensionNamespaceError(
                "cannot release extensions owned by another compiled agent"
            )
        _active_state.references -= 1
        if _active_state.references > 0:
            return
        _release_activated(tuple(reversed(_active_state.extensions)))
        _active_state = None


@contextmanager
def extension_namespaces(
    descriptors: Sequence[ExtensionDescriptor],
) -> Iterator[tuple[ActivatedExtension, ...]]:
    """Activate extensions for one bounded compiler or test operation."""

    resolved = tuple(descriptors)
    activated = activate_extensions(resolved)
    try:
        yield activated
    finally:
        if activated:
            release_extensions(resolved)


def _validate_activation_order(
    descriptors: Sequence[ExtensionDescriptor],
) -> None:
    """Require dependency-first descriptors before executing any authored code."""

    seen: set[str] = set()
    casefold_names: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, ExtensionDescriptor):
            raise TypeError("extension activation requires discovered descriptors")
        if descriptor.name in seen or descriptor.name.casefold() in casefold_names:
            raise ExtensionNamespaceError(
                f"duplicate extension activation name: {descriptor.name!r}"
            )
        unavailable = sorted(set(descriptor.requires) - seen)
        if unavailable:
            raise ExtensionNamespaceError(
                f"extension {descriptor.name!r} must follow dependencies: "
                + ", ".join(unavailable)
            )
        seen.add(descriptor.name)
        casefold_names.add(descriptor.name.casefold())


def _activation_key(
    descriptors: Sequence[ExtensionDescriptor],
) -> tuple[tuple[str, str, str], ...]:
    """Identify an immutable extension set without retaining executable objects."""

    return tuple(
        (item.name, str(item.directory.resolve()), item.digest) for item in descriptors
    )


def _activate_extension(descriptor: ExtensionDescriptor) -> ActivatedExtension:
    """Load one extension into its compiler-owned public namespace."""

    namespace = descriptor.namespace
    module = _extension_module(descriptor)
    _publish_namespace(module, namespace, descriptor.name)
    failure: str | None = None
    try:
        loader = module.__spec__.loader if module.__spec__ is not None else None
        if loader is None:
            raise ImportError("extension module loader is unavailable")
        loader.exec_module(module)
    except Exception as error:
        failure = type(error).__name__
    if failure is not None:
        _remove_extension_modules(descriptor.name, namespace=namespace)
        raise ExtensionImportError(
            f"failed to import extension {descriptor.name!r} with {failure}"
        )
    try:
        extension = _validated_extension_export(module, descriptor)
    except Exception:
        _remove_extension_modules(descriptor.name, namespace=namespace)
        raise
    Extension._bind_identity(extension, descriptor.name)
    return ActivatedExtension(descriptor, module, extension)


def _publish_namespace(module: ModuleType, namespace: str, name: str) -> None:
    """Publish one canonical extension namespace after collision checks."""

    parent = importlib.import_module(namespace.rpartition(".")[0])
    if namespace in sys.modules or hasattr(parent, name):
        raise ExtensionNamespaceError(
            f"extension namespace is already occupied: {namespace}"
        )
    sys.modules[namespace] = module
    setattr(parent, name, module)


def _extension_module(descriptor: ExtensionDescriptor) -> ModuleType:
    """Create a package-like module so relative extension imports stay contained."""

    spec = importlib.util.spec_from_file_location(
        descriptor.namespace,
        descriptor.source,
        submodule_search_locations=[str(descriptor.directory)],
    )
    if spec is None or spec.loader is None:
        raise ExtensionImportError(
            f"cannot create a module for extension {descriptor.name!r}"
        )
    return importlib.util.module_from_spec(spec)


def _validated_extension_export(
    module: ModuleType, descriptor: ExtensionDescriptor
) -> Extension[ExtensionContext]:
    """Require one locally-authored public class and its singleton instance."""

    export = descriptor.entrypoint.partition(":")[2]
    value = getattr(module, export, None)
    if not isinstance(value, Extension):
        raise ExtensionImportError(
            f"extension {descriptor.name!r} must export Extension instance '{export}'"
        )
    extension_type = type(value)
    if extension_type is Extension or extension_type.__module__ != module.__name__:
        raise ExtensionImportError(
            f"extension {descriptor.name!r} instance must use a local Extension class"
        )
    class_name = extension_type.__name__
    if (
        class_name.startswith("_")
        or not class_name.isidentifier()
        or keyword.iskeyword(class_name)
        or getattr(module, class_name, None) is not extension_type
    ):
        raise ExtensionImportError(
            f"extension {descriptor.name!r} must publicly export its Extension class"
        )
    _reject_extra_extension_exports(module, value, extension_type, descriptor.name)
    return cast(Extension[ExtensionContext], value)


def _reject_extra_extension_exports(
    module: ModuleType,
    extension: Extension[ExtensionContext],
    extension_type: type[Extension[ExtensionContext]],
    name: str,
) -> None:
    """Prevent ambiguous lifecycle ownership from multiple public extensions."""

    extras = [
        *_extra_extension_instances(module, extension),
        *_extra_extension_classes(module, extension_type),
    ]
    if extras:
        raise ExtensionImportError(
            f"extension {name!r} exports additional Extension resources: "
            + ", ".join(extras)
        )


def _extra_extension_instances(
    module: ModuleType, extension: Extension[ExtensionContext]
) -> list[str]:
    """Return public singleton exports competing with the declared extension."""

    return sorted(
        export
        for export, value in vars(module).items()
        if not export.startswith("_")
        and isinstance(value, Extension)
        and value is not extension
    )


def _extra_extension_classes(
    module: ModuleType, extension_type: type[Extension[ExtensionContext]]
) -> list[str]:
    """Return additional public Extension subclasses with ambiguous ownership."""

    return sorted(
        export
        for export, value in vars(module).items()
        if not export.startswith("_")
        and inspect.isclass(value)
        and value not in {Extension, extension_type}
        and issubclass(value, Extension)
    )


def _release_activated(extensions: Sequence[ActivatedExtension]) -> None:
    """Remove child modules in reverse dependency order."""

    for activated in extensions:
        Extension._clear_identity(activated.extension, activated.descriptor.name)
        _remove_extension_modules(
            activated.descriptor.name, namespace=activated.descriptor.namespace
        )


def _remove_extension_modules(name: str, *, namespace: str | None = None) -> None:
    """Remove only the extension namespace and its relative imports."""

    namespace = namespace or f"{__name__}.{name}"
    for module_name in tuple(sys.modules):
        if module_name == namespace or module_name.startswith(namespace + "."):
            sys.modules.pop(module_name, None)
    parent = sys.modules.get(namespace.rpartition(".")[0])
    if parent is not None and hasattr(parent, name):
        delattr(parent, name)


__all__ = [
    "ActivatedExtension",
    "Extension",
    "ExtensionContext",
    "ExtensionContextUnavailableError",
    "ExtensionImportError",
    "ExtensionNamespaceError",
    "ExtensionStartContext",
    "activate_extensions",
    "extension_namespaces",
    "extension_mutation",
    "release_extensions",
]

"""Application ownership and invocation binding for same-process extensions."""

from __future__ import annotations

import asyncio
import inspect
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from ._exception_notes import add_exception_note
from .extension_runtime_context import (
    ExtensionInvocationBinding,
    ExtensionStartContext,
)

if TYPE_CHECKING:
    from .extensions import ActivatedExtension


class ExtensionRuntimeError(RuntimeError):
    """A same-process extension failed at a sanitized runtime boundary."""


_runtime_owner_lock = RLock()
_runtime_owners: dict[int, tuple[Any, object]] = {}


class ExtensionRuntimeManager:
    """Start extension singletons once and create revocable invocation views."""

    def __init__(
        self,
        activated: Sequence["ActivatedExtension"],
        *,
        framework: str,
        root_agent_name: str,
        custom_stores: Mapping[str, Any] | None = None,
        continuation_runtime: Any | None = None,
        release_namespaces_on_close: bool = False,
    ) -> None:
        """Validate dependencies without initializing extension-owned resources."""

        if framework not in {"adk", "langgraph"}:
            raise ValueError("extension runtime framework must be adk or langgraph")
        _require_text(root_agent_name, "root agent name")
        stores = {} if custom_stores is None else custom_stores
        if not isinstance(stores, Mapping):
            raise TypeError("extension custom storage must be a mapping")
        if not isinstance(release_namespaces_on_close, bool):
            raise TypeError("release_namespaces_on_close must be boolean")
        self._activated = _ordered_extensions(activated)
        self._framework = framework
        self._root_agent_name = root_agent_name
        self._custom_stores = MappingProxyType(dict(stores))
        self._continuation_runtime = continuation_runtime
        self._release_namespaces_on_close = release_namespaces_on_close
        self._namespaces_released = False
        self._lock = asyncio.Lock()
        self._state = "new"
        self._started: list[Any] = []
        self._owner_token = object()
        self._runtime_claimed = False

    @property
    def started(self) -> bool:
        """Report readiness without exposing extension instances or resources."""

        return self._state == "started"

    async def start(self) -> None:
        """Start dependencies first and unwind every attempted extension on failure."""

        failure: BaseException | None = None
        async with self._lock:
            if self._state == "started":
                return
            if self._state != "new":
                failure = ExtensionRuntimeError("extension runtime cannot be restarted")
            else:
                failure = await self._claim_and_start_locked()
        if failure is not None:
            # The sanitized value was built outside the authored exception
            # object, so its message and traceback cannot leak extension state.
            raise failure

    async def close(self) -> None:
        """Stop extensions in reverse dependency order exactly once."""

        failure: BaseException | None = None
        async with self._lock:
            if self._state == "closed":
                return
            if self._state == "started":
                failure = await _stop_extensions(tuple(reversed(self._started)))
            self._state = "closed"
            self._started.clear()
            namespace_failure = self._release_namespaces_locked()
            failure = _merge_failure(
                failure, namespace_failure, label="extension namespace"
            )
            self._release_runtime_claim_locked()
        if failure is not None:
            raise failure

    def invocation_bindings(self) -> Mapping[str, ExtensionInvocationBinding]:
        """Construct fresh typed views after application startup succeeds."""

        if self._state != "started":
            raise ExtensionRuntimeError("extension runtime is not started")
        bindings: dict[str, ExtensionInvocationBinding] = {}
        for activated in self._activated:
            binding, failure = _create_invocation_binding(
                activated, self._continuation_runtime
            )
            if failure is not None:
                _revoke_bindings(bindings.values())
                raise failure
            if binding is None:  # pragma: no cover - failure pairing owns this
                raise RuntimeError("extension context construction returned no binding")
            bindings[activated.descriptor.name] = binding
        return MappingProxyType(bindings)

    async def _start_locked(self) -> BaseException | None:
        """Start one ordered set while the manager owns its state lock."""

        attempted: list[Any] = []
        for activated in self._activated:
            attempted.append(activated)
            failure = await _extension_hook_failure(
                activated,
                "start",
                self._start_context(activated),
            )
            if failure is not None:
                cleanup = await _stop_extensions(tuple(reversed(attempted)))
                _annotate_cleanup(failure, cleanup)
                self._state = "failed"
                self._started.clear()
                return failure
            self._started.append(activated)
        self._state = "started"
        return None

    async def _claim_and_start_locked(self) -> BaseException | None:
        """Claim process singletons before authored startup can produce effects."""

        if not _claim_extensions(self._activated, self._owner_token):
            self._state = "failed"
            return ExtensionRuntimeError(
                "runtime extension singleton is already active in another runtime"
            )
        self._runtime_claimed = True
        failure = await self._start_locked()
        if failure is not None:
            # Failed startup has already unwound every attempted hook, so no
            # live runtime authority remains for this manager to protect.
            self._release_runtime_claim_locked()
        return failure

    def _release_runtime_claim_locked(self) -> None:
        """Release only this manager's process claim after hooks have stopped."""

        if not self._runtime_claimed:
            return
        _release_runtime_extension_claims(self._activated, self._owner_token)
        self._runtime_claimed = False

    def _start_context(self, activated: Any) -> ExtensionStartContext:
        """Expose custom stores only under the descriptor's declared authority."""

        descriptor = activated.descriptor
        # Capabilities are reviewable compiler contracts rather than a Python
        # sandbox, but the host still withholds undeclared startup authorities.
        stores = (
            self._custom_stores
            if "context.storage" in descriptor.capabilities
            else {}
        )

        return ExtensionStartContext(
            extension_name=descriptor.name,
            framework=self._framework,
            root_agent_name=self._root_agent_name,
            _custom_stores=stores,
            _continuations=(
                self._continuation_runtime.application_port(descriptor.name)
                if self._continuation_runtime is not None
                and "context.continuations" in descriptor.capabilities
                else None
            ),
        )

    def _release_namespaces_locked(self) -> BaseException | None:
        """Release a transferred namespace acquisition at most once."""

        if not self._release_namespaces_on_close or self._namespaces_released:
            return None
        # Mark ownership consumed before calling the external release boundary;
        # retries could otherwise decrement another application's reference.
        self._namespaces_released = True
        from .extensions import release_extensions

        descriptors = tuple(item.descriptor for item in self._activated)
        try:
            release_extensions(descriptors)
        except BaseException as error:
            return ExtensionRuntimeError(
                "runtime extension namespace release failed with "
                f"{type(error).__name__}"
            )
        return None


def _claim_extensions(activated: Sequence[Any], owner: object) -> bool:
    """Atomically reject overlap without changing namespace reference counts."""

    extensions = tuple(item.extension for item in activated)
    with _runtime_owner_lock:
        if any(_owned_by_other(extension, owner) for extension in extensions):
            return False
        for extension in extensions:
            # Keep the object itself so an integer identity can never be reused
            # while process ownership remains registered.
            _runtime_owners[id(extension)] = (extension, owner)
    return True


def _owned_by_other(extension: Any, owner: object) -> bool:
    """Report whether one exact singleton belongs to a different manager."""

    current = _runtime_owners.get(id(extension))
    return current is not None and current[0] is extension and current[1] is not owner


def _release_runtime_extension_claims(
    activated: Sequence[Any], owner: object
) -> None:
    """Remove only claims created by the closing manager."""

    with _runtime_owner_lock:
        for item in activated:
            extension = item.extension
            current = _runtime_owners.get(id(extension))
            if (
                current is not None
                and current[0] is extension
                and current[1] is owner
            ):
                _runtime_owners.pop(id(extension), None)


def _ordered_extensions(activated: Sequence[Any]) -> tuple[Any, ...]:
    """Validate activated values and enforce dependency order defensively."""

    if isinstance(activated, (str, bytes)):
        raise TypeError("activated extensions must be a sequence")
    values = tuple(activated)
    _validate_activated_extensions(values)
    by_name = {item.descriptor.name: item for item in values}
    _validate_dependencies(by_name)
    ordered: list[Any] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    for item in values:
        _visit_extension(item, by_name, visiting, visited, ordered)
    return tuple(ordered)


def _validate_activated_extensions(values: tuple[Any, ...]) -> None:
    """Reject manual runtime construction that bypasses namespace validation."""

    from .extensions import ActivatedExtension, Extension

    if any(not isinstance(item, ActivatedExtension) for item in values):
        raise TypeError("extension runtime requires ActivatedExtension values")
    if any(not isinstance(item.extension, Extension) for item in values):
        raise TypeError("activated extensions must contain Extension singletons")
    names = tuple(item.descriptor.name for item in values)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("duplicate runtime extension names: " + ", ".join(duplicates))


def _validate_dependencies(by_name: Mapping[str, Any]) -> None:
    """Reject unresolved dependencies before any extension can acquire resources."""

    for name, activated in by_name.items():
        missing = tuple(
            dependency
            for dependency in activated.descriptor.requires
            if dependency not in by_name
        )
        if missing:
            raise ValueError(
                f"runtime extension {name!r} requires missing extension {missing[0]!r}"
            )


def _visit_extension(
    activated: Any,
    by_name: Mapping[str, Any],
    visiting: set[str],
    visited: set[str],
    ordered: list[Any],
) -> None:
    """Append one extension after its dependencies and report cycles by identity."""

    name = activated.descriptor.name
    if name in visited:
        return
    if name in visiting:
        raise ValueError(f"runtime extension dependency cycle includes {name!r}")
    visiting.add(name)
    for dependency in activated.descriptor.requires:
        _visit_extension(by_name[dependency], by_name, visiting, visited, ordered)
    visiting.remove(name)
    visited.add(name)
    ordered.append(activated)


async def _extension_hook_failure(
    activated: Any, hook_name: str, *arguments: Any
) -> BaseException | None:
    """Call an authored hook while detaching secret-bearing exceptions."""

    hook = getattr(activated.extension, hook_name, None)
    if not callable(hook):
        return None
    try:
        value = hook(*arguments)
        if not inspect.isawaitable(value):
            raise TypeError("extension lifecycle hooks must be asynchronous")
        await value
    except asyncio.CancelledError:
        return asyncio.CancelledError()
    except BaseException as error:
        return ExtensionRuntimeError(
            f"runtime extension {activated.descriptor.name!r} {hook_name} failed "
            f"with {type(error).__name__}"
        )
    return None


async def _stop_extensions(activated: tuple[Any, ...]) -> BaseException | None:
    """Attempt every cleanup and retain deterministic failure priority."""

    primary: BaseException | None = None
    for item in activated:
        failure = await _extension_hook_failure(item, "stop")
        if failure is None:
            continue
        if primary is None:
            primary = failure
        else:
            add_exception_note(
                primary,
                "extension cleanup also failed with " f"{type(failure).__name__}"
            )
    return primary


def _create_invocation_binding(
    activated: Any,
    continuation_runtime: Any | None = None,
) -> tuple[ExtensionInvocationBinding | None, BaseException | None]:
    """Construct one authored view without retaining its original failure."""

    from .extensions import ExtensionContext

    try:
        base = ExtensionContext(activated.descriptor.name)
        value = activated.extension.create_context(base)
        if not isinstance(value, ExtensionContext):
            raise TypeError("create_context must return ExtensionContext")
        if value.extension_name != activated.descriptor.name:
            raise ValueError("extension context identity cannot be replaced")
        ExtensionContext._require_active(value)
        if (
            continuation_runtime is not None
            and "context.continuations" in activated.descriptor.capabilities
        ):
            # Bind after authored adaptation so a subclass cannot substitute a
            # broader provider identity or retain another extension's authority.
            ExtensionContext._bind_continuations(
                value,
                continuation_runtime.invocation_port(activated.descriptor.name),
            )
    except BaseException as error:
        failure = ExtensionRuntimeError(
            f"runtime extension {activated.descriptor.name!r} context failed with "
            f"{type(error).__name__}"
        )
        return None, failure
    return ExtensionInvocationBinding(activated.extension, value), None


def _revoke_bindings(bindings: Any) -> None:
    """Revoke successfully constructed views after a later factory fails."""

    from .extensions import ExtensionContext

    for binding in bindings:
        ExtensionContext._revoke(binding.extension_context)


def _annotate_cleanup(
    primary: BaseException, cleanup: BaseException | None
) -> None:
    """Record only cleanup type while retaining the primary startup failure."""

    if cleanup is not None:
        add_exception_note(
            primary,
            "extension startup cleanup also failed with " f"{type(cleanup).__name__}"
        )


def _merge_failure(
    primary: BaseException | None,
    cleanup: BaseException | None,
    *,
    label: str,
) -> BaseException | None:
    """Retain primary failure priority while recording cleanup type only."""

    if cleanup is None:
        return primary
    if primary is None:
        return cleanup
    add_exception_note(
        primary, f"{label} cleanup also failed with {type(cleanup).__name__}"
    )
    return primary


def _require_text(value: Any, label: str) -> None:
    """Require stable identity without coercing authored configuration values."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"extension runtime {label} must be a non-empty string")


__all__ = ["ExtensionRuntimeError", "ExtensionRuntimeManager"]

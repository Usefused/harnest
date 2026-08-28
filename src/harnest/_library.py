"""Process-scoped namespaces for authored reusable Python modules."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from threading import RLock
from types import ModuleType
from typing import Iterator

_IGNORED_DIRECTORIES = {"__pycache__"}
_lock = RLock()


@dataclass
class _AuthoredNamespace:
    """Track one authored folder and its temporary ``harnest.*`` binding."""

    directory: str
    label: str
    active_root: Path | None = None
    active_references: int = 0

    @property
    def namespace(self) -> str:
        """Return the public import namespace owned by this folder."""

        return f"harnest.{self.directory}"


_NAMESPACES = (
    _AuthoredNamespace("lib", "authored lib"),
    _AuthoredNamespace("models", "authored models"),
)


class LibraryConventionError(RuntimeError):
    """An authored Python namespace violates the safe folder convention."""


class LibraryImportError(RuntimeError):
    """An authored namespace package initializer could not be imported."""


def activate_authored_library(bundle_root: Path) -> bool:
    """Bind authored ``lib/`` and ``models/`` folders below ``harnest``.

    The historical function name remains the runtime ownership boundary. Empty
    or absent folders reserve no namespace, matching optional resource folders.
    """

    roots = [
        (binding, _validated_namespace_root(bundle_root, binding))
        for binding in _NAMESPACES
    ]
    activated: list[tuple[_AuthoredNamespace, Path]] = []
    with _lock:
        _reject_competing_bundle(bundle_root.resolve())
        try:
            for binding, root in roots:
                if root is not None:
                    _activate_binding(binding, root)
                    activated.append((binding, root))
        except Exception:
            # Activation is one ownership transaction: never leave lib bound
            # when the companion models namespace fails validation or import.
            for binding, root in reversed(activated):
                _release_binding(binding, root)
            raise
    return bool(activated)


def _reject_competing_bundle(bundle_root: Path) -> None:
    """Prevent authored namespaces from different agents sharing one process."""

    for binding in _NAMESPACES:
        if binding.active_root is None or binding.active_root.parent == bundle_root:
            continue
        raise LibraryConventionError(
            f"{binding.namespace} is already bound to {binding.active_root}; "
            f"cannot also bind authored namespaces from {bundle_root}"
        )


def release_authored_library(bundle_root: Path) -> None:
    """Release authored namespace bindings acquired for ``bundle_root``."""

    with _lock:
        for binding in reversed(_NAMESPACES):
            root = (bundle_root / binding.directory).resolve()
            _release_binding(binding, root)


@contextmanager
def authored_library(bundle_root: Path) -> Iterator[None]:
    """Activate authored namespaces for a bounded compiler operation."""

    acquired = activate_authored_library(bundle_root)
    try:
        yield
    finally:
        if acquired:
            release_authored_library(bundle_root)


def _validated_namespace_root(
    bundle_root: Path, binding: _AuthoredNamespace
) -> Path | None:
    """Validate one optional authored namespace and return its import root."""

    root = bundle_root / binding.directory
    if root.is_symlink():
        raise LibraryConventionError(
            f"{binding.label} directory cannot be a symlink: {root}"
        )
    if not root.exists():
        return None
    if not root.is_dir():
        raise LibraryConventionError(f"{binding.label} path must be a directory: {root}")
    python_files = _validate_directory(root, binding)
    return root.resolve() if python_files else None


def _validate_directory(directory: Path, binding: _AuthoredNamespace) -> int:
    """Recursively validate importable entries in one authored namespace."""

    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    _reject_local_collisions(directory, entries, binding)
    python_files = 0
    for path in entries:
        if path.is_symlink():
            raise LibraryConventionError(
                f"{binding.label} cannot contain symlinks: {path}"
            )
        if path.is_dir():
            python_files += _validate_namespace_directory(path, binding)
        elif path.is_file():
            python_files += _validate_namespace_file(path, binding)
        else:
            raise LibraryConventionError(
                f"unsupported {binding.label} entry: {path}"
            )
    return python_files


def _validate_namespace_directory(path: Path, binding: _AuthoredNamespace) -> int:
    """Validate a nested package directory or safely ignore cache content."""

    if path.name in _IGNORED_DIRECTORIES or path.name.startswith("."):
        _reject_symlinks_below(path, binding)
        return 0
    _require_identifier(path.name, path, binding)
    return _validate_directory(path, binding)


def _validate_namespace_file(path: Path, binding: _AuthoredNamespace) -> int:
    """Validate one module while allowing underscore-prefixed folder guides."""

    if path.name.startswith("."):
        return 0
    if path.suffix != ".py":
        if path.name.startswith("_"):
            return 0
        raise LibraryConventionError(
            f"unexpected file in {binding.label}: {path}; use Python modules or "
            "prefix documentation placeholders with _"
        )
    if path.stem != "__init__":
        _require_identifier(path.stem, path, binding)
    return 1


def _reject_symlinks_below(directory: Path, binding: _AuthoredNamespace) -> None:
    """Validate ignored trees because copied caches must not escape the root."""

    for path in directory.iterdir():
        if path.is_symlink():
            raise LibraryConventionError(
                f"{binding.label} cannot contain symlinks: {path}"
            )
        if path.is_dir():
            _reject_symlinks_below(path, binding)
        elif not path.is_file():
            raise LibraryConventionError(
                f"unsupported {binding.label} entry: {path}"
            )


def _reject_local_collisions(
    directory: Path, entries: list[Path], binding: _AuthoredNamespace
) -> None:
    """Reject platform-dependent module collisions before source is bundled."""

    names: dict[str, Path] = {}
    for path in entries:
        if path.name.startswith(".") or path.name in _IGNORED_DIRECTORIES:
            continue
        import_name = path.stem if path.is_file() and path.suffix == ".py" else path.name
        key = import_name.casefold()
        previous = names.get(key)
        if previous is not None:
            raise LibraryConventionError(
                f"{binding.label} import collision in {directory}: {previous.name} "
                f"and {path.name} both resolve as {import_name!r}"
            )
        names[key] = path


def _require_identifier(
    name: str, path: Path, binding: _AuthoredNamespace
) -> None:
    """Require deterministic Python module names in authored namespaces."""

    if not name.isidentifier():
        raise LibraryConventionError(
            f"{binding.label} module names must be valid Python identifiers: {path}"
        )


def _activate_binding(binding: _AuthoredNamespace, root: Path) -> None:
    """Acquire an existing binding or install a new namespace package."""

    if binding.active_root is not None:
        _acquire_existing_binding(binding, root)
        return
    _install_namespace(binding, root)
    binding.active_root = root
    binding.active_references = 1


def _acquire_existing_binding(binding: _AuthoredNamespace, root: Path) -> None:
    """Reference-count nested compiler use of the same authored root."""

    if binding.active_root != root:
        raise LibraryConventionError(
            f"{binding.namespace} is already bound to {binding.active_root}; "
            f"cannot also bind {root} in the same process"
        )
    binding.active_references += 1


def _install_namespace(binding: _AuthoredNamespace, root: Path) -> None:
    """Install one namespace package and execute its optional initializer."""

    existing = sys.modules.get(binding.namespace)
    if existing is not None:
        raise LibraryConventionError(
            f"{binding.namespace} is already provided by another package; remove "
            "that namespace collision before compiling the agent"
        )
    initializer = root / "__init__.py"
    module = (
        _initialized_package(binding, initializer, root)
        if initializer.is_file()
        else _namespace_package(binding, root)
    )
    sys.modules[binding.namespace] = module
    _set_parent_attribute(binding, module)
    if initializer.is_file():
        _execute_initializer(binding, module, initializer)


def _namespace_package(binding: _AuthoredNamespace, root: Path) -> ModuleType:
    """Create a namespace package for a folder without ``__init__.py``."""

    module = ModuleType(binding.namespace)
    module.__package__ = binding.namespace
    module.__path__ = [str(root)]  # type: ignore[attr-defined]
    module.__file__ = None
    return module


def _initialized_package(
    binding: _AuthoredNamespace, initializer: Path, root: Path
) -> ModuleType:
    """Create a regular package module for an authored initializer."""

    spec = importlib.util.spec_from_file_location(
        binding.namespace,
        initializer,
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise LibraryImportError(f"cannot create an import spec for {initializer}")
    return importlib.util.module_from_spec(spec)


def _execute_initializer(
    binding: _AuthoredNamespace, module: ModuleType, initializer: Path
) -> None:
    """Execute an initializer and remove partial namespace state on failure."""

    spec = module.__spec__
    try:
        if spec is None or spec.loader is None:
            raise LibraryImportError(
                f"cannot load {binding.label} initializer: {initializer}"
            )
        spec.loader.exec_module(module)
    except Exception as exc:
        _remove_namespace_modules(binding)
        if isinstance(exc, LibraryImportError):
            raise
        raise LibraryImportError(
            f"failed to import {binding.label} initializer {initializer}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _set_parent_attribute(binding: _AuthoredNamespace, module: ModuleType) -> None:
    """Expose the authored namespace as an attribute of the Harnest package."""

    parent = sys.modules.get("harnest")
    if parent is not None:
        setattr(parent, binding.directory, module)


def _release_binding(binding: _AuthoredNamespace, root: Path) -> None:
    """Release one reference when it belongs to the supplied agent root."""

    if binding.active_root != root:
        return
    binding.active_references -= 1
    if binding.active_references > 0:
        return
    _remove_namespace_modules(binding)
    binding.active_root = None
    binding.active_references = 0


def _remove_namespace_modules(binding: _AuthoredNamespace) -> None:
    """Remove the namespace and every imported child module from the process."""

    for name in tuple(sys.modules):
        if name == binding.namespace or name.startswith(f"{binding.namespace}."):
            sys.modules.pop(name, None)
    parent = sys.modules.get("harnest")
    if parent is not None and hasattr(parent, binding.directory):
        delattr(parent, binding.directory)


__all__ = [
    "LibraryConventionError",
    "LibraryImportError",
    "activate_authored_library",
    "authored_library",
    "release_authored_library",
]

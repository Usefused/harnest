"""Process-scoped namespace for authored reusable Python modules."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from threading import RLock
from types import ModuleType
from typing import Iterator

_NAMESPACE = "harnest.lib"
_IGNORED_DIRECTORIES = {"__pycache__"}
_lock = RLock()
_active_root: Path | None = None
_active_references = 0


class LibraryConventionError(RuntimeError):
    """An authored library does not follow the safe namespace convention."""


class LibraryImportError(RuntimeError):
    """An authored library package initializer could not be imported."""


def activate_authored_library(bundle_root: Path) -> bool:
    """Bind ``harnest.lib`` to one validated agent root.

    Returns whether a binding was acquired. Empty or absent ``lib/`` folders do
    not reserve the namespace, matching every other optional Harnest folder.
    """

    library_root = _validated_library_root(bundle_root)
    if library_root is None:
        return False
    with _lock:
        if _active_root is not None:
            _acquire_existing_binding(library_root)
            return True
        _install_namespace(library_root)
        _set_active_binding(library_root)
    return True


def release_authored_library(bundle_root: Path) -> None:
    """Release one binding previously acquired for ``bundle_root``."""

    library_root = (bundle_root / "lib").resolve()
    with _lock:
        if _active_root != library_root:
            return
        _release_active_binding()


@contextmanager
def authored_library(bundle_root: Path) -> Iterator[None]:
    """Activate an authored library for a bounded compiler operation."""

    acquired = activate_authored_library(bundle_root)
    try:
        yield
    finally:
        if acquired:
            release_authored_library(bundle_root)


def _validated_library_root(bundle_root: Path) -> Path | None:
    root = bundle_root / "lib"
    if root.is_symlink():
        raise LibraryConventionError(f"authored lib directory cannot be a symlink: {root}")
    if not root.exists():
        return None
    if not root.is_dir():
        raise LibraryConventionError(f"authored lib path must be a directory: {root}")
    python_files = _validate_directory(root)
    return root.resolve() if python_files else None


def _validate_directory(directory: Path) -> int:
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    _reject_local_collisions(directory, entries)
    python_files = 0
    for path in entries:
        if path.is_symlink():
            raise LibraryConventionError(
                f"authored lib cannot contain symlinks: {path}"
            )
        if path.is_dir():
            python_files += _validate_library_directory(path)
        elif path.is_file():
            python_files += _validate_library_file(path)
        else:
            raise LibraryConventionError(f"unsupported authored lib entry: {path}")
    return python_files


def _validate_library_directory(path: Path) -> int:
    if path.name in _IGNORED_DIRECTORIES or path.name.startswith("."):
        _reject_symlinks_below(path)
        return 0
    _require_identifier(path.name, path)
    return _validate_directory(path)


def _validate_library_file(path: Path) -> int:
    if path.name.startswith("."):
        return 0
    if path.suffix != ".py":
        # Init creates _README.md files so empty optional folders remain useful
        # without accidentally turning documentation into runtime resources.
        if path.name.startswith("_"):
            return 0
        raise LibraryConventionError(
            f"unexpected file in authored lib: {path}; use Python modules or "
            "prefix documentation placeholders with _"
        )
    if path.stem != "__init__":
        _require_identifier(path.stem, path)
    return 1


def _reject_symlinks_below(directory: Path) -> None:
    """Validate ignored trees because copied or cached files must not escape root."""

    for path in directory.iterdir():
        if path.is_symlink():
            raise LibraryConventionError(
                f"authored lib cannot contain symlinks: {path}"
            )
        if path.is_dir():
            _reject_symlinks_below(path)
        elif not path.is_file():
            raise LibraryConventionError(f"unsupported authored lib entry: {path}")


def _reject_local_collisions(directory: Path, entries: list[Path]) -> None:
    names: dict[str, Path] = {}
    for path in entries:
        if path.name.startswith(".") or path.name in _IGNORED_DIRECTORIES:
            continue
        import_name = path.stem if path.is_file() and path.suffix == ".py" else path.name
        key = import_name.casefold()
        previous = names.get(key)
        if previous is not None:
            raise LibraryConventionError(
                f"authored lib import collision in {directory}: {previous.name} and "
                f"{path.name} both resolve as {import_name!r}"
            )
        names[key] = path


def _require_identifier(name: str, path: Path) -> None:
    if not name.isidentifier():
        raise LibraryConventionError(
            f"authored lib module names must be valid Python identifiers: {path}"
        )


def _acquire_existing_binding(library_root: Path) -> None:
    global _active_references
    if _active_root != library_root:
        raise LibraryConventionError(
            f"harnest.lib is already bound to {_active_root}; cannot also bind "
            f"{library_root} in the same process"
        )
    _active_references += 1


def _install_namespace(library_root: Path) -> None:
    existing = sys.modules.get(_NAMESPACE)
    if existing is not None:
        raise LibraryConventionError(
            "harnest.lib is already provided by another package; remove that "
            "namespace collision before compiling the agent"
        )
    initializer = library_root / "__init__.py"
    module = (
        _initialized_package(initializer, library_root)
        if initializer.is_file()
        else _namespace_package(library_root)
    )
    sys.modules[_NAMESPACE] = module
    _set_parent_attribute(module)
    if initializer.is_file():
        _execute_initializer(module, initializer)


def _namespace_package(library_root: Path) -> ModuleType:
    module = ModuleType(_NAMESPACE)
    module.__package__ = _NAMESPACE
    module.__path__ = [str(library_root)]  # type: ignore[attr-defined]
    module.__file__ = None
    return module


def _initialized_package(initializer: Path, library_root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        _NAMESPACE,
        initializer,
        submodule_search_locations=[str(library_root)],
    )
    if spec is None or spec.loader is None:
        raise LibraryImportError(f"cannot create an import spec for {initializer}")
    return importlib.util.module_from_spec(spec)


def _execute_initializer(module: ModuleType, initializer: Path) -> None:
    spec = module.__spec__
    try:
        if spec is None or spec.loader is None:
            raise LibraryImportError(f"cannot load authored lib initializer: {initializer}")
        spec.loader.exec_module(module)
    except Exception as exc:
        _remove_namespace_modules()
        if isinstance(exc, LibraryImportError):
            raise
        raise LibraryImportError(
            f"failed to import authored lib initializer {initializer}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _set_parent_attribute(module: ModuleType) -> None:
    parent = sys.modules.get("harnest")
    if parent is not None:
        setattr(parent, "lib", module)


def _set_active_binding(library_root: Path) -> None:
    global _active_root, _active_references
    _active_root = library_root
    _active_references = 1


def _release_active_binding() -> None:
    global _active_root, _active_references
    _active_references -= 1
    if _active_references > 0:
        return
    _remove_namespace_modules()
    _active_root = None
    _active_references = 0


def _remove_namespace_modules() -> None:
    for name in tuple(sys.modules):
        if name == _NAMESPACE or name.startswith(f"{_NAMESPACE}."):
            sys.modules.pop(name, None)
    parent = sys.modules.get("harnest")
    if parent is not None and hasattr(parent, "lib"):
        delattr(parent, "lib")


__all__ = [
    "LibraryConventionError",
    "LibraryImportError",
    "activate_authored_library",
    "authored_library",
    "release_authored_library",
]

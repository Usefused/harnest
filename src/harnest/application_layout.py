"""Resolve canonical and legacy lifecycle roots without importing user code."""

from pathlib import Path


class ApplicationLayoutError(ValueError):
    """A project mixes incompatible lifecycle and extension layouts."""


def lifecycle_directory(root: Path) -> Path:
    """Select one lifecycle tree; never interpret extension packages as hooks."""

    current, previous = root / "lifecycle", root / "extensions"
    _validate_directories(current, previous)
    entries = public_entries(previous)
    packages = tuple(item for item in entries if _is_extension_package(item))
    if packages or current.exists():
        unexpected = tuple(item for item in entries if item not in packages)
        if unexpected:
            raise ApplicationLayoutError(
                f"{unexpected[0]} is not a Harnest Extension package. "
                "Put lifecycle hooks and resource factories in lifecycle/, and "
                "reusable packages in extensions/<name>/ with extension.yaml and "
                "extension.py. Run `harnest upgrade <project>` to preview migration; "
                "resolve existing destination folders before applying it."
            )
        return current
    # Legacy projects remain readable until an explicit, backed-up upgrade.
    # Empty guide-only folders carry no executable content to reinterpret.
    return previous if entries else current


def _validate_directories(*directories: Path) -> None:
    """Reject invalid containers independently of the format-selection policy."""

    for directory in directories:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ApplicationLayoutError(f"{directory} must be a regular directory")


def public_entries(directory: Path) -> tuple[Path, ...]:
    """Ignore guides and caches while rejecting even hidden symlink entries."""

    if not directory.exists():
        return ()
    entries = tuple(sorted(directory.iterdir()))
    for item in entries:
        if item.is_symlink():
            raise ApplicationLayoutError(f"application resource cannot be a symlink: {item}")
    return tuple(item for item in entries if not item.name.startswith(("_", ".")))


def _is_extension_package(directory: Path) -> bool:
    """Recognize package markers even when incomplete; never import them as hooks."""

    return directory.is_dir() and any(
        (directory / name).exists() or (directory / name).is_symlink()
        for name in ("extension.yaml", "extension.py")
    )

"""Record and enforce the framework selected by a project's isolated environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

import yaml
from packaging.version import Version, InvalidVersion

from .compatibility import FrameworkCompatibilityError, validate_framework_compatibility
from .upgrade import PROJECT_SCHEMA


def read_project_lock(root: Path) -> dict[str, Any]:
    """Read a bounded regular lock without following authored links or dropping keys."""
    path = root / "harnest.lock"
    if not path.exists() and not path.is_symlink():
        return {"apiVersion": "harnest.dev/v1alpha1", "kind": "ProjectLock", "projectSchema": PROJECT_SCHEMA}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise FrameworkCompatibilityError("harnest.lock must be a regular file of at most 64KiB")
    # Reuse duplicate-key rejection so pins cannot mean different things to Go and Python.
    from .server_config import _UniqueKeyLoader
    value = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    if not isinstance(value, dict) or value.get("apiVersion") != "harnest.dev/v1alpha1" or value.get("kind") != "ProjectLock":
        raise FrameworkCompatibilityError("invalid Harnest project lock")
    schema = value.get("projectSchema")
    if type(schema) is not int or not 0 <= schema <= PROJECT_SCHEMA:
        raise FrameworkCompatibilityError("unsupported harnest.lock projectSchema")
    return value


def framework_pin(value: dict[str, Any]) -> dict[str, str] | None:
    """Accept an exact public version, never a dependency specifier or URL."""
    pin = value.get("framework")
    if pin is None:
        return None
    from .compatibility import FRAMEWORK_COMPATIBILITY
    if not isinstance(pin, dict) or set(pin) != {"name", "distribution", "version"}:
        raise FrameworkCompatibilityError("harnest.lock framework requires name, distribution, and version")
    contract = FRAMEWORK_COMPATIBILITY.get(pin["name"]) if isinstance(pin["name"], str) else None
    if contract is None or pin["distribution"] != contract.distribution:
        raise FrameworkCompatibilityError("harnest.lock framework distribution does not match its name")
    try:
        if not isinstance(pin["version"], str) or str(Version(pin["version"])) != pin["version"]:
            raise InvalidVersion("not a canonical version")
    except InvalidVersion as error:
        raise FrameworkCompatibilityError("harnest.lock framework version must be an exact canonical version") from error
    return pin


def verify_framework_lock(root: Path, resolved: Any) -> None:
    """Reject drift before importing authored code; old schema-only locks remain valid."""
    pin = framework_pin(read_project_lock(root))
    if pin is not None and pin != _resolved_pin(resolved):
        raise FrameworkCompatibilityError(
            "installed framework differs from harnest.lock; run harnest env sync "
            "to install the locked version (or remove the framework entry to resolve a new version)"
        )


def _resolved_pin(resolved: Any) -> dict[str, str]:
    """Keep the authored lock independent of transient installation paths."""
    return {"name": resolved.framework, "distribution": resolved.distribution,
            "version": str(Version(resolved.distribution_version))}


def resolve_project_lock(root: Path, framework: str, *, frozen: bool = False) -> None:
    """Publish the successfully installed version atomically, preserving schema metadata."""
    value = read_project_lock(root)
    previous = framework_pin(value)
    resolved = _resolved_pin(validate_framework_compatibility(framework))
    if previous == resolved:
        return
    if frozen or (previous is not None and previous["name"] == framework):
        raise FrameworkCompatibilityError("framework resolution differs from harnest.lock; frozen sync requires an existing matching pin")
    value["framework"] = resolved
    path = root / "harnest.lock"
    fd, temporary = tempfile.mkstemp(prefix=".harnest-lock-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(value, stream, sort_keys=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    """Run inside the installed project interpreter, never the CLI bootstrap Python."""
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("framework", choices=("adk", "langgraph"))
    parser.add_argument("--frozen", action="store_true")
    args = parser.parse_args()
    resolve_project_lock(args.directory, args.framework, frozen=args.frozen)


if __name__ == "__main__":
    main()

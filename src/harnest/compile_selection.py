"""Inert compile-only selections maintained by agents or team project packs."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

import yaml

import tomllib

from .server_config import _UniqueKeyLoader

FILENAME = "harnest-compile.yaml"
REPORT = "harnest-build-report.json"
_EXTRA = re.compile(r"[A-Za-z0-9]+([._-][A-Za-z0-9]+)*\Z")


def read_compile_selection(root: Path) -> dict[str, Any]:
    """Validate declarations before authored Python or project pack code can run."""
    path = root / FILENAME
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "extras": []}
    try:
        document = yaml.load(_read_regular_file(path), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid {FILENAME}: {exc}") from exc
    _validate_document(document)
    result = {"version": 1, "extras": _string_list(document.get("extras", []), "extras")}
    for extra in result["extras"]:
        if not _EXTRA.fullmatch(extra):
            raise ValueError(f"invalid compile extra: {extra!r}")
    _selected_requirements(root, result["extras"])
    return result


def _validate_document(document: Any) -> None:
    """Reject unsupported versions and unknown keys instead of ignoring team intent."""
    if not isinstance(document, dict):
        raise ValueError(f"{FILENAME} must be a mapping")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError(f"{FILENAME} requires version: 1")
    if set(document) - {"version", "extras"}:
        raise ValueError(f"unknown {FILENAME} field")


def _string_list(value: Any, label: str) -> list[str]:
    """Canonicalize repeated declarations while retaining strict input types."""
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{FILENAME}.{label} must be a list of non-empty strings")
    return sorted(set(value))


def _project_relative_path(value: str) -> Path:
    """Require exact portable relative paths without glob or traversal semantics."""
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError(f"dependency file must be an exact project-relative path: {value!r}")
    if any(char in value for char in "\\:\x00*?["):
        raise ValueError(f"invalid dependency file path: {value!r}")
    return Path(value)


def write_compile_report(directory: Path, selection: dict[str, Any] | None) -> None:
    """Record actual artifact bytes and inclusion reasons without inspecting runtime imports."""
    if selection is None:
        return
    files = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        files.append({"path": relative, "size": path.stat().st_size,
                      "reasons": ["authored source" if relative.startswith("source/") else "generated artifact"]})
    report = {"version": 1, "kind": "agent", "selection": selection,
              "logicalBytes": sum(item["size"] for item in files), "files": files,
              "selectedRequirements": _selected_requirements(directory / "source", selection["extras"])}
    (directory / REPORT).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _selected_requirements(root: Path, extras: list[str]) -> list[str]:
    """Validate direct Python compilation against the same PEP 621 declarations as the CLI."""
    if not extras:
        return []
    dependency_file = "pyproject.toml"
    config = root / "config.yaml"
    if config.is_file():
        settings = yaml.load(config.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        dependency_file = settings.get("spec", {}).get("runtime", {}).get("dependencyFile", dependency_file)
    path = root / _project_relative_path(dependency_file)
    project = tomllib.loads(_read_regular_file(path)).get("project", {})
    groups = project.get("optional-dependencies", {})
    requirements = []
    for extra in extras:
        if extra not in groups:
            raise ValueError(f"unknown compile extra: {extra}")
        requirements.extend(_string_list(groups[extra], f"optional-dependencies.{extra}"))
    return sorted(set(requirements))


def _read_regular_file(path: Path) -> str:
    """Bound configuration reads and reject links before decoding any declarations."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"{path.name} must be a regular file under 16 MiB")
    return path.read_text(encoding="utf-8")

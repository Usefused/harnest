"""Migrate retired Python declarations through the normal backed-up upgrade plan."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import tomllib

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
import yaml

from .upgrade import UpgradeAction, UpgradeError, _rewrite


def plan_python_minimum(root: Path, actions: list[UpgradeAction], blockers: list[str]) -> None:
    """Compose Python 3.10 migration with pending legacy dependency-file changes."""
    try:
        config = _planned_source(root, "config.yaml", actions)
        node = _runtime_version(config)
        if node.value != "3.10":
            return
        project = _planned_source(root, "pyproject.toml", actions)
        updated_project = _project_source(project)
        updated_config = _config_source(config, node)
    except (UpgradeError, OSError, ValueError, yaml.YAMLError) as exc:
        blockers.append(f"Python upgrade: {exc}")
        return
    # Resolve both edits before recording either: an incompatible custom range
    # must not produce a partially applicable migration.
    _record(root, "config.yaml", updated_config, actions)
    if updated_project != project:
        _record(root, "pyproject.toml", updated_project, actions)


def _planned_source(root: Path, name: str, actions: list[UpgradeAction]) -> str:
    """Read pending content first so requirements.txt upgrades remain one transaction."""
    for action in actions:
        if action.path == name and action.kind in {"create", "rewrite"}:
            return action.content or ""
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise UpgradeError(f"{name} must be a regular file")
    return path.read_text(encoding="utf-8")


def _runtime_version(source: str) -> yaml.ScalarNode:
    """Locate the exact runtime scalar without reserializing user-owned YAML."""
    node = yaml.compose(source)
    for name in ("spec", "runtime", "version"):
        if not isinstance(node, yaml.MappingNode):
            raise UpgradeError("cannot locate spec.runtime.version in config.yaml")
        matches = [value for key, value in node.value if key.value == name]
        if len(matches) != 1:
            raise UpgradeError(f"config.yaml requires one {name} field")
        node = matches[0]
    if not isinstance(node, yaml.ScalarNode):
        raise UpgradeError("spec.runtime.version must be a scalar")
    return node


def _config_source(source: str, node: yaml.ScalarNode) -> str:
    """Replace only a literal version, retaining comments, ordering, and formatting."""
    start, end = node.start_mark.index, node.end_mark.index
    if not re.fullmatch(r'''(?:3\.10|"3\.10"|'3\.10')''', source[start:end]):
        raise UpgradeError("use a literal spec.runtime.version before upgrading Python")
    return source[:start] + '"3.11"' + source[end:]


def _python_requirement(value: str) -> str:
    """Raise the floor and shift only standard minor-specific 3.10 constraints."""
    try:
        requirements = SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise UpgradeError("invalid project.requires-python") from exc
    updated = [_updated_specifier(item) for item in requirements]
    # A floor also covers missing requirements and ranges such as <4.
    result = SpecifierSet(",".join([*updated, ">=3.11"]))
    if not result.contains("3.11"):
        raise UpgradeError("project.requires-python excludes Python 3.11; choose a compatible range before applying")
    return str(result)


def _updated_specifier(item) -> str:
    """Keep authored upper bounds and exclusions except the retired minor interval."""
    special = {"<3.11": "<3.12", "<3.11.0": "<3.12", "==3.10.*": "==3.11.*",
               "~=3.10": "~=3.11", "~=3.10.0": "~=3.11.0"}
    if str(item) in special:
        return special[str(item)]
    if item.operator in {">=", ">"} and Version(item.version) < Version("3.11"):
        return ">=3.11"
    return str(item)


def _project_source(source: str) -> str:
    """Edit the project requirement while preserving dependencies and TOML comments."""
    project = tomllib.loads(source).get("project", {})
    if not isinstance(project, dict):
        raise UpgradeError("pyproject.toml requires a project table")
    if "requires-python" in project.get("dynamic", []):
        raise UpgradeError("project.requires-python is dynamic; choose a static range before applying")
    value = project.get("requires-python", "")
    if not isinstance(value, str):
        raise UpgradeError("project.requires-python must be a string")
    requirement = _python_requirement(value)
    # Scope the edit to [project]; similarly named tool configuration is untouched.
    section = re.search(r"(?m)^\[project\][^\S\n]*(?:#[^\n]*)?\n(?P<body>(?:(?!^\[).*(?:\n|$))*)", source)
    if section is None:
        raise UpgradeError("pyproject.toml requires an explicit [project] table for Python migration")
    updated = _replace_requirement(source, section, requirement, "requires-python" in project)
    if tomllib.loads(updated)["project"]["requires-python"] != requirement:
        raise UpgradeError("cannot safely update project.requires-python")
    return updated


def _replace_requirement(source: str, section: re.Match, requirement: str, present: bool) -> str:
    """Replace one simple string assignment or insert the previously omitted field."""
    body = section.group("body")
    field = re.search(r'''(?m)^[ \t]*(?:requires-python|"requires-python"|'requires-python')[ \t]*=[ \t]*(?P<value>"[^"\n]*"|'[^'\n]*')''', body)
    if field is None:
        if present:
            raise UpgradeError("use a single-line project.requires-python string before upgrading")
        start = end = section.start("body")
        replacement = f"requires-python = {json.dumps(requirement)}\n"
    else:
        start = section.start("body") + field.start("value")
        end = section.start("body") + field.end("value")
        replacement = json.dumps(requirement)
    return source[:start] + replacement + source[end:]


def _record(root: Path, name: str, source: str, actions: list[UpgradeAction]) -> None:
    """Retain original digests and create semantics when composing earlier edits."""
    detail = "upgrade Python 3.10 to the supported Python 3.11 minimum"
    for index, action in enumerate(actions):
        if action.path == name and action.kind in {"create", "rewrite"}:
            actions[index] = replace(action, content=source, detail=f"{action.detail}; {detail}")
            return
    actions.append(_rewrite(root, root / name, source, detail))

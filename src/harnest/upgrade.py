"""Plan and apply explicit filesystem-contract upgrades for Harnest agents."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import textwrap
import uuid
from typing import Any, Iterable

import yaml

from .server_config import DEFAULT_SERVER_YAML


PROJECT_SCHEMA = 2
PROJECT_LOCK = """apiVersion: harnest.dev/v1alpha1
kind: ProjectLock
projectSchema: 2
"""
_FRAMEWORK_DEPENDENCIES = {
    "adk": ("google-adk[eval,extensions,mcp]>=2.8,<3",),
    "langgraph": (
        "langgraph>=1.2,<2",
        "langchain>=1.3,<2",
        "langchain-litellm>=0.7,<1",
        "langchain-mcp-adapters>=0.3,<1",
    ),
}
_COMPILER_OWNED_DISTRIBUTIONS = frozenset({"harnest"}) | frozenset(
    re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9._-]+", value).group(0).lower())
    for dependencies in _FRAMEWORK_DEPENDENCIES.values()
    for value in dependencies
)
_PORTABLE_PHASES = (
    "authenticate",
    "before_invoke",
    "after_invoke",
    "on_event",
    "on_error",
    "before_model",
    "after_model",
    "on_model_error",
)


class UpgradeError(RuntimeError):
    """An agent cannot be safely planned or upgraded."""


@dataclass(frozen=True, slots=True)
class UpgradeAction:
    """One reviewed filesystem mutation in an upgrade plan."""

    kind: str
    path: str
    detail: str
    destination: str | None = None
    content: str | None = field(default=None, repr=False)
    digest: str | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        value = {"kind": self.kind, "path": self.path, "detail": self.detail}
        if self.destination is not None:
            value["destination"] = self.destination
        if self.digest is not None:
            value["sourceSha256"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """A deterministic, reviewable plan for one agent repository."""

    root: Path
    framework: str
    actions: tuple[UpgradeAction, ...]
    blockers: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "apiVersion": "harnest.dev/v1alpha1",
            "kind": "UpgradePlan",
            "projectSchema": PROJECT_SCHEMA,
            "agentDirectory": str(self.root),
            "framework": self.framework,
            "actions": [item.public() for item in self.actions],
            "blockers": list(self.blockers),
        }


def plan_upgrade(directory: str | Path) -> UpgradePlan:
    """Inspect an existing repository without importing authored Python."""

    root = _agent_root(directory)
    framework = _framework(root / "config.yaml")
    actions: list[UpgradeAction] = []
    blockers: list[str] = []
    _plan_server(root, actions, blockers)
    _plan_project_lock(root, actions, blockers)
    _plan_dependencies(root, framework, actions, blockers)
    _plan_mcp(root, actions, blockers)
    _plan_extensions(root, framework, actions, blockers)
    return UpgradePlan(
        root,
        framework,
        tuple(sorted(actions, key=_action_order)),
        tuple(sorted(set(blockers))),
    )


def render_upgrade_plan(plan: UpgradePlan, *, applying: bool = False) -> str:
    """Render a plan before any explicit apply mutates the repository."""

    heading = (
        "Harnest repository upgrade plan (applying)"
        if applying
        else "Harnest repository upgrade plan (read-only)"
    )
    lines = [
        heading,
        f"Agent: {plan.root}",
        f"Framework: {plan.framework}",
        f"Target project schema: {PROJECT_SCHEMA}",
        "",
    ]
    lines.extend(_render_actions(plan.actions))
    lines.extend(_render_blockers(plan.blockers))
    if not plan.actions and not plan.blockers:
        lines.append("No repository changes are required.")
    elif not applying and not plan.blockers:
        lines.extend(("", "Run the same command with --apply to perform exactly these classes of changes."))
    return "\n".join(lines) + "\n"


def apply_upgrade(plan: UpgradePlan) -> Path | None:
    """Apply a fresh plan after backing up every existing mutation target."""

    if plan.blockers:
        raise UpgradeError(
            "upgrade has manual blockers; resolve them and rerun the read-only plan"
        )
    if not plan.actions:
        return None
    # Verify the whole reviewed input before creating even the backup directory.
    # This keeps a stale plan strictly non-mutating.
    for action in plan.actions:
        _verify_action_source(plan.root, action)
    backup = plan.root / ".harnest" / "upgrade-backups" / uuid.uuid4().hex
    backup.mkdir(parents=True, exist_ok=False)
    (backup / "plan.json").write_text(
        json.dumps(plan.public(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Snapshot every source before the first authored-file mutation so all
    # recovery material reflects the reviewed pre-upgrade repository.
    for action in plan.actions:
        _backup_action(plan.root, backup, action)
    for action in plan.actions:
        _apply_action(plan.root, action)
    return backup


def _agent_root(directory: str | Path) -> Path:
    raw = Path(directory)
    if raw.is_symlink():
        raise UpgradeError(f"agent directory cannot be a symlink: {raw}")
    root = raw.resolve()
    if not root.is_dir():
        raise UpgradeError(f"agent directory is invalid: {root}")
    for required in ("config.yaml", "agent.py", "instructions.md", "agent-card.yaml"):
        path = root / required
        if path.is_symlink() or not path.is_file():
            raise UpgradeError(f"agent repository is missing a regular {required}: {path}")
    return root


def _framework(path: Path) -> str:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        framework = value["spec"]["framework"]["name"]
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise UpgradeError(f"cannot read framework from {path}") from exc
    if framework not in {"adk", "langgraph"}:
        raise UpgradeError(f"unsupported framework in {path}: {framework!r}")
    return framework


def _plan_server(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    path = root / "server.yaml"
    if path.is_symlink() or path.exists() and not path.is_file():
        blockers.append("server.yaml must be a regular file")
        return
    if not path.exists():
        actions.append(
            UpgradeAction(
                "create",
                "server.yaml",
                "add standalone HTTP, streaming, live, limits, and playground policy",
                content=DEFAULT_SERVER_YAML,
            )
        )


def _plan_project_lock(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    path = root / "harnest.lock"
    if path.is_symlink():
        blockers.append("harnest.lock is a symlink")
        return
    if not path.exists():
        actions.append(
            UpgradeAction(
                "create",
                "harnest.lock",
                "record the committed Harnest project schema for future migrations",
                content=PROJECT_LOCK,
            )
        )
        return
    try:
        api_version, kind, schema = _project_lock_values(path)
    except UpgradeError:
        blockers.append("harnest.lock is not a valid Harnest project lock")
        return
    _plan_project_schema(root, path, api_version, kind, schema, actions, blockers)


def _plan_project_schema(
    root: Path,
    path: Path,
    api_version: Any,
    kind: Any,
    schema: Any,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    if api_version != "harnest.dev/v1alpha1" or kind != "ProjectLock":
        blockers.append("harnest.lock has an unsupported apiVersion or kind")
    elif not isinstance(schema, int) or isinstance(schema, bool) or schema < 0:
        blockers.append("harnest.lock projectSchema must be a non-negative integer")
    elif schema > PROJECT_SCHEMA:
        blockers.append(f"harnest.lock project schema {schema!r} is newer than this CLI")
    elif schema < PROJECT_SCHEMA:
        actions.append(
            _rewrite(
                root,
                path,
                PROJECT_LOCK,
                f"advance the project schema from {schema} to {PROJECT_SCHEMA}",
            )
        )


def _project_lock_values(path: Path) -> tuple[Any, Any, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value["apiVersion"], value["kind"], value["projectSchema"]
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise UpgradeError(f"cannot read project lock: {path}") from exc


def _plan_dependencies(
    root: Path,
    framework: str,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    config_path = root / "config.yaml"
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        runtime = value["spec"]["runtime"]
        name = value["metadata"]["name"]
        python_version = str(runtime["version"])
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
        blockers.append(f"cannot inspect dependency contract in config.yaml: {type(exc).__name__}")
        return
    current = runtime.get("dependencyFile")
    legacy = runtime.get("requirementsFile")
    if current is not None:
        _validate_current_dependency_contract(root, current, blockers)
        return
    dependencies: tuple[str, ...] = ()
    legacy_path = None
    if legacy is not None:
        legacy_path = root / str(legacy)
        parsed = _legacy_requirements(root, legacy_path, blockers)
        if parsed is None:
            return
        # Framework packages are injected from the release wheel so an old
        # manifest cannot silently turn into a user-owned version override.
        dependencies = _agent_owned_requirements(parsed)
    pyproject = root / "pyproject.toml"
    if pyproject.exists() or pyproject.is_symlink():
        blockers.append("pyproject.toml already exists while config.yaml uses the legacy dependency contract")
        return
    try:
        config_source = _dependency_config_source(config_path)
        pyproject_source = _agent_pyproject(str(name), python_version, dependencies)
    except UpgradeError as exc:
        blockers.append(str(exc))
        return
    actions.append(_rewrite(root, config_path, config_source, "select the isolated pyproject dependency environment"))
    actions.append(
        UpgradeAction(
            "create",
            "pyproject.toml",
            "replace the legacy requirements manifest with a lockable uv project",
            content=pyproject_source,
        )
    )
    if legacy_path is not None:
        actions.append(
            UpgradeAction(
                "delete",
                _relative(root, legacy_path),
                "remove the superseded requirements manifest after backing it up",
                digest=_file_digest(legacy_path),
            )
        )


def _validate_current_dependency_contract(
    root: Path, value: Any, blockers: list[str]
) -> None:
    if value != "pyproject.toml":
        blockers.append("config.yaml runtime.dependencyFile must be pyproject.toml")
        return
    path = root / "pyproject.toml"
    if path.is_symlink() or not path.is_file():
        blockers.append("pyproject.toml must be a regular file")


def _legacy_requirements(
    root: Path, path: Path, blockers: list[str]
) -> tuple[str, ...] | None:
    try:
        relative = _relative(root, path.resolve(strict=False))
    except ValueError:
        blockers.append("legacy requirementsFile escapes the agent directory")
        return None
    if path.is_symlink() or not path.is_file():
        blockers.append(f"legacy dependency file must be regular: {relative}")
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        blockers.append(f"legacy dependency file is not readable UTF-8: {relative}")
        return None
    dependencies = []
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith("-"):
            blockers.append(f"legacy dependency option requires manual migration in {relative}: {value.split()[0]}")
            return None
        dependencies.append(value)
    return tuple(dependencies)


def _requirement_distribution(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9._-]+", requirement.strip())
    if match is None:
        return ""
    return re.sub(r"[-_.]+", "-", match.group(0).lower())


def _agent_owned_requirements(requirements: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        value
        for value in requirements
        if _requirement_distribution(value) not in _COMPILER_OWNED_DISTRIBUTIONS
    )


def _dependency_config_source(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("requirementsFile:"):
            indent = line[: len(line) - len(line.lstrip())]
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{indent}dependencyFile: pyproject.toml{newline}"
            return "".join(lines)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("version:") and len(line) - len(line.lstrip()) >= 4:
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(index + 1, f"{indent}dependencyFile: pyproject.toml\n")
            return "".join(lines)
    raise UpgradeError(f"{path}: cannot locate spec.runtime.version for dependency migration")


def _agent_pyproject(
    name: str, python_version: str, dependencies: Iterable[str]
) -> str:
    dependency_lines = "".join(f"  {json.dumps(value)},\n" for value in dependencies)
    try:
        major, minor = python_version.split(".", 1)
        next_minor = int(minor) + 1
    except (TypeError, ValueError) as exc:
        raise UpgradeError(
            f"config.yaml runtime.version cannot form a Python range: {python_version!r}"
        ) from exc
    return (
        "[project]\n"
        f"name = {json.dumps(name)}\n"
        'version = "0.1.0"\n'
        f'requires-python = ">={major}.{minor},<{major}.{next_minor}"\n'
        "dependencies = [\n"
        f"{dependency_lines}]\n\n"
        "[dependency-groups]\n"
        'dev = ["pytest>=8,<9"]\n\n'
        "[tool.uv]\n"
        "package = false\n"
    )


def _plan_mcp(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    _plan_root_mcp(root, actions, blockers)
    _plan_plugin_mcp(root, actions, blockers)


def _plan_root_mcp(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    legacy = root / "mcp_servers"
    current = root / "mcp"
    if legacy.exists() or legacy.is_symlink():
        if legacy.is_symlink() or not legacy.is_dir():
            blockers.append("mcp_servers must be a regular directory before migration")
        elif current.exists() or current.is_symlink():
            blockers.append("both mcp_servers/ and mcp/ exist; merge them manually")
        else:
            _plan_mcp_directory(root, legacy, actions, blockers)
            digest = _safe_tree_digest(root, legacy, blockers)
            if digest is None:
                return
            actions.append(
                UpgradeAction(
                    "move",
                    "mcp_servers",
                    "rename the removed MCP folder convention",
                    destination="mcp",
                    digest=digest,
                )
            )
    else:
        _plan_mcp_directory(root, current, actions, blockers)


def _plan_plugin_mcp(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    plugins = root / "plugins"
    if plugins.is_dir() and not plugins.is_symlink():
        for child in sorted(plugins.iterdir()):
            if child.is_symlink():
                blockers.append(f"{_relative(root, child)} is a symlink")
        for directory in _public_directories(plugins):
            _plan_mcp_directory(root, directory / "mcp", actions, blockers)


def _plan_mcp_directory(
    root: Path,
    directory: Path,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        blockers.append(f"{_relative(root, directory)} is not a regular directory")
        return
    for child in sorted(directory.iterdir()):
        if child.is_symlink():
            blockers.append(f"{_relative(root, child)} is a symlink")
    for path in _public_python_files(directory):
        try:
            replacement = _mcp_factory_source(path)
        except UpgradeError as exc:
            blockers.append(str(exc))
            continue
        if replacement is not None:
            actions.append(_rewrite(root, path, replacement, "replace the legacy MCP value export with client()"))


def _mcp_factory_source(path: Path) -> str | None:
    source, module = _python_source(path)
    if _has_current_mcp_factory(path, module):
        return None
    assignments = [item for item in module.body if _assignment_name(item) == path.stem]
    if len(assignments) != 1:
        raise UpgradeError(
            f"{path}: expected one legacy export named {path.stem!r} or client()"
        )
    assignment = assignments[0]
    value = assignment.value
    expression = ast.get_source_segment(source, value)
    if expression is None:
        raise UpgradeError(f"{path}: cannot preserve the legacy MCP expression")
    body = textwrap.indent("return " + expression, "    ")
    replacement = f"def client():\n{body}"
    return _replace_node(source, assignment, replacement)


def _has_current_mcp_factory(path: Path, module: ast.Module) -> bool:
    factories = [
        item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == "client"
    ]
    if len(factories) > 1 or factories and isinstance(factories[0], ast.AsyncFunctionDef):
        raise UpgradeError(f"{path}: client() must be one synchronous factory")
    return bool(factories)


def _plan_extensions(
    root: Path,
    framework: str,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    directory = root / "extensions"
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        blockers.append("extensions/ must be a regular directory")
        return
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            blockers.append(f"{_relative(root, item)} is a symlink")
    for path in sorted(directory.rglob("*.py")):
        if _ignored(path.relative_to(directory).parts):
            continue
        _plan_extension_file(root, path, framework, actions, blockers)


def _plan_extension_file(
    root: Path,
    path: Path,
    framework: str,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    if path.is_symlink():
        blockers.append(f"{_relative(root, path)} is a symlink")
        return
    if path.name == "lifecycle.py":
        _plan_portable_extension(root, path, actions, blockers)
        return
    if path.name not in {"adk.py", "langgraph.py"}:
        return
    target_framework = path.stem
    if target_framework != framework:
        destination = path.with_name(f"_{path.name}")
        if destination.exists() or destination.is_symlink():
            blockers.append(f"cannot preserve {_relative(root, path)} because {_relative(root, destination)} exists")
            return
        actions.append(
            UpgradeAction(
                "move",
                _relative(root, path),
                f"make the inactive {target_framework} native extension private",
                destination=_relative(root, destination),
                digest=_file_digest(path),
            )
        )
        return
    try:
        replacement = _native_extension_source(path, framework)
    except UpgradeError as exc:
        blockers.append(str(exc))
        return
    if replacement is not None:
        actions.append(_rewrite(root, path, replacement, f"wrap the legacy {framework} native export in a lifecycle factory"))


def _plan_portable_extension(
    root: Path,
    path: Path,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    try:
        replacement = _portable_extension_source(path)
    except UpgradeError as exc:
        blockers.append(str(exc))
        return
    if replacement is not None:
        actions.append(_rewrite(root, path, replacement, "replace Extension(...) aggregation with lifecycle decorators"))


def _portable_extension_source(path: Path) -> str | None:
    source, module = _python_source(path)
    assignment = _extension_assignment(module)
    if assignment is None:
        if _has_legacy_extension(module):
            raise UpgradeError(
                f"{path}: expected exactly one legacy Extension(...) export named 'extension'"
            )
        return None
    callbacks = _extension_callbacks(path, assignment.value)
    functions = {
        item.name: item
        for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = sorted(set(callbacks.values()) - set(functions))
    if missing:
        raise UpgradeError(f"{path}: lifecycle callbacks must be local functions: {', '.join(missing)}")
    edits = [_node_edit(source, assignment, "")]
    edits.extend(_extension_import_edits(path, source, module))
    for phase, name in callbacks.items():
        function = functions[name]
        line = function.decorator_list[0].lineno if function.decorator_list else function.lineno
        edits.append((_line_offset(source, line), _line_offset(source, line), f"@lifecycle.{phase}\n"))
    return _apply_text_edits(source, edits)


def _extension_assignment(module: ast.Module) -> ast.Assign | ast.AnnAssign | None:
    found = [
        item
        for item in module.body
        if _assignment_name(item) == "extension"
        and isinstance(getattr(item, "value", None), ast.Call)
        and _call_name(item.value.func) == "Extension"
    ]
    return found[0] if len(found) == 1 else None


def _has_legacy_extension(module: ast.Module) -> bool:
    return any(
        isinstance(item, ast.ImportFrom) and item.module == "harnest.extension"
        for item in module.body
    ) or any(
        _assignment_name(item) == "extension"
        and isinstance(getattr(item, "value", None), ast.Call)
        and _call_name(item.value.func) == "Extension"
        for item in module.body
    )


def _extension_callbacks(path: Path, call: ast.Call) -> dict[str, str]:
    if call.args:
        raise UpgradeError(
            f"{path}: positional Extension arguments cannot be migrated automatically"
        )
    callbacks: dict[str, str] = {}
    for keyword in call.keywords:
        if keyword.arg in {None, "name"}:
            if keyword.arg is None:
                raise UpgradeError(f"{path}: Extension **kwargs cannot be migrated automatically")
            continue
        if keyword.arg not in _PORTABLE_PHASES:
            raise UpgradeError(f"{path}: unsupported legacy Extension field {keyword.arg!r}")
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
            continue
        if not isinstance(keyword.value, ast.Name):
            raise UpgradeError(f"{path}: Extension callback {keyword.arg} must name a local function")
        callbacks[keyword.arg] = keyword.value.id
    return callbacks


def _extension_import_edits(
    path: Path, source: str, module: ast.Module
) -> list[tuple[int, int, str]]:
    imports = [item for item in module.body if isinstance(item, ast.ImportFrom) and item.module == "harnest.extension"]
    if len(imports) != 1:
        raise UpgradeError(f"{path}: expected one import from harnest.extension")
    imported = [item.name for item in imports[0].names if item.name != "Extension"]
    names = sorted(set((*imported, "lifecycle")))
    replacement = f"from harnest.lifecycle import {', '.join(names)}"
    return [_node_edit(source, imports[0], replacement)]


def _native_extension_source(path: Path, framework: str) -> str | None:
    source, module = _python_source(path)
    phase = "adk_plugin" if framework == "adk" else "langgraph_middleware"
    if f"lifecycle.{phase}" in source:
        return None
    if not any(_assignment_name(item) == "extension" for item in module.body):
        raise UpgradeError(f"{path}: expected a legacy native export named 'extension'")
    suffix = (
        "\n\nfrom harnest.lifecycle import lifecycle as _harnest_lifecycle\n\n"
        f"@_harnest_lifecycle.{phase}\n"
        f"def harnest_{phase}():\n"
        "    return extension\n"
    )
    return source.rstrip() + suffix


def _python_source(path: Path) -> tuple[str, ast.Module]:
    try:
        source = path.read_text(encoding="utf-8")
        return source, ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise UpgradeError(f"{path}: cannot parse Python source") from exc


def _assignment_name(value: ast.AST) -> str | None:
    if isinstance(value, ast.Assign) and len(value.targets) == 1 and isinstance(value.targets[0], ast.Name):
        return value.targets[0].id
    if isinstance(value, ast.AnnAssign) and isinstance(value.target, ast.Name):
        return value.target.id
    return None


def _call_name(value: ast.AST) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _replace_node(source: str, node: ast.AST, replacement: str) -> str:
    return _apply_text_edits(source, [_node_edit(source, node, replacement)])


def _node_edit(source: str, node: ast.AST, replacement: str) -> tuple[int, int, str]:
    start = _position_offset(source, node.lineno, node.col_offset)
    end = _position_offset(source, node.end_lineno, node.end_col_offset)
    return start, end, replacement


def _apply_text_edits(source: str, edits: Iterable[tuple[int, int, str]]) -> str:
    result = source
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _position_offset(source: str, line: int, column: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(value) for value in lines[: line - 1]) + column


def _line_offset(source: str, line: int) -> int:
    return _position_offset(source, line, 0)


def _rewrite(root: Path, path: Path, content: str, detail: str) -> UpgradeAction:
    return UpgradeAction(
        "rewrite",
        _relative(root, path),
        detail,
        content=content,
        digest=_file_digest(path),
    )


def _action_order(action: UpgradeAction) -> tuple[int, str]:
    return {"rewrite": 0, "create": 1, "move": 2, "delete": 3}.get(action.kind, 9), action.path


def _render_actions(actions: tuple[UpgradeAction, ...]) -> list[str]:
    if not actions:
        return []
    lines = ["Changes:"]
    for item in actions:
        target = f" -> {item.destination}" if item.destination else ""
        lines.append(f"  [{item.kind}] {item.path}{target}: {item.detail}")
    return lines


def _render_blockers(blockers: tuple[str, ...]) -> list[str]:
    if not blockers:
        return []
    return ["", "Manual blockers:", *(f"  - {item}" for item in blockers)]


def _verify_action_source(root: Path, action: UpgradeAction) -> None:
    if action.digest is None:
        return
    path = root / action.path
    actual = _tree_digest(path) if path.is_dir() else _file_digest(path)
    if actual != action.digest:
        raise UpgradeError(f"upgrade source changed after planning: {action.path}")


def _backup_action(root: Path, backup: Path, action: UpgradeAction) -> None:
    source = root / action.path
    if not source.exists():
        return
    destination = backup / action.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        # A rewritten child may already have created this backup directory;
        # merging preserves the complete pre-move tree without duplicating it.
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)


def _apply_action(root: Path, action: UpgradeAction) -> None:
    source = root / action.path
    if action.kind in {"create", "rewrite"}:
        if action.content is None:
            raise UpgradeError(f"upgrade action has no content: {action.path}")
        _atomic_write(source, action.content)
        return
    if action.kind == "move" and action.destination is not None:
        destination = root / action.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return
    if action.kind == "delete":
        source.unlink()
        return
    raise UpgradeError(f"unsupported upgrade action: {action.kind}")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.harnest-{uuid.uuid4().hex}")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _public_python_files(directory: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(directory.iterdir())
        if path.is_file()
        and not path.is_symlink()
        and path.suffix == ".py"
        and path.name != "__init__.py"
        and not path.name.startswith(("_", "."))
    )


def _public_directories(directory: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(directory.iterdir())
        if path.is_dir()
        and not path.is_symlink()
        and not path.name.startswith(("_", "."))
    )


def _ignored(parts: tuple[str, ...]) -> bool:
    return any(part.startswith(("_", ".")) for part in parts)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise UpgradeError(f"upgrade source cannot contain symlinks: {item}")
        if not item.is_file():
            continue
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_tree_digest(
    root: Path, path: Path, blockers: list[str]
) -> str | None:
    try:
        return _tree_digest(path)
    except (OSError, UpgradeError):
        blockers.append(f"{_relative(root, path)} contains an unreadable file or symlink")
        return None


__all__ = [
    "PROJECT_SCHEMA",
    "UpgradeAction",
    "UpgradeError",
    "UpgradePlan",
    "apply_upgrade",
    "plan_upgrade",
    "render_upgrade_plan",
]

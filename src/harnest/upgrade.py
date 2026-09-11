"""Plan and apply explicit filesystem-contract upgrades for Harnest agents."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
import textwrap
import uuid
from typing import Any, Iterable

import yaml

from .server_config import ServerConfigError, project_server_config_yaml
from .application_layout import ApplicationLayoutError, lifecycle_directory
from .lifecycle import _decorator_path_for_phase


PROJECT_SCHEMA = 5
PROJECT_LOCK = """apiVersion: harnest.dev/v1alpha1
kind: ProjectLock
projectSchema: 5
"""
_STORAGE_LIBRARY = """from harnest.store import MemoryStore


store = MemoryStore()
"""
_STORAGE_EXTENSION = '''from harnest import lifecycle
from harnest.lib.storage import store


@lifecycle.storage.sessions
def session_store():
    """Return the store for completed conversation and business state."""
    return store


@lifecycle.storage.checkpoints
def checkpointer():
    """Return the same store for private in-progress execution state."""
    return store
'''

# Schema 5 removes the package-level object facade. Keep this reviewed map in
# the migrator so every former root contract moves to its owning public domain.
_FLAT_ROOT_IMPORT_GROUPS = {
    "a2a": frozenset(
        {
            "A2AClient",
            "A2AClientError",
            "A2AResult",
            "A2AUpdate",
            "RemoteAgent",
            "RemoteAgentError",
        }
    ),
    "agent": frozenset(
        {
            "Agent",
            "AgentDefinition",
            "AgentRuntimePermissionError",
            "AgentRuntimePrincipal",
            "client_tool",
            "instruction_file",
            "tool",
        }
    ),
    "application": frozenset({"CompiledApplication"}),
    "approval": frozenset(
        {"request_human_approval", "require_human_approval"}
    ),
    "assets": frozenset({"AssetStorage", "AssetURLStorage", "Stored"}),
    "bundle": frozenset(
        {
            "BundleConventionError",
            "BundleDuplicateError",
            "BundleError",
            "BundleEvalError",
            "BundleExportError",
            "BundleImportError",
            "BundleSkillError",
            "EvalSuite",
            "bundle_agent",
            "compile_agent",
            "compile_app",
            "compile_application",
            "compile_artifact",
            "discover_evals",
        }
    ),
    "checkpoint": frozenset({"ADKStore", "HarnestStore", "LangGraphStore"}),
    "context": frozenset(
        {"AgentContext", "SessionContext", "SessionDataError", "StorageContext"}
    ),
    "continuation": frozenset(
        {
            "ContinuationConflictError",
            "ContinuationFailure",
            "ContinuationProvider",
            "ContinuationRecord",
            "ContinuationStore",
            "ContinuationValidationError",
            "ProviderPendingContinuation",
            "continuation_schema_id",
        }
    ),
    "credentials": frozenset(
        {
            "Credential",
            "CredentialContext",
            "CredentialError",
            "CredentialProvider",
            "CredentialProviderError",
            "CredentialRequest",
            "CredentialUnavailableError",
            "credentials",
        }
    ),
    "cron": frozenset({"Cron"}),
    "graph": frozenset({"START", "Edge", "Event", "Graph", "GraphContext", "Join"}),
    "http": frozenset(
        {
            "AgentInvoker",
            "AgentResponse",
            "HTTPCallRequest",
            "HTTPLifecycleContext",
            "HTTPLifecycleError",
            "HTTPResponseHead",
            "HTTPRouteError",
        }
    ),
    "lifecycle": frozenset(
        {
            "CoverageLevel",
            "DROP_EVENT",
            "Finish",
            "LifecycleContext",
            "LifecycleCoverage",
            "Next",
            "lifecycle_coverage",
        }
    ),
    "logging": frozenset({"Logger", "get_logger"}),
    "mcp": frozenset(
        {
            "MCPClient",
            "MCPClientContext",
            "MCPClientLifecycle",
            "MCPClientUnavailableError",
            "MCPContext",
            "MCPContextUnavailableError",
            "MCPHTTPClientOptions",
            "MCPLifecycleError",
            "MCPLifecyclePipeline",
            "MCPToolCallError",
            "MCPToolCallRequest",
            "MCPToolLifecycleContext",
            "MCPToolUnavailableError",
            "ManagedMCPClient",
        }
    ),
    "model": frozenset(
        {
            "LiteLLMContext",
            "LiteLLMLifecycle",
            "LiteLLMModel",
            "ModelConnector",
            "OllamaModel",
        }
    ),
    "orchestrator": frozenset(
        {"AgentSource", "Orchestrator", "define_orchestrator"}
    ),
    "output": frozenset({"AgentMetadata", "OutputPolicy", "TokenUsage"}),
    "runtime": frozenset({"ResponseRequest"}),
    "sandbox": frozenset(
        {
            "Sandbox",
            "SandboxBackend",
            "SandboxBudget",
            "SandboxContext",
            "SandboxExecutionError",
            "SandboxFile",
            "SandboxRequest",
            "SandboxResult",
            "SandboxStatus",
            "cleanup_control",
        }
    ),
    "skills": frozenset(
        {
            "FilesystemSkillSource",
            "SkillCatalogPage",
            "SkillContext",
            "SkillDescriptor",
            "SkillDocument",
            "SkillError",
            "SkillNotFoundError",
            "SkillPage",
            "SkillRegistry",
            "SkillResource",
            "SkillResourceNotSupported",
            "SkillSource",
            "SkillSourceExecutionError",
            "SkillValidationError",
        }
    ),
    "store": frozenset({"MemoryStore", "PostgresStore", "RedisStore"}),
    "structured": frozenset({"FrameworkMetadata", "StructuredOutputError"}),
    "task": frozenset({"TaskHandle", "TaskUnavailableError", "task"}),
    "telemetry": frozenset({"TelemetryExporter", "TelemetryExporterError"}),
    "tracing": frozenset(
        {"Tracer", "current_trace_ids", "get_tracer", "span", "traced"}
    ),
}
_FLAT_ROOT_IMPORTS = {
    name: domain
    for domain, names in _FLAT_ROOT_IMPORT_GROUPS.items()
    for name in names
}
_FRAMEWORK_DEPENDENCIES = {
    "adk": ("google-adk>=2.8,<3",),
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
_OUTPUT_POLICY_POSITIONAL_FIELDS = (
    "subagent_messages",
    "thinking",
    "agent_metadata",
    "persist_raw_agent_metadata",
    "tool_activity",
)
_OUTPUT_POLICY_BOOLEAN_FIELDS = frozenset(
    {"subagent_messages", "thinking", "tool_activity"}
)
_OUTPUT_POLICY_BOOLEAN_VALUES = {"include": "True", "suppress": "False"}
_OUTPUT_POLICY_METADATA_VALUES = {
    "suppress": "SUPPRESS",
    "normalized": "NORMALIZED",
    "raw": "RAW",
}


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
    _plan_server(root, blockers)
    _plan_project_lock(root, actions, blockers)
    _plan_dependencies(root, framework, actions, blockers)
    _plan_mcp(root, actions, blockers)
    _plan_extensions(root, framework, actions, blockers)
    _plan_storage(root, actions, blockers)
    from .upgrade_layout import plan_application_layout

    plan_application_layout(root, actions, blockers)
    _plan_authoring_namespaces(root, actions, blockers)
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
    _verify_contained_target(plan.root, backup)
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


def _plan_server(root: Path, blockers: list[str]) -> None:
    """Validate existing policy without generating a redundant defaults file."""

    try:
        project_server_config_yaml(root)
    except ServerConfigError as exc:
        blockers.append(str(exc))


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
                _upgraded_project_lock(path),
                f"advance the project schema from {schema} to {PROJECT_SCHEMA}",
            )
        )


def _upgraded_project_lock(path: Path) -> str:
    """Advance layout metadata without silently unpinning a resolved framework."""
    from .project_lock import read_project_lock

    value = read_project_lock(path.parent)
    value["projectSchema"] = PROJECT_SCHEMA
    return yaml.safe_dump(value, sort_keys=False)


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
    """Upgrade legacy decorators in the selected root without scanning packages."""

    try:
        directory = lifecycle_directory(root)
    except ApplicationLayoutError as error:
        blockers.append(str(error))
        return
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
        _plan_output_policy_file(root, path, actions, blockers)


def _plan_storage(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    """Add the minimal shared store only when no authored authority exists."""

    factories = _storage_factories(root, blockers)
    if factories is None or _block_duplicate_storage(factories, blockers):
        return
    present = {phase for phase, locations in factories.items() if locations}
    if present == {"session_store", "checkpointer"}:
        return
    if present:
        missing = ({"session_store", "checkpointer"} - present).pop()
        blockers.append(
            f"storage lifecycle already defines one authority; add @{missing} "
            "manually so ownership remains explicit"
        )
        return
    if _storage_targets_conflict(root, blockers):
        return
    actions.extend(
        (
            UpgradeAction(
                "create",
                "lib/storage.py",
                "add one shared development store for session and checkpoint state",
                content=_STORAGE_LIBRARY,
            ),
            UpgradeAction(
                "create",
                f"{lifecycle_directory(root).name}/storage.py",
                "declare the required session-store and checkpointer factories",
                content=_STORAGE_EXTENSION,
            ),
        )
    )


def _storage_factories(
    root: Path, blockers: list[str]
) -> dict[str, list[str]] | None:
    """Inspect lifecycle ownership without executing authored extension code."""

    from .runtime_plugins import discover_application_extensions, RuntimePluginConventionError

    try:
        directories = (lifecycle_directory(root), *(
            item.lifecycle_directory for item in discover_application_extensions(root)
        ))
    except (ApplicationLayoutError, RuntimePluginConventionError) as error:
        blockers.append(str(error))
        return None
    found = {"session_store": [], "checkpointer": []}
    for directory in directories:
        if not _collect_storage_factories(root, directory, found, blockers):
            return None
    return found


def _collect_storage_factories(root: Path, directory: Path, found: dict, blockers: list[str]) -> bool:
    """Include extension-owned storage before deciding to generate default factories."""

    if not directory.exists():
        return True
    if directory.is_symlink() or not directory.is_dir():
        return False
    for path in sorted(directory.rglob("*.py")):
        if _ignored(path.relative_to(directory).parts) or path.is_symlink():
            continue
        try:
            _, module = _python_source(path)
        except UpgradeError as exc:
            blockers.append(str(exc))
            return False
        for phase, line in _module_storage_factories(module):
            found[phase].append(f"{_relative(root, path)}:{line}")
    return True


def _module_storage_factories(module: ast.Module) -> tuple[tuple[str, int], ...]:
    """Conservatively recognize storage decorators at module scope."""

    found = []
    for item in module.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in item.decorator_list:
            value = decorator.func if isinstance(decorator, ast.Call) else decorator
            phase = _storage_decorator_phase(value)
            if phase is not None:
                found.append((phase, item.lineno))
    return tuple(found)


def _storage_decorator_phase(value: ast.AST) -> str | None:
    """Normalize both public storage decorator spellings without evaluating code."""

    if not isinstance(value, ast.Attribute):
        return None
    if value.attr in {"session_store", "checkpointer"}:
        return value.attr
    if isinstance(value.value, ast.Attribute) and value.value.attr == "storage":
        return {"sessions": "session_store", "checkpoints": "checkpointer"}.get(value.attr)
    return None


def _block_duplicate_storage(
    factories: dict[str, list[str]], blockers: list[str]
) -> bool:
    """Report every competing authority before an upgrade can mutate files."""

    duplicates = [
        f"duplicate @{phase} factories: {', '.join(locations)}"
        for phase, locations in factories.items()
        if len(locations) > 1
    ]
    blockers.extend(duplicates)
    return bool(duplicates)


def _storage_targets_conflict(root: Path, blockers: list[str]) -> bool:
    """Preserve authored paths instead of choosing alternate hidden locations."""

    conflicts = [
        relative
        for relative in ("lib/storage.py", f"{lifecycle_directory(root).name}/storage.py")
        if (root / relative).exists() or (root / relative).is_symlink()
    ]
    if conflicts:
        blockers.append(
            "cannot generate required storage because authored paths exist: "
            + ", ".join(conflicts)
        )
    invalid_directories = []
    for relative in ("lib", lifecycle_directory(root).name):
        path = root / relative
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            invalid_directories.append(relative)
            blockers.append(f"{relative}/ must be a regular directory")
    return bool(conflicts or invalid_directories)


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


def _plan_output_policy_file(
    root: Path,
    path: Path,
    actions: list[UpgradeAction],
    blockers: list[str],
) -> None:
    """Rewrite released OutputPolicy inputs without importing authored code."""

    if path.is_symlink():
        return
    try:
        pending, source = _planned_python_source(root, path, actions)
        replacement = _output_policy_source(path, source)
    except UpgradeError as exc:
        blockers.append(str(exc))
        return
    if replacement is None:
        return
    _record_output_policy_rewrite(root, path, replacement, pending, actions)


def _plan_authoring_namespaces(
    root: Path, actions: list[UpgradeAction], blockers: list[str]
) -> None:
    """Rewrite released facade imports and flat lifecycle decorators project-wide."""

    for path in _authoring_python_files(root):
        if path.is_symlink():
            blockers.append(f"{_relative(root, path)} is a symlink")
            continue
        try:
            pending, source = _planned_python_source(root, path, actions)
            replacement = _authoring_namespace_source(path, source)
        except UpgradeError as exc:
            blockers.append(str(exc))
            continue
        if replacement is not None:
            _record_composed_rewrite(
                root,
                path,
                replacement,
                pending,
                actions,
                "move root contracts into first-class feature namespaces",
            )


def _authoring_python_files(root: Path) -> tuple[Path, ...]:
    """Return authored Python while excluding managed environments and backups."""

    excluded = frozenset({".harnest", ".venv", "venv", "__pycache__"})
    return tuple(
        path
        for path in sorted(root.rglob("*.py"))
        if not any(
            part in excluded or part.startswith(".")
            for part in path.relative_to(root).parts
        )
    )


def _authoring_namespace_source(path: Path, source: str) -> str | None:
    """Build a formatting-preserving rewrite to the module namespace contract."""

    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise UpgradeError(f"{path}: cannot parse Python source") from exc
    _check_retired_model_imports(path, module)
    lifecycle_prefixes = _namespace_prefixes(module, "lifecycle")
    context_prefixes = _namespace_prefixes(module, "context")
    edits = _retired_tool_import_edits(source, module)
    edits.extend(_flat_root_import_edits(path, source, module))
    edits.extend(_namespace_import_edits(source, module, "lifecycle"))
    edits.extend(_namespace_import_edits(source, module, "context"))
    edits.extend(
        _lifecycle_namespace_edits(source, module, lifecycle_prefixes)
    )
    edits.extend(_context_provider_edits(source, module, context_prefixes))
    return _apply_text_edits(source, edits) if edits else None


def _check_retired_model_imports(path: Path, module: ast.Module) -> None:
    """Require endpoint review instead of rewriting a native provider blindly."""

    qualified = {f"{prefix}.OllamaModel" for prefix in _namespace_prefixes(module, "model")}
    for node in ast.walk(module):
        if _retired_model_import(node) or _dotted_name(node) in qualified:
            raise UpgradeError(
                f"{path}:{node.lineno}: OllamaModel was removed; migrate to "
                "LiteLLMModel.from_openai_environment() and explicitly configure "
                "OPENAI_MODEL, OPENAI_BASE_URL (compatible API, usually /v1), "
                "and optional OPENAI_API_KEY before upgrading"
            )


def _retired_model_import(node: ast.AST) -> bool:
    """Recognize direct and aliased imports without importing user code."""

    return (
        isinstance(node, ast.ImportFrom)
        and node.module in {"harnest", "harnest.model"}
        and any(name.name == "OllamaModel" for name in node.names)
    )


def _retired_tool_import_edits(
    source: str, module: ast.Module
) -> list[tuple[int, int, str]]:
    """Move the retired tool module surface into the agent feature."""

    edits: list[tuple[int, int, str]] = []
    rewrite_qualified_access = False
    for item in module.body:
        if isinstance(item, ast.ImportFrom) and item.module == "harnest.tool":
            names = ", ".join(_render_import_alias(name) for name in item.names)
            edits.append(_node_edit(source, item, f"from harnest.agent import {names}"))
            continue
        if not isinstance(item, ast.Import):
            continue
        replacement, unaliased = _retired_tool_import_statement(item)
        if replacement is not None:
            edits.append(_node_edit(source, item, replacement))
            rewrite_qualified_access = rewrite_qualified_access or unaliased
    if rewrite_qualified_access:
        edits.extend(_retired_tool_attribute_edits(source, module))
    return edits


def _retired_tool_import_statement(item: ast.Import) -> tuple[str | None, bool]:
    """Rewrite imported tool modules while retaining local aliases."""

    changed = False
    unaliased = False
    rendered: list[str] = []
    for name in item.names:
        imported = "harnest.agent" if name.name == "harnest.tool" else name.name
        changed = changed or imported != name.name
        unaliased = unaliased or (imported != name.name and name.asname is None)
        rendered.append(imported + (f" as {name.asname}" if name.asname else ""))
    return (f"import {', '.join(rendered)}", unaliased) if changed else (None, False)


def _retired_tool_attribute_edits(
    source: str, module: ast.Module
) -> list[tuple[int, int, str]]:
    """Rewrite outermost qualified access after an unaliased module import."""

    nested = {
        id(node.value)
        for node in ast.walk(module)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
    }
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Attribute) or id(node) in nested:
            continue
        dotted = _dotted_name(node)
        if dotted is None or not (
            dotted == "harnest.tool" or dotted.startswith("harnest.tool.")
        ):
            continue
        replacement = "harnest.agent" + dotted[len("harnest.tool") :]
        edits.append(_node_edit(source, node, replacement))
    return edits


def _flat_root_import_edits(
    path: Path, source: str, module: ast.Module
) -> list[tuple[int, int, str]]:
    """Move former root objects into their owning public domain modules."""

    edits: list[tuple[int, int, str]] = []
    for item in module.body:
        if not isinstance(item, ast.ImportFrom) or item.module != "harnest":
            continue
        if any(name.name == "*" for name in item.names):
            raise UpgradeError(
                f"{path}: replace 'from harnest import *' with explicit domain imports"
            )
        replacement = _flat_root_import_replacement(item)
        if replacement is not None:
            edits.append(_node_edit(source, item, replacement))
    return edits


def _flat_root_import_replacement(item: ast.ImportFrom) -> str | None:
    """Render one root import as stable, grouped domain imports."""

    grouped: dict[str, list[ast.alias]] = {}
    root_names: list[ast.alias] = []
    for name in item.names:
        domain = _FLAT_ROOT_IMPORTS.get(name.name)
        if domain is None:
            root_names.append(name)
            continue
        grouped.setdefault(domain, []).append(name)
    if not grouped:
        return None
    lines = _render_grouped_domain_imports(root_names, grouped)
    return "\n".join(lines)


def _render_grouped_domain_imports(
    root_names: list[ast.alias], grouped: dict[str, list[ast.alias]]
) -> list[str]:
    """Keep module imports at the root and place objects under their domains."""

    lines: list[str] = []
    if root_names:
        rendered = ", ".join(_render_import_alias(name) for name in root_names)
        lines.append(f"from harnest import {rendered}")
    for domain, names in grouped.items():
        rendered = ", ".join(_render_import_alias(name) for name in names)
        lines.append(f"from harnest.{domain} import {rendered}")
    return lines


def _namespace_prefixes(module: ast.Module, domain: str) -> frozenset[str]:
    """Resolve local spellings that refer to one public domain namespace."""

    prefixes: set[str] = set()
    for item in module.body:
        if isinstance(item, ast.ImportFrom):
            prefixes.update(_from_import_namespace_prefixes(item, domain))
        elif isinstance(item, ast.Import):
            prefixes.update(_import_namespace_prefixes(item, domain))
    return frozenset(prefixes)


def _from_import_namespace_prefixes(
    item: ast.ImportFrom, domain: str
) -> set[str]:
    """Resolve root and removed facade imports for one domain namespace."""

    if item.module not in {"harnest", f"harnest.{domain}"}:
        return set()
    prefixes = {
        name.asname or domain for name in item.names if name.name == domain
    }
    if item.module == f"harnest.{domain}" and any(
        name.name == "*" for name in item.names
    ):
        prefixes.add(domain)
    return prefixes


def _import_namespace_prefixes(item: ast.Import, domain: str) -> set[str]:
    """Resolve qualified and aliased ``import`` spellings for one namespace."""

    prefixes: set[str] = set()
    for name in item.names:
        if name.name == "harnest":
            prefixes.add(f"{name.asname or 'harnest'}.{domain}")
        elif name.name == f"harnest.{domain}":
            prefixes.add(name.asname or name.name)
    return prefixes


def _namespace_import_edits(
    source: str, module: ast.Module, domain: str
) -> list[tuple[int, int, str]]:
    """Move a same-named facade import from its submodule to the root package."""

    candidates = (
        item for item in module.body if isinstance(item, ast.ImportFrom)
    )
    return [
        edit
        for item in candidates
        if (edit := _namespace_import_edit(source, item, domain)) is not None
    ]


def _namespace_import_edit(
    source: str, item: ast.ImportFrom, domain: str
) -> tuple[int, int, str] | None:
    """Rewrite one removed facade import while retaining sibling contracts."""

    if item.module != f"harnest.{domain}":
        return None
    replacement = _namespace_import_replacement(item, domain)
    return _node_edit(source, item, replacement) if replacement else None


def _namespace_import_replacement(
    item: ast.ImportFrom, domain: str
) -> str | None:
    """Render the root import required by one legacy namespace import."""

    facade = next((name for name in item.names if name.name == domain), None)
    if facade is None:
        if any(name.name == "*" for name in item.names):
            return f"from harnest import {domain}\nfrom harnest.{domain} import *"
        return None
    remaining = [name for name in item.names if name is not facade]
    root_import = f"from harnest import {_render_import_alias(facade)}"
    if remaining:
        rendered = ", ".join(_render_import_alias(name) for name in remaining)
        root_import += f"\nfrom harnest.{domain} import {rendered}"
    return root_import


def _lifecycle_namespace_edits(
    source: str, module: ast.Module, prefixes: frozenset[str]
) -> list[tuple[int, int, str]]:
    """Replace every removed flat decorator with its grouped lifecycle path."""

    bare_decorators = {
        id(decorator)
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Attribute)
    }
    attributes = (
        node for node in ast.walk(module) if isinstance(node, ast.Attribute)
    )
    edits = [
        edit
        for node in attributes
        if (
            edit := _lifecycle_attribute_edit(
                source, node, prefixes, id(node) in bare_decorators
            )
        ) is not None
    ]
    edits.extend(_asset_default_argument_edits(source, module, prefixes))
    return edits


def _asset_default_argument_edits(
    source: str, module: ast.Module, prefixes: frozenset[str]
) -> list[tuple[int, int, str]]:
    """Preserve the flat asset decorator's implicit default store name."""

    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or node.args:
            continue
        prefix, member = _matched_namespace_member(
            _dotted_name(node.func), prefixes
        )
        if member != "asset_store" or any(
            item.arg == "name" for item in node.keywords
        ):
            continue
        # Insert independently of the function-name rewrite so authored keyword
        # formatting and comments remain intact.
        function_end = _position_offset(
            source, node.func.end_lineno, node.func.end_col_offset
        )
        call_end = _position_offset(source, node.end_lineno, node.end_col_offset)
        opening = source.find("(", function_end, call_end)
        if prefix is not None and opening >= 0:
            separator = ", " if node.keywords else ""
            edits.append((opening + 1, opening + 1, f'"default"{separator}'))
    return edits


def _lifecycle_attribute_edit(
    source: str,
    node: ast.Attribute,
    prefixes: frozenset[str],
    bare_decorator: bool,
) -> tuple[int, int, str] | None:
    """Map one removed lifecycle member without evaluating authored code."""

    prefix, old_name = _matched_namespace_member(_dotted_name(node), prefixes)
    if prefix is None or old_name is None:
        return None
    replacement = _decorator_path_for_phase(old_name)
    if replacement == old_name:
        return None
    value = f"{prefix}.{replacement}"
    if old_name == "asset_store" and bare_decorator:
        value += '("default")'
    return _node_edit(source, node, value)


def _context_provider_edits(
    source: str, module: ast.Module, prefixes: frozenset[str]
) -> list[tuple[int, int, str]]:
    """Make context provider registration explicit without changing other calls."""

    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            name = _dotted_name(decorator.func) if isinstance(decorator, ast.Call) else None
            if name in prefixes:
                edits.append(_node_edit(source, decorator.func, f"{name}.provider"))
    return edits


def _matched_namespace_member(
    dotted: str | None, prefixes: frozenset[str]
) -> tuple[str | None, str | None]:
    """Split a qualified member only when its prefix is a known namespace import."""

    if dotted is None:
        return None, None
    for prefix in prefixes:
        marker = f"{prefix}."
        if dotted.startswith(marker) and "." not in dotted[len(marker):]:
            return prefix, dotted[len(marker):]
    return None, None


def _dotted_name(value: ast.AST) -> str | None:
    """Return a static dotted name without evaluating authored expressions."""

    parts: list[str] = []
    current = value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _planned_python_source(
    root: Path, path: Path, actions: list[UpgradeAction]
) -> tuple[tuple[int, UpgradeAction] | None, str]:
    """Read the latest planned content when another migration owns the same file."""

    relative = _relative(root, path)
    pending = next(
        (
            (index, action)
            for index, action in enumerate(actions)
            if action.kind == "rewrite" and action.path == relative
        ),
        None,
    )
    if pending is not None and pending[1].content is not None:
        return pending, pending[1].content
    try:
        return pending, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise UpgradeError(f"{path}: cannot read Python source") from exc


def _record_output_policy_rewrite(
    root: Path,
    path: Path,
    content: str,
    pending: tuple[int, UpgradeAction] | None,
    actions: list[UpgradeAction],
) -> None:
    """Compose source migrations so a path is rewritten only once before moves."""

    _record_composed_rewrite(
        root,
        path,
        content,
        pending,
        actions,
        "migrate OutputPolicy to keyword-only booleans and AgentMetadataMode",
    )


def _record_composed_rewrite(
    root: Path,
    path: Path,
    content: str,
    pending: tuple[int, UpgradeAction] | None,
    actions: list[UpgradeAction],
    detail: str,
) -> None:
    """Merge independent source migrations into one digest-bound rewrite."""

    if pending is None:
        actions.append(_rewrite(root, path, content, detail))
        return
    index, action = pending
    actions[index] = replace(
        action,
        content=content,
        detail=f"{action.detail}; {detail}",
    )


def _output_policy_source(path: Path, source: str) -> str | None:
    """Build a formatting-preserving migration for imported policy call sites."""

    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise UpgradeError(f"{path}: cannot parse Python source") from exc
    imports = _output_policy_imports(module)
    policy_names = _imported_names(imports, "OutputPolicy")
    legacy_type_names = _imported_names(imports, "SubagentMessageMode")
    metadata_names = _imported_names(imports, "AgentMetadataMode")
    if not policy_names and not legacy_type_names:
        return None
    metadata_name = next(iter(metadata_names), "AgentMetadataMode")
    edits, needs_metadata = _output_policy_call_edits(
        path, source, module, policy_names, metadata_name
    )
    edits.extend(
        _legacy_output_policy_type_edits(
            path, source, module, legacy_type_names
        )
    )
    edits.extend(
        _output_policy_import_edits(
            source,
            imports,
            add_metadata=needs_metadata and not metadata_names,
        )
    )
    return _apply_text_edits(source, edits) if edits else None


def _output_policy_imports(module: ast.Module) -> tuple[ast.ImportFrom, ...]:
    """Return public imports that can bind the released policy names directly."""

    return tuple(
        item
        for item in module.body
        if isinstance(item, ast.ImportFrom)
        and item.module in {"harnest", "harnest.output"}
    )


def _imported_names(
    imports: tuple[ast.ImportFrom, ...], imported_name: str
) -> frozenset[str]:
    """Resolve local aliases for one directly imported public name."""

    return frozenset(
        name.asname or name.name
        for item in imports
        for name in item.names
        if name.name == imported_name
    )


def _output_policy_call_edits(
    path: Path,
    source: str,
    module: ast.Module,
    policy_names: frozenset[str],
    metadata_name: str,
) -> tuple[list[tuple[int, int, str]], bool]:
    """Convert known policy calls while rejecting ambiguous argument expansion."""

    edits: list[tuple[int, int, str]] = []
    needs_metadata = False
    calls = (
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in policy_names
    )
    for call in calls:
        call_edits, call_needs_metadata = _output_policy_call_edit(
            path, source, call, metadata_name
        )
        edits.extend(call_edits)
        needs_metadata = needs_metadata or call_needs_metadata
    return edits, needs_metadata


def _output_policy_call_edit(
    path: Path,
    source: str,
    call: ast.Call,
    metadata_name: str,
) -> tuple[list[tuple[int, int, str]], bool]:
    """Map one call from the released positional and string-valued contract."""

    positional_fields = _validate_output_policy_call(path, call)
    positional_edits, positional_metadata = _output_policy_positional_edits(
        path, source, call, positional_fields, metadata_name
    )
    keyword_edits, keyword_metadata = _output_policy_keyword_edits(
        path, source, call, metadata_name
    )
    return positional_edits + keyword_edits, positional_metadata or keyword_metadata


def _validate_output_policy_call(path: Path, call: ast.Call) -> tuple[str, ...]:
    """Reject calls whose runtime expansion cannot be mapped deterministically."""

    if len(call.args) > len(_OUTPUT_POLICY_POSITIONAL_FIELDS):
        raise UpgradeError(
            f"{path}:{call.lineno}: OutputPolicy has too many positional arguments"
        )
    if any(isinstance(value, ast.Starred) for value in call.args):
        raise UpgradeError(
            f"{path}:{call.lineno}: OutputPolicy *args require manual migration"
        )
    if any(keyword.arg is None for keyword in call.keywords):
        raise UpgradeError(
            f"{path}:{call.lineno}: OutputPolicy **kwargs require manual migration"
        )
    positional_fields = _OUTPUT_POLICY_POSITIONAL_FIELDS[: len(call.args)]
    keyword_fields = {keyword.arg for keyword in call.keywords}
    overlap = next((field for field in positional_fields if field in keyword_fields), None)
    if overlap is not None:
        raise UpgradeError(
            f"{path}:{call.lineno}: OutputPolicy field {overlap!r} is duplicated"
        )
    return positional_fields


def _output_policy_positional_edits(
    path: Path,
    source: str,
    call: ast.Call,
    fields: tuple[str, ...],
    metadata_name: str,
) -> tuple[list[tuple[int, int, str]], bool]:
    """Name positional values according to the exact released field order."""

    edits: list[tuple[int, int, str]] = []
    needs_metadata = False
    for field_name, value in zip(fields, call.args):
        replacement, converted_metadata = _output_policy_value(
            path, source, field_name, value, metadata_name
        )
        edits.append(_node_edit(source, value, f"{field_name}={replacement}"))
        needs_metadata = needs_metadata or converted_metadata
    return edits, needs_metadata


def _output_policy_keyword_edits(
    path: Path,
    source: str,
    call: ast.Call,
    metadata_name: str,
) -> tuple[list[tuple[int, int, str]], bool]:
    """Replace released string literals in already named policy arguments."""

    edits: list[tuple[int, int, str]] = []
    needs_metadata = False
    for keyword in call.keywords:
        assert keyword.arg is not None
        replacement, converted_metadata = _output_policy_value(
            path, source, keyword.arg, keyword.value, metadata_name
        )
        if replacement != ast.get_source_segment(source, keyword.value):
            edits.append(_node_edit(source, keyword.value, replacement))
        needs_metadata = needs_metadata or converted_metadata
    return edits, needs_metadata


def _output_policy_value(
    path: Path,
    source: str,
    field_name: str,
    value: ast.AST,
    metadata_name: str,
) -> tuple[str, bool]:
    """Translate released string literals and preserve already typed expressions."""

    segment = ast.get_source_segment(source, value)
    if segment is None:
        raise UpgradeError(f"{path}:{value.lineno}: cannot preserve OutputPolicy value")
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return segment, False
    if field_name in _OUTPUT_POLICY_BOOLEAN_FIELDS:
        replacement = _OUTPUT_POLICY_BOOLEAN_VALUES.get(value.value)
        if replacement is not None:
            return replacement, False
    elif field_name == "agent_metadata":
        member = _OUTPUT_POLICY_METADATA_VALUES.get(value.value)
        if member is not None:
            return f"{metadata_name}.{member}", True
    else:
        return segment, False
    raise UpgradeError(
        f"{path}:{value.lineno}: unsupported legacy OutputPolicy value "
        f"{value.value!r} for {field_name}"
    )


def _legacy_output_policy_type_edits(
    path: Path,
    source: str,
    module: ast.Module,
    legacy_names: frozenset[str],
) -> list[tuple[int, int, str]]:
    """Replace the removed string alias only where it is used as a type."""

    if not legacy_names:
        return []
    annotation_nodes = _annotation_node_ids(module)
    references = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Name) and node.id in legacy_names
    ]
    unsupported = next(
        (node for node in references if id(node) not in annotation_nodes), None
    )
    if unsupported is not None:
        raise UpgradeError(
            f"{path}:{unsupported.lineno}: SubagentMessageMode value usage "
            "requires manual migration"
        )
    return [_node_edit(source, node, "bool") for node in references]


def _annotation_node_ids(module: ast.Module) -> frozenset[int]:
    """Identify syntax owned by annotations without treating runtime names as types."""

    roots: list[ast.AST] = []
    for node in ast.walk(module):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return frozenset(id(node) for root in roots for node in ast.walk(root))


def _output_policy_import_edits(
    source: str,
    imports: tuple[ast.ImportFrom, ...],
    *,
    add_metadata: bool,
) -> list[tuple[int, int, str]]:
    """Remove the stale alias and add the enum beside the canonical policy import."""

    output_imports = [item for item in imports if item.module == "harnest.output"]
    metadata_target = output_imports[0] if add_metadata and output_imports else None
    edits = [
        edit
        for item in output_imports
        if (edit := _output_policy_import_edit(source, item, item is metadata_target))
    ]
    if add_metadata and metadata_target is None:
        edits.append(_agent_metadata_import_insertion(source, imports))
    return edits


def _output_policy_import_edit(
    source: str, item: ast.ImportFrom, add_metadata: bool
) -> tuple[int, int, str] | None:
    """Rewrite one canonical import only when its public members change."""

    names = [name for name in item.names if name.name != "SubagentMessageMode"]
    if add_metadata:
        names.append(ast.alias(name="AgentMetadataMode"))
    if names == item.names:
        return None
    rendered = ", ".join(_render_import_alias(name) for name in names)
    replacement = f"from harnest.output import {rendered}" if names else ""
    return _node_edit(source, item, replacement)


def _agent_metadata_import_insertion(
    source: str, imports: tuple[ast.ImportFrom, ...]
) -> tuple[int, int, str]:
    """Place the enum import after a root-level OutputPolicy import."""

    target = next(
        item
        for item in imports
        if any(name.name == "OutputPolicy" for name in item.names)
    )
    offset = _line_offset(source, target.end_lineno + 1)
    prefix = "" if offset == 0 or source[offset - 1 : offset] == "\n" else "\n"
    return offset, offset, f"{prefix}from harnest.output import AgentMetadataMode\n"


def _render_import_alias(value: ast.alias) -> str:
    """Render an import member while retaining its local binding."""

    return f"{value.name} as {value.asname}" if value.asname else value.name


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
        line = (
            function.decorator_list[0].lineno
            if function.decorator_list
            else function.lineno
        )
        decorator = _decorator_path_for_phase(phase)
        edits.append(
            (
                _line_offset(source, line),
                _line_offset(source, line),
                f"@lifecycle.{decorator}\n",
            )
        )
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
    replacement = "from harnest import lifecycle"
    if imported:
        names = ", ".join(sorted(set(imported)))
        replacement += f"\nfrom harnest.lifecycle import {names}"
    return [_node_edit(source, imports[0], replacement)]


def _native_extension_source(path: Path, framework: str) -> str | None:
    source, module = _python_source(path)
    phase = "adk_plugin" if framework == "adk" else "langgraph_middleware"
    if f"lifecycle.{phase}" in source:
        return None
    if not any(_assignment_name(item) == "extension" for item in module.body):
        raise UpgradeError(f"{path}: expected a legacy native export named 'extension'")
    suffix = (
        "\n\nfrom harnest import lifecycle as _harnest_lifecycle\n\n"
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
    """Move child files first, then vacate the old lifecycle root for packages."""

    rank = {"rewrite": 0, "create": 1, "move": 2, "delete": 3,
            "relocate_lifecycle": 4, "relocate_extension": 5}
    return rank.get(action.kind, 9), action.path


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
    """Fail stale sources and newly occupied destinations before any mutation."""

    target = root / action.path
    _verify_contained_target(root, target)
    _verify_action_destination(root, action)
    if action.kind == "create" and (target.exists() or target.is_symlink()):
        raise UpgradeError(f"upgrade source changed after planning: {action.path}")
    if action.digest is None:
        return
    actual = _tree_digest(target) if target.is_dir() else _file_digest(target)
    if actual != action.digest:
        raise UpgradeError(f"upgrade source changed after planning: {action.path}")


def _verify_action_destination(root: Path, action: UpgradeAction) -> None:
    """Fail occupied move destinations before creating backups or rewriting sources."""

    if action.destination is not None:
        destination = root / action.destination
        _verify_contained_target(root, destination)
        # Package destinations can currently be children of the legacy lifecycle
        # tree, which is moved away first. All other destinations must stay absent.
        vacated = action.kind == "relocate_extension" and lifecycle_directory(root) == root / "extensions"
        if not vacated and (destination.exists() or destination.is_symlink()):
            raise UpgradeError(f"upgrade destination changed after planning: {action.destination}")


def _verify_contained_target(root: Path, target: Path) -> None:
    """Reject parent-link swaps and path escapes before any upgrade writes."""

    if not target.is_relative_to(root) or ".." in target.relative_to(root).parts:
        raise UpgradeError(f"upgrade target escapes the project: {target}")
    current = target
    while current != root.parent:
        if current.is_symlink():
            raise UpgradeError(f"upgrade target contains a symlink: {current}")
        current = current.parent


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
    """Apply one verified change in lifecycle-before-package migration order."""
    source = root / action.path
    if action.kind in {"create", "rewrite"}:
        if action.content is None:
            raise UpgradeError(f"upgrade action has no content: {action.path}")
        _atomic_write(source, action.content)
        return
    if action.kind in {"move", "relocate_lifecycle", "relocate_extension"} and action.destination is not None:
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

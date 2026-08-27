"""Filesystem-first agent composition."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Sequence, TypeVar

from .agent import AgentDefinition, _AdvancedAgentDefinition
from .application import CompiledApplication
from .compatibility import (
    FrameworkCompatibilityError,
    validate_framework_compatibility,
)
from .backends import (
    AdvancedBackendValidationError,
    BackendDependencyError,
    UnknownBackendError,
    get_backend,
)
from .graph import Graph
from .extension_loader import ExtensionDiscoveryError, discover_extensions
from ._library import (
    LibraryConventionError,
    LibraryImportError as AuthoredLibraryImportError,
    authored_library,
)
from .mcp import MCPClient
from .plugin import PluginConventionError, PluginResources, discover_plugins
from .sandbox import Sandbox

_T = TypeVar("_T")
_IGNORED_FILE_NAMES = {"__init__.py"}
_MAX_METADATA_FILE_BYTES = 16 * 1024 * 1024
_ARTIFACT_IGNORED_DIRECTORIES = {
    ".adk",
    ".git",
    ".harnest",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "venv",
}
_SKILL_TOOL_NAMES = {
    "list_skills",
    "load_skill",
    "load_skill_resource",
    "run_skill_script",
}


class BundleError(RuntimeError):
    """Base error for invalid filesystem agent bundles."""


class BundleConventionError(BundleError):
    """A bundle path does not follow the filesystem convention."""


class BundleImportError(BundleError):
    """A discovered resource module could not be imported."""


class BundleExportError(BundleError):
    """A discovered module does not export its file-stem resource."""


class BundleDuplicateError(BundleError):
    """Two explicit or discovered resources have the same identity."""


class BundleSkillError(BundleError):
    """A discovered progressive skill is invalid or cannot be loaded."""


class BundleEvalError(BundleError):
    """A discovered ADK evaluation file is invalid."""


@dataclass(frozen=True, slots=True)
class EvalSuite:
    """Validated evaluation files associated with one agent folder."""

    eval_sets: tuple[Path, ...]
    config: Path | None = None


@dataclass(frozen=True, slots=True)
class _DiscoveredResources:
    """Resources discovered once for a single folder-owned agent scope."""

    tools: tuple[Callable[..., Any], ...]
    subagents: tuple[AgentDefinition, ...]
    mcp: tuple[MCPClient, ...]
    sandbox: Sandbox | None
    skill_directories: tuple[Path, ...]


def compile_agent(
    source: str | Path,
    *,
    entrypoint: str = "agent:root_agent",
    framework: str = "adk",
    mode: str = "managed",
) -> Any:
    """Compile a filesystem definition into its framework-native target.

    ``source`` may be the agent directory or its ``agent.py`` file. Authored
    modules import the compiler-provided namespace explicitly, for example
    ``from harnest.agent import Agent``. The Harnest compiler/runtime provides
    that namespace; it does not belong in the agent's requirements file.
    """

    return compile_application(
        source, entrypoint=entrypoint, framework=framework, mode=mode
    ).target


def compile_app(
    source: str | Path,
    *,
    entrypoint: str = "agent:root_agent",
    framework: str = "adk",
    mode: str = "managed",
) -> Any:
    """Compile a filesystem definition and return its provider application."""

    compiled = compile_application(
        source, entrypoint=entrypoint, framework=framework, mode=mode
    )
    return compiled.native_app if compiled.native_app is not None else compiled.target


def compile_application(
    source: str | Path,
    *,
    entrypoint: str,
    framework: str = "adk",
    mode: str = "managed",
) -> CompiledApplication:
    """Compile one managed definition or advanced framework application."""

    try:
        backend = get_backend(framework)
    except UnknownBackendError as exc:
        raise BundleConventionError(str(exc)) from exc
    if mode not in {"managed", "advanced"}:
        raise BundleConventionError("framework mode must be managed or advanced")
    try:
        compatibility = validate_framework_compatibility(framework)
    except FrameworkCompatibilityError as exc:
        raise BundleImportError(str(exc)) from exc
    anchor = _resolve_compile_anchor(source, entrypoint)
    export_name = entrypoint.partition(":")[2]
    with _bundle_library(anchor.parent):
        module, value = _load_export(anchor, export_name)
        if mode == "advanced":
            return _compile_advanced_application(
                anchor, module, value, export_name, framework, backend, compatibility
            )
        return _compile_managed_application(
            anchor,
            module,
            value,
            export_name,
            framework,
            mode,
            backend,
            compatibility,
        )


@contextmanager
def _bundle_library(bundle_root: Path) -> Iterator[None]:
    """Translate namespace validation failures into the compiler error model."""

    try:
        with authored_library(bundle_root):
            yield
    except LibraryConventionError as exc:
        raise BundleConventionError(str(exc)) from exc
    except AuthoredLibraryImportError as exc:
        raise BundleImportError(str(exc)) from exc


def _compile_advanced_application(
    anchor: Path,
    module: ModuleType,
    value: Any,
    export_name: str,
    framework: str,
    backend: Any,
    compatibility: Any,
) -> CompiledApplication:
    if not isinstance(value, _AdvancedAgentDefinition):
        raise BundleExportError(
            f"advanced agent module {anchor} must export the result of "
            f"Agent.advanced(...) as {export_name!r}; got {type(value).__name__}"
        )
    _reject_extra_exports(
        module,
        anchor,
        export_name,
        kind="advanced application",
        predicate=lambda item: isinstance(item, _AdvancedAgentDefinition),
    )
    _reject_advanced_filesystem_resources(anchor.parent, framework=framework)
    try:
        advanced = backend.validate_advanced(
            value, fallback_name=anchor.parent.name
        )
    except BackendDependencyError as exc:
        raise BundleImportError(str(exc)) from exc
    except AdvancedBackendValidationError as exc:
        raise BundleExportError(str(exc)) from exc
    return CompiledApplication(
        name=advanced.name,
        framework=framework,
        mode="advanced",
        target=advanced.target,
        native_app=advanced.native_app,
        kind="advanced",
        bridge=value,
        harnest_version=compatibility.harnest_version,
        framework_distribution=compatibility.distribution,
        framework_version=compatibility.distribution_version,
    )


def _compile_managed_application(
    anchor: Path,
    module: ModuleType,
    value: Any,
    export_name: str,
    framework: str,
    mode: str,
    backend: Any,
    compatibility: Any,
) -> CompiledApplication:
    if not isinstance(value, (AgentDefinition, Graph)):
        raise BundleExportError(
            f"managed agent module {anchor} must export Agent or Graph "
            f"{export_name!r}; got {type(value).__name__}"
        )
    _reject_extra_exports(
        module,
        anchor,
        export_name,
        kind="managed application",
        predicate=lambda item: isinstance(item, (AgentDefinition, Graph)),
    )
    discovered_extensions = _load_extensions(anchor.parent, framework)
    portable_extensions = tuple(
        item.portable
        for item in discovered_extensions
        if item.portable is not None
    )
    native_extensions = tuple(
        item.native for item in discovered_extensions if item.native is not None
    )
    if isinstance(value, AgentDefinition):
        definition = _compose_definition(anchor, value, framework=framework)
        target = backend.lower_managed(
            definition, native_extensions=native_extensions
        )
    else:
        graph = _resolve_graph_resources(anchor, value, framework=framework)
        target = backend.lower_managed(
            graph, native_extensions=native_extensions
        )

    # Extensions wrap the completed target because capability composition must
    # finish first and remain independent of runtime lifecycle behavior.
    try:
        native_app = backend.wrap_managed(
            target, native_extensions=native_extensions
        )
    except BackendDependencyError as exc:
        raise BundleImportError(str(exc)) from exc
    except AdvancedBackendValidationError as exc:
        raise BundleConventionError(str(exc)) from exc
    return CompiledApplication(
        name=getattr(target, "name", value.name),
        framework=framework,
        mode=mode,
        target=target,
        native_app=native_app,
        kind="agent" if isinstance(value, AgentDefinition) else "graph",
        extensions=portable_extensions,
        harnest_version=compatibility.harnest_version,
        framework_distribution=compatibility.distribution,
        framework_version=compatibility.distribution_version,
    )


def _load_extensions(bundle_root: Path, framework: str) -> tuple[Any, ...]:
    try:
        return discover_extensions(bundle_root / "extensions", framework=framework)
    except ExtensionDiscoveryError as exc:
        raise BundleConventionError(str(exc)) from exc


def compile_artifact(
    source: str | Path,
    output: str | Path,
    *,
    entrypoint: str = "agent:root_agent",
    framework: str = "adk",
    mode: str = "managed",
) -> dict[str, Any]:
    """Validate an agent and atomically materialize its importable artifact."""

    anchor = _resolve_compile_anchor(source, entrypoint)
    source_directory = anchor.parent
    output_directory = Path(output).resolve()
    if output_directory == source_directory:
        raise BundleConventionError(
            "compiled output must not replace the authored agent directory"
        )
    if output_directory.is_symlink():
        raise BundleConventionError(
            f"compiled output cannot be a symlink: {output_directory}"
        )
    try:
        output_relative = output_directory.relative_to(source_directory)
    except ValueError:
        output_relative = None
    if output_relative is not None and (
        len(output_relative.parts) < 2 or output_relative.parts[0] != ".harnest"
    ):
        raise BundleConventionError(
            "output inside an authored agent must be below .harnest/ so it is "
            "excluded from the copied source"
        )

    built = compile_application(
        anchor, entrypoint=entrypoint, framework=framework, mode=mode
    )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.tmp-",
            dir=output_directory.parent,
        )
    )
    try:
        _copy_agent_source(source_directory, staging / "source")
        _write_artifact_loader(staging, entrypoint, framework, mode)
        file_records = _artifact_file_records(staging)
        manifest = {
            "apiVersion": "harnest.dev/v1alpha1",
            "kind": "CompiledAgent",
            "name": built.name,
            "entrypoint": "agent:root_agent",
            "sourceEntrypoint": entrypoint,
            "sourceDirectory": "source",
            "harnestVersion": built.harnest_version,
            "framework": {
                "name": framework,
                "mode": mode,
                "distribution": built.framework_distribution,
                "version": built.framework_version,
            },
            "digest": _artifact_digest(file_records),
            "files": file_records,
        }
        (staging / "harnest-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _replace_artifact(staging, output_directory)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _copy_agent_source(source: Path, destination: Path) -> None:
    destination.mkdir()
    paths = sorted(
        source.rglob("*"),
        key=lambda item: item.relative_to(source).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(source)
        if path.is_symlink():
            raise BundleConventionError(
                f"agent source copied into an artifact cannot contain symlinks: {path}"
            )
        if any(part in _ARTIFACT_IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if relative.name == ".env" or relative.name.startswith(".env."):
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleConventionError(f"unsupported agent source resource: {path}")
        if path.suffix in {".pyc", ".pyo"}:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _write_artifact_loader(
    directory: Path, source_entrypoint: str, framework: str, mode: str
) -> None:
    (directory / "agent.py").write_text(
        '"""Generated Harnest application entrypoint; do not edit."""\n\n'
        "from pathlib import Path\n\n"
        "from harnest import compile_application\n\n"
        "application = compile_application(\n"
        '    Path(__file__).parent / "source",\n'
        f"    entrypoint={source_entrypoint!r},\n"
        f"    framework={framework!r},\n"
        f"    mode={mode!r},\n"
        ")\n"
        "app = application.native_app or application.target\n"
        "root_agent = application.target\n",
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text(
        '"""Generated Harnest agent package."""\n\n'
        "from .agent import app, application, root_agent\n\n"
        '__all__ = ["application", "app", "root_agent"]\n',
        encoding="utf-8",
    )
    (directory / "__main__.py").write_text(
        '"""Run this compiled agent with ``python <artifact>``."""\n\n'
        "from pathlib import Path\n\n"
        "import sys\n\n"
        "from harnest.runtime import main\n\n"
        "raise SystemExit(\n"
        "    main([\"--artifact\", str(Path(__file__).parent), *sys.argv[1:]])\n"
        ")\n",
        encoding="utf-8",
    )
    launcher = directory / "harnest-agent"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "from harnest.runtime import main\n"
        "raise SystemExit(main([\"--artifact\", str(Path(__file__).parent), *sys.argv[1:]]))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)


def _artifact_file_records(directory: Path) -> list[dict[str, Any]]:
    records = []
    paths = sorted(
        directory.rglob("*"),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    for path in paths:
        if not path.is_file():
            continue
        contents = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "size": len(contents),
            }
        )
    return records


def _artifact_digest(records: Sequence[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _replace_artifact(staging: Path, output: Path) -> None:
    if not output.exists():
        os.replace(staging, output)
        return
    if not output.is_dir():
        raise BundleConventionError(
            f"compiled output exists and is not a directory: {output}"
        )

    backup = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.old-", dir=output.parent)
    )
    backup.rmdir()
    os.replace(output, backup)
    try:
        os.replace(staging, output)
    except Exception:
        os.replace(backup, output)
        raise
    shutil.rmtree(backup)


def bundle_agent(anchor: str | Path, root: AgentDefinition) -> Any:
    """Discover sibling resources and build an ADK root agent.

    Pass ``__file__`` from an agent folder's ``agent.py``. Public Python files
    below ``tools/``, ``subagents/``, and ``mcp/`` are imported in
    sorted path order and must export an object named after their file stem.
    Directory-first ``plugins/`` contribute MCP clients and skills without an
    aggregation import.
    """

    if not isinstance(root, AgentDefinition):
        raise TypeError("bundle_agent root must be an AgentDefinition")
    validated_anchor = _validate_anchor(anchor)
    with _bundle_library(validated_anchor.parent):
        definition = _compose_definition(validated_anchor, root)
        return definition.build()


def discover_evals(anchor: str | Path) -> EvalSuite:
    """Return validated, sorted ADK eval paths without building the agent."""

    return _discover_evals(_validate_anchor(anchor).parent / "evals")


def _resolve_compile_anchor(source: str | Path, entrypoint: str) -> Path:
    if not isinstance(entrypoint, str):
        raise TypeError("agent entrypoint must be a module:symbol string")
    module_name, separator, export_name = entrypoint.partition(":")
    if (
        separator != ":"
        or module_name != "agent"
        or not export_name.isidentifier()
    ):
        raise BundleConventionError(
            "filesystem agent entrypoint must use agent:<python_identifier>"
        )

    path = Path(source)
    if path.is_symlink():
        raise BundleConventionError(f"agent source cannot be a symlink: {path}")
    if path.is_dir():
        path = path / "agent.py"
    return _validate_anchor(path)


def _validate_anchor(anchor: str | Path) -> Path:
    path = Path(anchor)
    if path.is_symlink():
        raise BundleConventionError(f"agent bundle anchor cannot be a symlink: {path}")
    path = path.resolve()
    if not path.is_file():
        raise BundleConventionError(f"agent bundle anchor does not exist: {path}")
    if path.name != "agent.py":
        raise BundleConventionError(
            f"agent bundle anchor must be named agent.py, got: {path}"
        )
    return path


def _has_public_entries(directory: Path, *, kind: str) -> bool:
    """Return whether an optional resource directory contains authored content."""

    if directory.is_symlink():
        raise BundleConventionError(f"{kind} directory cannot be a symlink: {directory}")
    if not directory.exists():
        return False
    if not directory.is_dir():
        raise BundleConventionError(f"{kind} path must be a directory: {directory}")
    for path in directory.iterdir():
        if path.is_symlink():
            raise BundleConventionError(f"{kind} resource cannot be a symlink: {path}")
        if _is_ignored(path):
            continue
        if path.is_dir():
            if _has_public_entries(path, kind=kind):
                return True
            continue
        if path.is_file():
            return True
        raise BundleConventionError(f"unsupported {kind} resource: {path}")
    return False


def _reject_advanced_filesystem_resources(bundle_root: Path, *, framework: str) -> None:
    """Advanced applications own wiring, so resources must be explicit."""

    resource_directories = (
        "tools",
        "subagents",
        "mcp",
        "extensions",
        "plugins",
        "sandbox",
        "skills",
        "evals",
    )
    populated = [
        name
        for name in resource_directories
        if _has_public_entries(bundle_root / name, kind=name)
    ]
    if populated:
        rendered = ", ".join(f"{name}/" for name in populated)
        raise BundleConventionError(
            f"advanced {framework} mode does not compose filesystem resources from: "
            f"{rendered}; wire them into Agent.advanced(...) in agent.py or remove them"
        )


def _graph_contains_agent(graph: Graph) -> bool:
    return any(
        isinstance(value, AgentDefinition)
        or (isinstance(value, Graph) and _graph_contains_agent(value))
        for value in graph.nodes.values()
    )


def _graph_string_references(graph: Graph) -> set[str]:
    references: set[str] = set()
    for value in graph.nodes.values():
        if isinstance(value, str):
            references.add(value)
        elif isinstance(value, Graph):
            references.update(_graph_string_references(value))
    return references


def _resolve_graph_resources(
    anchor: Path, graph: Graph, *, framework: str
) -> Graph:
    """Resolve string graph nodes from filesystem-discovered tools/subagents."""

    resources = _discover_folder_resources(anchor.parent, framework, is_root=True)
    has_root_agent_node = _graph_contains_agent(graph)
    referenced = _graph_string_references(graph)
    registry, resource_kinds = _graph_resource_registry(resources)
    _validate_graph_resource_consumption(
        graph, resources, resource_kinds, referenced, has_root_agent_node, framework
    )
    return replace(
        graph,
        nodes={
            name: _resolve_graph_node(value, graph.name, registry, anchor, framework)
            for name, value in graph.nodes.items()
        },
    )


def _graph_resource_registry(
    resources: _DiscoveredResources,
) -> tuple[dict[str, Any], dict[str, str]]:
    registry: dict[str, Any] = {}
    kinds: dict[str, str] = {}
    values = (
        *((value, "tool", _tool_name(value)) for value in resources.tools),
        *((value, "subagent", _subagent_name(value)) for value in resources.subagents),
    )
    for value, kind, name in values:
        if name in registry:
            raise BundleDuplicateError(f"duplicate graph resource {name!r}")
        registry[name] = value
        kinds[name] = kind
    return registry, kinds


def _validate_graph_resource_consumption(
    graph: Graph,
    resources: _DiscoveredResources,
    resource_kinds: dict[str, str],
    referenced: set[str],
    has_root_agent: bool,
    framework: str,
) -> None:
    ignored = [
        f"{kind} {name!r}"
        for name, kind in resource_kinds.items()
        if name not in referenced
        and not _root_agent_consumes(kind, has_root_agent, framework)
    ]
    if ignored:
        raise BundleConventionError(
            f"graph {graph.name!r} does not consume discovered resources: "
            + ", ".join(sorted(ignored))
            + "; reference them as graph nodes or remove them"
        )
    if has_root_agent:
        return
    _reject_unconsumed_graph_capabilities(graph.name, resources)


def _root_agent_consumes(kind: str, present: bool, framework: str) -> bool:
    return present and (kind == "tool" or (kind == "subagent" and framework == "adk"))


def _reject_unconsumed_graph_capabilities(
    graph_name: str, resources: _DiscoveredResources
) -> None:
    if resources.mcp:
        detail = "MCP clients"
    elif resources.skill_directories:
        detail = "skills"
    elif resources.sandbox is not None:
        detail = "sandbox configuration"
    else:
        return
    raise BundleConventionError(
        f"graph {graph_name!r} has {detail} but no Agent node in the root graph "
        "to consume them; filesystem subagents use only their own resources"
    )


def _resolve_graph_node(
    value: Any,
    graph_name: str,
    registry: dict[str, Any],
    anchor: Path,
    framework: str,
) -> Any:
    if isinstance(value, str):
        try:
            # String nodes retain the folder scope established during discovery;
            # recomposing them here would leak root resources into subagents.
            return registry[value]
        except KeyError as exc:
            raise BundleExportError(
                f"graph {graph_name!r} references unknown filesystem resource {value!r}"
            ) from exc
    if isinstance(value, Graph):
        return replace(
            value,
            nodes={
                name: _resolve_graph_node(node, graph_name, registry, anchor, framework)
                for name, node in value.nodes.items()
            },
        )
    if isinstance(value, AgentDefinition):
        return _compose_definition(
            anchor, value, framework=framework, graph_resource_context=True
        )
    return value


def _compose_definition(
    anchor: Path,
    root: AgentDefinition,
    *,
    is_root: bool = True,
    framework: str = "adk",
    graph_resource_context: bool = False,
) -> AgentDefinition:
    bundle_root = anchor.parent
    _validate_folder_scope(bundle_root, is_root)
    instruction = _resolve_instruction(bundle_root, root.instruction)
    resources = _discover_folder_resources(bundle_root, framework, is_root=is_root)
    tools = _merge_named_sources(
        "tool",
        ("explicit", root.tools),
        ("discovered", resources.tools),
        identity=_tool_name,
    )
    discovered_subagents = _agent_subagents_for_framework(
        resources.subagents, framework, graph_resource_context
    )
    subagents = _merge_named_sources(
        "subagent",
        ("explicit", root.subagents),
        ("discovered", discovered_subagents),
        identity=_subagent_name,
    )
    mcp = _merge_mcp_sources(
        ("explicit", root.mcp),
        ("discovered", resources.mcp),
    )
    tools = _attach_skill_tools(framework, resources.skill_directories, tools)
    sandbox = _resolve_sandbox(root.sandbox, resources.sandbox, framework)
    return replace(
        root,
        instruction=instruction,
        tools=tools,
        subagents=subagents,
        mcp=mcp,
        sandbox=sandbox,
    )


def _validate_folder_scope(bundle_root: Path, is_root: bool) -> None:
    legacy_mcp = bundle_root / "mcp_servers"
    if legacy_mcp.exists() or legacy_mcp.is_symlink():
        raise BundleConventionError(
            f"unsupported legacy MCP directory {legacy_mcp}; use mcp/"
        )
    if is_root:
        return
    root_only_resources = (
        ("plugins", "capability plugins"),
        ("extensions", "runtime extensions"),
        ("lib", "authored libraries"),
    )
    for directory, label in root_only_resources:
        path = bundle_root / directory
        if _has_public_entries(path, kind=directory):
            # These hooks affect the whole application, so accepting them below
            # a subagent would create ambiguous lifecycle and ownership order.
            raise BundleConventionError(
                f"{label} are root-only; move {path} to the root agent folder"
            )


def _discover_folder_resources(
    bundle_root: Path, framework: str, *, is_root: bool
) -> _DiscoveredResources:
    tools = _discover_tools(bundle_root / "tools")
    subagents = _discover_subagents(bundle_root / "subagents", framework=framework)
    discovered_mcp = _discover_mcp(bundle_root / "mcp")
    plugins = _discover_capability_plugins(bundle_root / "plugins") if is_root else ()
    plugin_mcp = _discover_plugin_mcp(plugins)
    mcp = _merge_mcp_sources(
        ("discovered", discovered_mcp),
        *((f"plugin {plugin.name!r}", clients) for plugin, clients in plugin_mcp),
    )
    sandbox = _discover_sandbox(bundle_root / "sandbox")
    skill_directories = _bundle_skill_directories(bundle_root, plugins)
    _validate_framework_evals(bundle_root / "evals", framework)
    return _DiscoveredResources(
        tools=tools,
        subagents=subagents,
        mcp=mcp,
        sandbox=sandbox,
        skill_directories=skill_directories,
    )


def _validate_framework_evals(directory: Path, framework: str) -> None:
    if framework == "adk":
        _discover_evals(directory)
        return
    if _has_public_entries(directory, kind="evals"):
        raise BundleConventionError(
            "evals/ uses ADK evaluation files and cannot be compiled by the "
            "LangGraph backend"
        )


def _agent_subagents_for_framework(
    discovered: tuple[AgentDefinition, ...],
    framework: str,
    graph_resource_context: bool,
) -> tuple[AgentDefinition, ...]:
    if framework != "langgraph":
        return discovered
    if discovered and not graph_resource_context:
        rendered = ", ".join(
            f"subagent {_subagent_name(value)!r}" for value in discovered
        )
        raise BundleConventionError(
            "LangGraph Agent definitions cannot consume discovered subagents: "
            f"{rendered}; use a Graph and reference each subagent explicitly"
        )
    # LangGraph requires explicit graph edges rather than implicit delegation.
    return ()


def _attach_skill_tools(
    framework: str, skill_directories: Sequence[Path], tools: tuple[Any, ...]
) -> tuple[Any, ...]:
    if framework == "adk":
        skill_toolset = _discover_skill_toolset(skill_directories, tools)
        return tools if skill_toolset is None else (*tools, skill_toolset)
    return (*tools, *_discover_portable_skill_tools(skill_directories, tools))


def _resolve_sandbox(
    explicit: Sandbox | None, discovered: Sandbox | None, framework: str
) -> Sandbox | None:
    if explicit is not None and discovered is not None:
        raise BundleDuplicateError(
            "sandbox is configured explicitly and in sandbox/sandbox.py"
        )
    resolved = explicit if explicit is not None else discovered
    if framework == "langgraph" and resolved is not None:
        raise BundleConventionError(
            "sandbox/ currently requires the ADK backend; expose sandboxed "
            "execution as a graph tool for LangGraph"
        )
    return resolved


def _resolve_instruction(bundle_root: Path, explicit: str | None) -> str:
    path = bundle_root / "instructions.md"
    contents = _read_required_text(path, kind="agent instructions")
    return explicit if explicit is not None else contents


def _read_required_text(path: Path, *, kind: str) -> str:
    if path.is_symlink():
        raise BundleConventionError(f"{kind} cannot be a symlink: {path}")
    if not path.is_file():
        raise BundleConventionError(f"required {kind} file is missing: {path}")
    try:
        size = path.stat().st_size
        if size > _MAX_METADATA_FILE_BYTES:
            raise BundleConventionError(
                f"{kind} file exceeds {_MAX_METADATA_FILE_BYTES} bytes: {path}"
            )
        contents = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise BundleConventionError(f"{kind} must be UTF-8 text: {path}") from exc
    except OSError as exc:
        raise BundleConventionError(f"unable to read {kind} at {path}: {exc}") from exc
    if not contents:
        raise BundleConventionError(f"{kind} must not be empty: {path}")
    return contents


def _discover_tools(directory: Path) -> tuple[Callable[..., Any], ...]:
    tools = []
    for path in _resource_files(directory, kind="tools"):
        name = path.stem
        module, value = _load_export(path, name)
        if not callable(value) or not getattr(value, "__harnest_tool__", False):
            raise BundleExportError(
                f"tool module {path} must export @tool callable {name!r}"
            )
        if getattr(value, "__name__", None) != name:
            raise BundleExportError(
                f"tool exported by {path} must be named {name!r}; "
                f"got {getattr(value, '__name__', None)!r}"
            )
        _reject_extra_exports(
            module,
            path,
            name,
            kind="tool",
            predicate=lambda item: callable(item)
            and getattr(item, "__harnest_tool__", False),
        )
        tools.append(value)
    return tuple(tools)


def _discover_subagents(
    directory: Path, *, framework: str = "adk"
) -> tuple[AgentDefinition, ...]:
    if directory.is_symlink():
        raise BundleConventionError(
            f"subagents directory cannot be a symlink: {directory}"
        )
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise BundleConventionError(f"subagents path must be a directory: {directory}")

    entries = [
        entry
        for path in directory.iterdir()
        if (entry := _subagent_entry(path)) is not None
    ]
    return _load_subagent_entries(entries, framework=framework)


def _subagent_entry(path: Path) -> tuple[str, Path, bool] | None:
    if path.is_symlink():
        raise BundleConventionError(f"subagent resource cannot be a symlink: {path}")
    if _is_ignored(path):
        return None
    if path.is_file():
        if path.suffix != ".py":
            raise BundleConventionError(
                f"unexpected file in subagents directory: {path}; "
                "use a public .py resource or prefix helper files with _"
            )
        _validate_resource_name(path.stem, path)
        return path.stem, path, False
    if path.is_dir():
        return _nested_subagent_entry(path)
    raise BundleConventionError(f"unsupported subagent resource: {path}")


def _nested_subagent_entry(path: Path) -> tuple[str, Path, bool]:
    _validate_resource_name(path.name, path)
    nested_anchor = path / "agent.py"
    if nested_anchor.is_symlink():
        raise BundleConventionError(
            f"nested subagent agent.py cannot be a symlink: {nested_anchor}"
        )
    if not nested_anchor.is_file():
        raise BundleConventionError(
            f"nested subagent directory must contain agent.py: {path}"
        )
    return path.name, nested_anchor, True


def _load_subagent_entries(
    entries: Sequence[tuple[str, Path, bool]], *, framework: str
) -> tuple[AgentDefinition, ...]:
    agents = []
    seen_exports: dict[str, Path] = {}
    for export_name, path, nested in sorted(entries, key=lambda item: item[0]):
        previous = seen_exports.get(export_name)
        if previous is not None:
            raise BundleDuplicateError(
                f"duplicate subagent export {export_name!r}: {previous} and {path}"
            )
        seen_exports[export_name] = path
        module, value = _load_export(path, export_name)
        if not isinstance(value, AgentDefinition):
            raise BundleExportError(
                f"subagent module {path} must export AgentDefinition {export_name!r}; "
                f"got {type(value).__name__}"
            )
        if value.name != export_name:
            raise BundleExportError(
                f"subagent exported by {path} must have name {export_name!r}; "
                f"got {value.name!r}"
            )
        _reject_extra_exports(
            module,
            path,
            export_name,
            kind="subagent",
            predicate=lambda item: isinstance(item, AgentDefinition),
        )
        if not nested and value.instruction is None:
            raise BundleExportError(
                f"flat subagent {path} must provide instruction explicitly; "
                "use a nested subagent folder to load instructions.md"
            )
        agents.append(
            _compose_definition(
                path, value, is_root=False, framework=framework
            )
            if nested
            else value
        )
    return tuple(agents)


def _discover_capability_plugins(directory: Path) -> tuple[PluginResources, ...]:
    try:
        return discover_plugins(directory)
    except PluginConventionError as exc:
        raise BundleConventionError(str(exc)) from exc


def _discover_plugin_mcp(
    plugins: Sequence[PluginResources],
) -> tuple[tuple[PluginResources, tuple[MCPClient, ...]], ...]:
    """Load each plugin's MCP clients without an aggregation module."""

    return tuple(
        (plugin, _discover_mcp(plugin.directory / "mcp"))
        for plugin in plugins
    )


def _discover_mcp(directory: Path) -> tuple[MCPClient, ...]:
    clients = []
    for path in _resource_files(directory, kind="mcp"):
        name = path.stem
        module, value = _load_export(path, name)
        if value is None:
            _reject_extra_exports(
                module,
                path,
                name,
                kind="MCP client",
                predicate=lambda item: isinstance(item, MCPClient),
            )
            continue
        if not isinstance(value, MCPClient):
            raise BundleExportError(
                f"MCP module {path} must export MCPClient {name!r} or set it "
                f"to None to disable it; got {type(value).__name__}"
            )
        _reject_extra_exports(
            module,
            path,
            name,
            kind="MCP client",
            predicate=lambda item: isinstance(item, MCPClient),
        )
        clients.append(value)
    return tuple(clients)


def _discover_sandbox(directory: Path) -> Sandbox | None:
    files = _resource_files(directory, kind="sandbox")
    if not files:
        return None
    if len(files) != 1 or files[0].name != "sandbox.py":
        rendered = ", ".join(path.name for path in files)
        raise BundleConventionError(
            "sandbox directory must contain only sandbox.py; "
            f"found: {rendered}"
        )
    path = files[0]
    module, value = _load_export(path, "sandbox")
    if not isinstance(value, Sandbox):
        raise BundleExportError(
            f"sandbox module {path} must export Sandbox 'sandbox'; "
            f"got {type(value).__name__}"
        )
    _reject_extra_exports(
        module,
        path,
        "sandbox",
        kind="sandbox",
        predicate=lambda item: isinstance(item, Sandbox),
    )
    return value


def _discover_skill_toolset(
    skill_directories: Sequence[Path], tools: Sequence[Any]
) -> Any | None:
    if not skill_directories:
        return None

    tool_names = {_tool_name(item) for item in tools}
    conflicts = sorted(tool_names & _SKILL_TOOL_NAMES)
    if conflicts:
        rendered = ", ".join(repr(name) for name in conflicts)
        raise BundleDuplicateError(
            f"tools conflict with ADK SkillToolset names: {rendered}"
        )

    load_skill_from_dir, skill_toolset_class = _adk_skill_api()
    skills = _load_adk_skills(skill_directories, load_skill_from_dir)
    expected_names = [path.name for path in skill_directories]
    loaded_names = [getattr(skill, "name", None) for skill in skills]
    if loaded_names != expected_names:
        raise BundleSkillError(
            f"ADK loaded unexpected skills: expected "
            f"{expected_names!r}, got {loaded_names!r}"
        )
    try:
        return skill_toolset_class(skills=skills)
    except Exception as exc:
        raise BundleSkillError(
            "failed to create ADK SkillToolset: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _adk_skill_api() -> tuple[Callable[..., Any], type[Any]]:
    try:
        from google.adk.skills import load_skill_from_dir
        from google.adk.tools.skill_toolset import SkillToolset
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise BundleImportError(
            "discovered skills require Google ADK 2.x skill support"
        ) from exc
    return load_skill_from_dir, SkillToolset


def _load_adk_skills(
    skill_directories: Sequence[Path], loader: Callable[..., Any]
) -> list[Any]:
    try:
        return [loader(path) for path in skill_directories]
    except Exception as exc:
        raise BundleSkillError(
            f"failed to load skills: {type(exc).__name__}: {exc}"
        ) from exc


def _discover_portable_skill_tools(
    skill_directories: Sequence[Path], tools: Sequence[Any]
) -> tuple[Callable[..., Any], ...]:
    """Expose filesystem skills through framework-neutral progressive loaders."""

    if not skill_directories:
        return ()
    conflicts = sorted({_tool_name(item) for item in tools} & _SKILL_TOOL_NAMES)
    if conflicts:
        raise BundleDuplicateError(
            "tools conflict with Harnest skill tools: "
            + ", ".join(repr(name) for name in conflicts)
        )
    skills = {path.name: path for path in skill_directories}

    def _skill_path(name: str) -> Path:
        if name not in skills:
            raise ValueError(
                f"unknown skill {name!r}; available: {', '.join(sorted(skills))}"
            )
        return skills[name]

    def list_skills() -> str:
        """List the names of skills available for progressive loading."""

        return json.dumps({"skills": sorted(skills)})

    def load_skill(name: str) -> str:
        """Load the complete SKILL.md instructions for one named skill."""

        return _read_text(_skill_path(name) / "SKILL.md", kind="skill manifest")

    def load_skill_resource(name: str, path: str) -> str:
        """Load a UTF-8 resource contained within one named skill directory."""

        if not isinstance(path, str) or not path or Path(path).is_absolute():
            raise ValueError("skill resource path must be a non-empty relative path")
        root = _skill_path(name).resolve()
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("skill resource path escapes its skill directory") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"skill resource does not exist: {path}")
        return _read_text(candidate, kind="skill resource")

    return (list_skills, load_skill, load_skill_resource)


def _bundle_skill_directories(
    bundle_root: Path, plugins: Sequence[PluginResources]
) -> tuple[Path, ...]:
    """Merge direct skills with directory-owned capability plugin skills."""

    resolved = [
        *_skill_directories(bundle_root / "skills"),
        *(path for plugin in plugins for path in plugin.skill_directories),
    ]
    return _merge_skill_directories(resolved)


def _merge_skill_directories(
    *groups: Sequence[Path],
) -> tuple[Path, ...]:
    resolved: list[Path] = []
    by_name: dict[str, Path] = {}
    for path in (path for group in groups for path in group):
        canonical = path.resolve()
        previous = by_name.get(path.name)
        if previous is not None and previous.resolve() != canonical:
            raise BundleDuplicateError(
                f"duplicate skill name {path.name!r}: {previous} and {path}"
            )
        if previous is not None:
            continue
        by_name[path.name] = path
        resolved.append(path)
    return tuple(resolved)


def _skill_directories(directory: Path) -> tuple[Path, ...]:
    if directory.is_symlink():
        raise BundleConventionError(f"skills directory cannot be a symlink: {directory}")
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise BundleConventionError(f"skills path must be a directory: {directory}")

    skill_directories = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise BundleConventionError(f"skill resource cannot be a symlink: {path}")
        if _is_ignored(path):
            continue
        _validate_skill_directory(path)
        skill_directories.append(path)
    return tuple(skill_directories)


def _validate_skill_directory(path: Path) -> None:
    if path.is_symlink():
        raise BundleConventionError(f"skill resource cannot be a symlink: {path}")
    if not path.is_dir():
        raise BundleConventionError(
            f"unexpected resource in skills directory: {path}; "
            "each public entry must be a skill directory"
        )
    manifest = path / "SKILL.md"
    if manifest.is_symlink():
        raise BundleConventionError(f"skill manifest cannot be a symlink: {manifest}")
    if not manifest.is_file():
        raise BundleConventionError(
            f"skill directory must contain uppercase SKILL.md: {path}"
        )
    _validate_tree_has_no_symlinks(path, kind="skill")
    if manifest.stat().st_size > _MAX_METADATA_FILE_BYTES:
        raise BundleConventionError(
            f"skill manifest exceeds {_MAX_METADATA_FILE_BYTES} bytes: {manifest}"
        )


def _validate_tree_has_no_symlinks(directory: Path, *, kind: str) -> None:
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise BundleConventionError(f"{kind} resource cannot be a symlink: {path}")


def _discover_evals(directory: Path) -> EvalSuite:
    if directory.is_symlink():
        raise BundleConventionError(f"evals directory cannot be a symlink: {directory}")
    if not directory.exists():
        return EvalSuite(())
    if not directory.is_dir():
        raise BundleConventionError(f"evals path must be a directory: {directory}")

    eval_paths = []
    config_path = None
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        kind = _eval_file_kind(path)
        if kind == "ignored":
            continue
        if kind == "config":
            config_path = path
        else:
            eval_paths.append(path)
    if not eval_paths:
        if config_path is not None:
            raise BundleConventionError(
                f"evals directory must contain at least one *.evalset.json: {directory}"
            )
        return EvalSuite(())
    _validate_eval_files(eval_paths, config_path)
    return EvalSuite(tuple(eval_paths), config_path)


def _eval_file_kind(path: Path) -> str:
    if path.is_symlink():
        raise BundleConventionError(f"eval resource cannot be a symlink: {path}")
    if _is_ignored(path):
        return "ignored"
    if not path.is_file():
        raise BundleConventionError(
            f"unexpected resource in evals directory: {path}; "
            "evals must use a flat file layout"
        )
    if path.name == "test_config.json":
        return "config"
    if path.name.endswith(".evalset.json"):
        return "eval"
    raise BundleConventionError(
        f"unexpected eval file {path}; expected *.evalset.json or test_config.json"
    )


def _validate_eval_files(eval_paths: Sequence[Path], config_path: Path | None) -> None:
    try:
        from google.adk.evaluation.eval_config import EvalConfig
        from google.adk.evaluation.eval_set import EvalSet
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise BundleImportError(
            "discovered evals require Google ADK 2.x evaluation support"
        ) from exc

    for path in eval_paths:
        _validate_eval_set(path, EvalSet)
    if config_path is not None:
        _validate_eval_config(config_path, EvalConfig)


def _validate_eval_set(path: Path, eval_set_class: type[Any]) -> None:
    payload = _read_json_payload(path, kind="eval set")
    try:
        eval_set = eval_set_class.model_validate_json(payload)
    except Exception as exc:
        raise BundleEvalError(
            f"invalid ADK eval set {path}: {type(exc).__name__}: {exc}"
        ) from exc
    expected_id = path.name[: -len(".evalset.json")]
    if eval_set.eval_set_id != expected_id:
        raise BundleEvalError(
            f"eval_set_id in {path} must match filename {expected_id!r}; "
            f"got {eval_set.eval_set_id!r}"
        )
    case_ids = [case.eval_id for case in eval_set.eval_cases]
    duplicates = sorted({name for name in case_ids if case_ids.count(name) > 1})
    if duplicates:
        raise BundleEvalError(f"duplicate eval_id values in {path}: {duplicates!r}")


def _validate_eval_config(path: Path, eval_config_class: type[Any]) -> None:
    payload = _read_json_payload(path, kind="eval config")
    try:
        eval_config_class.model_validate_json(payload)
    except Exception as exc:
        raise BundleEvalError(
            f"invalid ADK eval config {path}: {type(exc).__name__}: {exc}"
        ) from exc


def _read_json_payload(path: Path, *, kind: str) -> str:
    try:
        if path.stat().st_size > _MAX_METADATA_FILE_BYTES:
            raise BundleEvalError(
                f"{kind} exceeds {_MAX_METADATA_FILE_BYTES} bytes: {path}"
            )
        payload = path.read_text(encoding="utf-8")
        json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
        return payload
    except UnicodeDecodeError as exc:
        raise BundleEvalError(f"{kind} must be UTF-8 JSON: {path}") from exc
    except BundleEvalError:
        raise
    except (OSError, ValueError) as exc:
        raise BundleEvalError(f"invalid {kind} JSON at {path}: {exc}") from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _resource_files(directory: Path, *, kind: str) -> tuple[Path, ...]:
    if directory.is_symlink():
        raise BundleConventionError(
            f"{kind} directory cannot be a symlink: {directory}"
        )
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise BundleConventionError(f"{kind} path must be a directory: {directory}")

    files = []
    for path in directory.iterdir():
        if path.is_symlink():
            raise BundleConventionError(f"{kind} resource cannot be a symlink: {path}")
        if _is_ignored(path):
            continue
        if not path.is_file() or path.suffix != ".py":
            raise BundleConventionError(
                f"unexpected resource in {kind} directory: {path}; "
                "use a public .py resource or prefix helper files with _"
            )
        _validate_resource_name(path.stem, path)
        files.append(path)
    return tuple(sorted(files, key=lambda item: item.name))


def _is_ignored(path: Path) -> bool:
    return (
        path.name in _IGNORED_FILE_NAMES
        or path.name.startswith(".")
        or path.name.startswith("_")
    )


def _validate_resource_name(name: str, path: Path) -> None:
    if not name.isidentifier():
        raise BundleConventionError(
            f"resource name must be a valid Python identifier: {path}"
        )


def _load_export(path: Path, export_name: str) -> tuple[ModuleType, Any]:
    module = _load_module(path)
    if not hasattr(module, export_name):
        raise BundleExportError(
            f"resource module {path} must export {export_name!r} "
            "(the file or directory name)"
        )
    return module, getattr(module, export_name)


def _reject_extra_exports(
    module: ModuleType,
    path: Path,
    expected_name: str,
    *,
    kind: str,
    predicate: Callable[[Any], bool],
) -> None:
    extras = sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and name != expected_name and predicate(value)
    )
    if extras:
        rendered = ", ".join(repr(name) for name in extras)
        raise BundleExportError(
            f"resource module {path} exports additional {kind} resources: {rendered}; "
            "use one public resource per file"
        )


def _load_module(path: Path) -> ModuleType:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module_name = f"_harnest_bundle_{digest}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise BundleImportError(f"unable to create an import spec for resource: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise BundleImportError(
            f"failed to import resource {path}: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def _tool_name(value: Any) -> str:
    name = getattr(value, "__name__", None)
    if not isinstance(name, str) or not name:
        name = getattr(value, "name", None)
    if not isinstance(name, str) or not name:
        raise BundleDuplicateError(
            f"tool {value!r} has no stable __name__ or name; use a named tool"
        )
    return name


def _subagent_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if not isinstance(name, str) or not name:
        raise BundleDuplicateError(
            f"subagent {value!r} has no stable name"
        )
    return name


def _merge_named_sources(
    kind: str,
    *sources: tuple[str, Sequence[_T]],
    identity: Callable[[_T], str],
) -> tuple[_T, ...]:
    merged = []
    seen: dict[str, str] = {}
    for source, values in sources:
        for value in values:
            name = identity(value)
            previous = seen.get(name)
            if previous is not None:
                raise BundleDuplicateError(
                    f"duplicate {kind} {name!r}: {previous} and {source} resources"
                )
            seen[name] = source
            merged.append(value)
    return tuple(merged)


def _merge_mcp_sources(
    *sources: tuple[str, Sequence[MCPClient]],
) -> tuple[MCPClient, ...]:
    merged = []
    origins: list[str] = []
    for source, values in sources:
        for client in values:
            if client in merged:
                previous = origins[merged.index(client)]
                raise BundleDuplicateError(
                    "duplicate MCP client configuration: "
                    f"{previous} and {source} resources"
                )
            merged.append(client)
            origins.append(source)
    return tuple(merged)

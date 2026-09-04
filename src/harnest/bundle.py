"""Filesystem-first agent composition."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping, Sequence, TypeVar

from .agent import AgentDefinition, _AdvancedAgentDefinition
from .authoring_errors import authoring_guidance, folder_entry_error, inactive_entry_hint
from .application import CompiledApplication
from .checkpoint import (
    ADKStore,
    CheckpointAuthority,
    HarnestStore,
    LangGraphStore,
    checkpoint_metadata,
)
from .compatibility import (
    FrameworkCompatibilityError,
    validate_framework_compatibility,
)
from .cron import CompiledCron, Cron
from .backends import (
    AdvancedBackendValidationError,
    BackendDependencyError,
    UnknownBackendError,
    get_backend,
)
from .graph import Graph
from .extension_loader import (
    ExtensionDiscoveryError,
    ExtensionSource,
    discover_extension_sources,
)
from ._library import (
    LibraryConventionError,
    LibraryImportError as AuthoredLibraryImportError,
    authored_library,
)
from .mcp import MCPClient, _mcp_connection_configuration
from .model_hooks import portable_model_extension
from .approval import approval_policy
from .plugin import PluginConventionError, PluginResources, discover_plugins
from .plugins import (
    ActivatedPlugin,
    PluginImportError,
    PluginNamespaceError,
    activate_runtime_plugins,
    release_runtime_plugins,
)
from .runtime_plugins import (
    RuntimePluginConventionError,
    RuntimePluginDescriptor,
    discover_application_extensions,
    validate_runtime_plugin_dependencies,
)
from .sandbox import Sandbox
from .sandbox_catalog import (
    SandboxCatalogError, current_sandbox_catalog, independent_sandbox_catalog, resolve_sandbox_assignments,
    sandbox_catalog_scope,
)
from .server_config import SERVER_CONFIG_FILENAME, project_server_config_yaml
from .skills import (
    FilesystemSkillSource,
    SkillRegistry,
    SkillScope,
    SkillSource,
    SkillValidationError,
    create_skill_tools,
    scoped_skill_sources,
)
from .source_tree import ignored_source_path
from .application_layout import ApplicationLayoutError, lifecycle_directory
from .task import CompiledTask, registration_for as task_registration_for

_T = TypeVar("_T")
_IGNORED_FILE_NAMES = {"__init__.py"}
_MAX_METADATA_FILE_BYTES = 16 * 1024 * 1024
_PROCRASTINATE_REQUIREMENT = "procrastinate==3.9.0"
_SKILL_TOOL_NAMES = {
    "list_skills",
    "load_skill",
    "load_skill_resource",
    "run_skill_script",
}
_RUNTIME_PLUGIN_ENTRIES = frozenset(
    {
        "plugin.yaml",
        "plugin.py",
        "pyproject.toml",
        "extensions",
        "lib",
        "mcp",
        "skills",
        "subagents",
        "tools",
    }
)
_RUNTIME_PLUGIN_CONTENT_CAPABILITIES = {
    "tools": "content.tools",
    "mcp": "content.mcp",
    "skills": "content.skills",
    "subagents": "content.subagents",
}
_RUNTIME_PLUGIN_PHASE_CAPABILITIES = {
    "authenticate": "lifecycle.http",
    "before_invoke": "lifecycle.agent",
    "after_invoke": "lifecycle.agent",
    "on_event": "lifecycle.agent",
    "on_error": "lifecycle.agent",
    "before_model": "lifecycle.model",
    "after_model": "lifecycle.model",
    "on_model_error": "lifecycle.model",
    "before_tool": "lifecycle.tool",
    "after_tool": "lifecycle.tool",
    "on_tool_error": "lifecycle.tool",
    "before_http": "lifecycle.http",
    "after_http": "lifecycle.http",
    "on_http_error": "lifecycle.http",
    "before_mcp": "lifecycle.mcp",
    "after_mcp": "lifecycle.mcp",
    "on_mcp_error": "lifecycle.mcp",
    "resource": "context.resources",
    "context": "context.resources",
    "session_store": "storage.sessions",
    "checkpointer": "storage.checkpoints",
    "asset_store": "storage.assets",
    "custom_store": "storage.custom",
    "credential_provider": "context.credentials",
    "skill_source": "lifecycle.skills",
    "http_routes": "http.routes",
    "output_policy": "policy.output",
    "telemetry_exporter": "telemetry.exporter",
    "adk_plugin": "native.adk",
    "langgraph_middleware": "native.langgraph",
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
    subagents: tuple[AgentDefinition | _AdvancedAgentDefinition, ...]
    mcp: tuple[MCPClient, ...]
    sandboxes: Mapping[str, Sandbox]
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
    from .project_lock import verify_framework_lock

    try:
        verify_framework_lock(anchor.parent, compatibility)
    except FrameworkCompatibilityError as exc:
        raise BundleImportError(str(exc)) from exc
    if mode == "advanced":
        _reject_advanced_plugin_files(anchor.parent, framework)
    runtime_plugins = _discover_runtime_plugins(anchor.parent)
    _validate_runtime_plugin_project_set(anchor.parent, runtime_plugins)
    _validate_runtime_plugin_layouts(runtime_plugins)
    export_name = entrypoint.partition(":")[2]
    activated_plugins: tuple[ActivatedPlugin, ...] = ()
    try:
        with _bundle_library(anchor.parent), independent_sandbox_catalog():
            # Plugins share the agent interpreter and may import application
            # helpers, so the authored namespace must exist before plugin.py.
            activated_plugins = _activate_bundle_runtime_plugins(runtime_plugins)
            module, value = _load_export(anchor, export_name)
            if mode == "advanced":
                return _compile_advanced_application(
                    anchor,
                    module,
                    value,
                    export_name,
                    framework,
                    backend,
                    compatibility,
                    runtime_plugins,
                    activated_plugins,
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
                runtime_plugins,
                activated_plugins,
            )
    except Exception:
        # A successful compilation transfers this acquisition to the runtime
        # manager; failed compilation has no later owner that could release it.
        if activated_plugins:
            release_runtime_plugins(runtime_plugins)
        raise


@contextmanager
def _bundle_library(bundle_root: Path) -> Iterator[None]:
    """Translate authored namespace failures into the compiler error model."""

    try:
        with authored_library(bundle_root):
            yield
    except LibraryConventionError as exc:
        raise BundleConventionError(str(exc)) from exc
    except AuthoredLibraryImportError as exc:
        raise BundleImportError(str(exc)) from exc


def _activate_bundle_runtime_plugins(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> tuple[ActivatedPlugin, ...]:
    """Acquire namespaces whose lifetime transfers to the compiled runtime."""

    try:
        return activate_runtime_plugins(descriptors)
    except PluginImportError as exc:
        raise BundleImportError(str(exc)) from exc
    except PluginNamespaceError as exc:
        raise BundleConventionError(str(exc)) from exc


def _compile_advanced_application(
    anchor: Path,
    module: ModuleType,
    value: Any,
    export_name: str,
    framework: str,
    backend: Any,
    compatibility: Any,
    runtime_plugins: Sequence[RuntimePluginDescriptor],
    activated_plugins: Sequence[ActivatedPlugin],
) -> CompiledApplication:
    """Validate native ownership while retaining Harnest's runtime boundaries."""

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
    _reject_advanced_plugin_resources(anchor.parent, runtime_plugins, framework)
    discovered_extensions = _load_extensions(
        anchor.parent, framework, runtime_plugins
    )
    _validate_plugin_extension_capabilities(
        runtime_plugins, discovered_extensions.declared_listeners
    )
    try:
        advanced = backend.validate_advanced(
            value, fallback_name=anchor.parent.name
        )
    except BackendDependencyError as exc:
        raise BundleImportError(str(exc)) from exc
    except AdvancedBackendValidationError as exc:
        raise BundleExportError(str(exc)) from exc
    credential_extensions = _credential_native_extensions(
        discovered_extensions.credential_provider,
        discovered_extensions.native,
        framework=framework,
    )
    advanced = _attach_advanced_native_extensions(
        advanced,
        discovered_extensions.native,
        framework=framework,
        prepend_extensions=credential_extensions,
    )
    advanced = _ensure_adk_resumability(advanced, framework=framework)
    _validate_advanced_checkpointer(
        advanced, discovered_extensions.checkpointer, framework=framework
    )
    skill_registry = _advanced_skill_registry(
        advanced.name, discovered_extensions.skill_sources
    )
    tasks, crons = _discover_tasks_and_crons(anchor.parent, advanced.name)
    return CompiledApplication(
        name=advanced.name,
        framework=framework,
        mode="advanced",
        target=advanced.target,
        native_app=advanced.native_app,
        kind="advanced",
        bridge=value,
        extensions=discovered_extensions.listeners,
        session_store=discovered_extensions.session_store,
        checkpointer=discovered_extensions.checkpointer,
        asset_store=discovered_extensions.asset_store,
        asset_stores=discovered_extensions.asset_stores,
        custom_stores=discovered_extensions.storage_registry.custom,
        credential_provider=discovered_extensions.credential_provider,
        http_routes=discovered_extensions.http_routes,
        output_policy=discovered_extensions.output_policy,
        telemetry_exporters=discovered_extensions.telemetry_exporters,
        checkpoint_metadata=checkpoint_metadata(discovered_extensions.checkpointer),
        context_values=discovered_extensions.context_values,
        skill_registry=skill_registry,
        tasks=tasks,
        crons=crons,
        plugins=activated_plugins,
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
    runtime_plugins: Sequence[RuntimePluginDescriptor],
    activated_plugins: Sequence[ActivatedPlugin],
) -> CompiledApplication:
    """Compose portable resources before lowering once to the selected framework."""

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
    discovered_extensions = _load_extensions(
        anchor.parent, framework, runtime_plugins
    )
    _validate_plugin_extension_capabilities(
        runtime_plugins, discovered_extensions.declared_listeners
    )
    portable_extensions = discovered_extensions.listeners
    model_extension = portable_model_extension(
        portable_extensions, framework=framework
    )
    authored_native_extensions = discovered_extensions.native + (
        (model_extension,) if model_extension is not None else ()
    )
    credential_extensions = _credential_native_extensions(
        discovered_extensions.credential_provider,
        authored_native_extensions,
        framework=framework,
    )
    # Credentials bind first so every authored native callback observes the
    # same private invocation authority as tools and subagents.
    native_extensions = credential_extensions + authored_native_extensions
    provider = discovered_extensions.checkpointer
    # Managed compilation owns framework adaptation. Native wrappers would
    # create a second authority outside that portable ownership boundary.
    if not isinstance(provider, HarnestStore):
        raise BundleConventionError(
            "managed agents require a Harnest checkpoint store; native "
            "LangGraphStore and ADKStore are advanced-mode ownership wrappers"
        )
    native_checkpointer = _managed_checkpointer(provider, framework)
    skill_scopes: dict[str, SkillScope] = {}
    if isinstance(value, AgentDefinition):
        definition = _compose_definition(
            anchor,
            value,
            framework=framework,
            runtime_plugins=runtime_plugins,
            skill_sources=discovered_extensions.skill_sources,
            skill_scopes=skill_scopes,
        )
        sandbox_definition = definition
        target = backend.lower_managed(
            definition,
            native_extensions=native_extensions,
            checkpointer=native_checkpointer,
        )
    else:
        graph = _resolve_graph_resources(
            anchor,
            value,
            framework=framework,
            runtime_plugins=runtime_plugins,
            skill_sources=discovered_extensions.skill_sources,
            skill_scopes=skill_scopes,
        )
        sandbox_definition = graph
        target = backend.lower_managed(
            graph,
            native_extensions=native_extensions,
            checkpointer=native_checkpointer,
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
    application_name = getattr(target, "name", value.name)
    tasks, crons = _discover_tasks_and_crons(anchor.parent, application_name)
    from .context_sandboxes import SandboxRegistry

    # Named sandboxes are runtime capabilities, not additions to model schemas.
    sandbox_registry = SandboxRegistry.from_definition(sandbox_definition, framework)
    return CompiledApplication(
        name=application_name,
        framework=framework,
        mode=mode,
        target=target,
        native_app=native_app,
        kind="agent" if isinstance(value, AgentDefinition) else "graph",
        extensions=portable_extensions,
        session_store=discovered_extensions.session_store,
        checkpointer=provider,
        asset_store=discovered_extensions.asset_store,
        asset_stores=discovered_extensions.asset_stores,
        custom_stores=discovered_extensions.storage_registry.custom,
        credential_provider=discovered_extensions.credential_provider,
        http_routes=discovered_extensions.http_routes,
        output_policy=discovered_extensions.output_policy,
        telemetry_exporters=discovered_extensions.telemetry_exporters,
        input_schema=(
            definition.input_schema if isinstance(value, AgentDefinition) else None
        ),
        output_schema=(
            definition.output_schema
            if isinstance(value, AgentDefinition)
            else graph.output_schema
        ),
        checkpoint_metadata=checkpoint_metadata(provider),
        context_values=discovered_extensions.context_values,
        skill_registry=SkillRegistry(skill_scopes),
        sandbox_registry=sandbox_registry,
        tasks=tasks,
        crons=crons,
        plugins=activated_plugins,
        harnest_version=compatibility.harnest_version,
        framework_distribution=compatibility.distribution,
        framework_version=compatibility.distribution_version,
    )


def _advanced_skill_registry(
    agent_name: str, sources: Mapping[str, SkillSource]
) -> SkillRegistry:
    """Expose dynamic sources to advanced code without mutating its native tools."""

    try:
        return SkillRegistry({agent_name: SkillScope(sources)})
    except (TypeError, ValueError, SkillValidationError) as exc:
        raise BundleSkillError(str(exc)) from exc


def _managed_checkpointer(provider: HarnestStore, framework: str) -> Any:
    """Adapt Harnest storage only when the framework owns checkpoint calls."""

    # ADK resumability persists through its runtime services, while LangGraph
    # requires its checkpoint protocol to be installed during graph compilation.
    if framework == "adk":
        return None
    from .checkpoint_langgraph import HarnestCheckpointSaver

    return HarnestCheckpointSaver(provider)


def _validate_advanced_checkpointer(
    advanced: Any,
    provider: CheckpointAuthority,
    *,
    framework: str,
) -> None:
    """Enforce one visible checkpoint authority for each advanced framework."""

    if framework == "langgraph":
        _validate_advanced_langgraph_checkpointer(advanced.target, provider)
        return
    if not isinstance(provider, (HarnestStore, ADKStore)):
        raise BundleConventionError(
            "advanced ADK agents require HarnestStore or ADKStore checkpoint ownership"
        )
    if advanced.native_app is None:
        return
    config = getattr(advanced.native_app, "resumability_config", None)
    if not bool(getattr(config, "is_resumable", False)):
        raise BundleConventionError(
            "advanced ADK App must enable ResumabilityConfig(is_resumable=True)"
        )


def _ensure_adk_resumability(advanced: Any, *, framework: str) -> Any:
    """Enable the ADK capability required by the checkpoint contract."""

    if framework != "adk" or advanced.native_app is None:
        return advanced
    config = getattr(advanced.native_app, "resumability_config", None)
    if bool(getattr(config, "is_resumable", False)):
        return advanced
    from google.adk.apps.app import ResumabilityConfig
    from ._adk_warnings import suppress_adk_warnings

    with suppress_adk_warnings("resumability"):
        native_app = advanced.native_app.model_copy(
            update={"resumability_config": ResumabilityConfig(is_resumable=True)}
        )
    return replace(advanced, native_app=native_app)


def _validate_advanced_langgraph_checkpointer(
    target: Any, provider: CheckpointAuthority
) -> None:
    """Match lifecycle ownership to the checkpointer compiled into the graph."""

    native = getattr(target, "checkpointer", None)
    if isinstance(provider, LangGraphStore):
        if native is not provider:
            raise BundleConventionError(
                "advanced LangGraph target must compile with the same LangGraphStore "
                "returned by @lifecycle.checkpointer"
            )
        return
    if not isinstance(provider, HarnestStore):
        raise BundleConventionError(
            "advanced LangGraph agents require HarnestStore or LangGraphStore"
        )
    if getattr(native, "store", None) is not provider:
        raise BundleConventionError(
            "advanced LangGraph target must compile with "
            "store.as_langgraph_checkpointer() when Harnest owns checkpointing"
        )


def _load_extensions(
    bundle_root: Path,
    framework: str,
    runtime_plugins: Sequence[RuntimePluginDescriptor] = (),
) -> Any:
    """Compile explicit lifecycle roots with format-specific capability provenance."""

    directory = lifecycle_directory(bundle_root)
    sources = (
        ExtensionSource(directory, f"root/{directory.name}"),
        *(
            ExtensionSource(
                plugin.lifecycle_directory,
                plugin.lifecycle_origin,
            )
            for plugin in runtime_plugins
        ),
    )
    try:
        return discover_extension_sources(sources, framework=framework)
    except ExtensionDiscoveryError as exc:
        raise BundleConventionError(str(exc)) from exc


def _attach_advanced_native_extensions(
    advanced: Any,
    native_extensions: Sequence[Any],
    *,
    framework: str,
    prepend_extensions: Sequence[Any] = (),
) -> Any:
    """Attach ADK plugins only where native construction remains mutable."""

    if not native_extensions and not prepend_extensions:
        return advanced
    if framework == "langgraph":
        raise BundleConventionError(
            "advanced LangGraph applications cannot attach discovered middleware "
            "after graph compilation; apply it before Agent.advanced(...)"
        )
    app = advanced.native_app
    if app is None:
        raise BundleConventionError("advanced ADK lifecycle plugins require an ADK App")
    # Harnest-owned security binders must run before plugins already attached
    # to an advanced App; ordinary discovered plugins retain append semantics.
    plugins = (
        tuple(prepend_extensions)
        + tuple(getattr(app, "plugins", ()))
        + tuple(native_extensions)
    )
    names = [getattr(plugin, "name", None) for plugin in plugins]
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    if duplicates:
        raise BundleConventionError(
            "duplicate ADK application plugin names: " + ", ".join(duplicates)
        )
    native_app = app.model_copy(update={"plugins": list(plugins)})
    return replace(advanced, native_app=native_app)


def _credential_native_extensions(
    provider: Any,
    native_extensions: Sequence[Any],
    *,
    framework: str,
) -> tuple[Any, ...]:
    """Bind private credentials across ADK's neutral and native run surfaces."""

    if provider is None or framework != "adk":
        return ()
    from .credentials_adk import adk_credential_plugin

    plugin = adk_credential_plugin(provider)
    if any(
        getattr(candidate, "name", None) == plugin.name
        for candidate in native_extensions
    ):
        raise BundleConventionError(
            f"ADK plugin name {plugin.name!r} is reserved by Harnest credentials"
        )
    return (plugin,)


def compile_artifact(
    source: str | Path,
    output: str | Path,
    *,
    entrypoint: str = "agent:root_agent",
    framework: str = "adk",
    mode: str = "managed",
    cli_enabled: bool = False,
) -> dict[str, Any]:
    """Validate server policy before authored code and atomically build an artifact."""

    if not isinstance(cli_enabled, bool):
        raise TypeError("cli_enabled must be a boolean")

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

    # Reject conflicting or invalid server settings before importing authored Python.
    server_contents = project_server_config_yaml(source_directory)
    built = compile_application(
        anchor, entrypoint=entrypoint, framework=framework, mode=mode
    )
    try:
        output_directory.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{output_directory.name}.tmp-",
                dir=output_directory.parent,
            )
        )
        try:
            _copy_agent_source(source_directory, staging / "source")
            # Runtime policy stays mutable beside the launcher; source remains hashed.
            (staging / SERVER_CONFIG_FILENAME).write_text(
                server_contents, encoding="utf-8"
            )
            _write_artifact_loader(staging, entrypoint, framework, mode, source_directory)
            file_records = _artifact_file_records(staging)
            plugin_records = _compiled_plugin_records(built.plugins)
            task_records = _compiled_task_records(built.tasks)
            cron_records = _compiled_cron_records(built.crons)
            runtime_dependencies = _runtime_dependencies(task_records, cron_records)
            interfaces = {"cli": cli_enabled}
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
                "interfaces": interfaces,
                "checkpoint": dict(built.checkpoint_metadata or {}),
                "plugins": plugin_records,
                "tasks": task_records,
                "crons": cron_records,
                "runtimeDependencies": runtime_dependencies,
                "digest": _artifact_digest(
                    file_records,
                    plugin_records,
                    task_records,
                    cron_records,
                    runtime_dependencies,
                    interfaces=interfaces,
                ),
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
    finally:
        _release_compiled_plugins(built.plugins)


def _compiled_plugin_records(
    plugins: Sequence[ActivatedPlugin],
) -> list[dict[str, Any]]:
    """Serialize dependency-ordered plugin provenance without executable state."""

    return [
        {
            "name": item.descriptor.name,
            "version": item.descriptor.version,
            "digest": item.descriptor.digest,
            "requires": list(item.descriptor.requires),
            "capabilities": list(item.descriptor.capabilities),
            "dependencies": list(item.descriptor.dependencies),
        }
        for item in plugins
    ]


def _compiled_task_records(tasks: Sequence[CompiledTask]) -> list[dict[str, Any]]:
    """Serialize queue policy without executable functions or customer data."""

    return [
        {
            "name": item.name,
            "source": item.source,
            "queue": item.queue,
            "maxRetries": item.max_retries,
        }
        for item in tasks
    ]


def _compiled_cron_records(crons: Sequence[CompiledCron]) -> list[dict[str, Any]]:
    """Serialize schedules without exposing their private static arguments."""

    return [
        {
            "name": item.name,
            "source": item.source,
            "schedule": item.schedule,
            "timezone": item.timezone,
            "task": item.task_name,
        }
        for item in crons
    ]


def _runtime_dependencies(
    tasks: Sequence[dict[str, Any]], crons: Sequence[dict[str, Any]]
) -> list[str]:
    """Select the pinned queue backend only for authored durable work."""

    return [_PROCRASTINATE_REQUIREMENT] if tasks or crons else []


def _release_compiled_plugins(plugins: Sequence[ActivatedPlugin]) -> None:
    """Release the compiler-owned acquisition when no runtime will adopt it."""

    release_runtime_plugins(tuple(item.descriptor for item in plugins))


def _copy_agent_source(source: Path, destination: Path) -> None:
    """Copy authored source and executable bits, excluding generated state."""

    destination.mkdir()
    paths = sorted(
        source.rglob("*"),
        key=lambda item: item.relative_to(source).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(source)
        # Managed environments are neither authored source nor capabilities;
        # skip their entire subtree before inspecting ordinary venv symlinks.
        if ignored_source_path(relative):
            continue
        if path.is_symlink():
            raise BundleConventionError(
                f"agent source copied into an artifact cannot contain symlinks: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleConventionError(f"unsupported agent source resource: {path}")
        if path.suffix in {".pyc", ".pyo"}:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        # Package-local MCP executables must remain runnable after compilation,
        # but source setuid/setgid and sticky bits must never enter the artifact.
        target.chmod((target.stat().st_mode & 0o666) | (path.stat().st_mode & 0o111))


def _write_artifact_loader(
    directory: Path, source_entrypoint: str, framework: str, mode: str,
    source_directory: Path | None = None,
) -> None:
    """Carry stable installation ownership across fresh immutable source generations."""
    from .agent_plugin_runtime import installation_id
    scope = installation_id(source_directory or directory)
    (directory / "agent.py").write_text(
        '"""Generated Harnest application entrypoint; do not edit."""\n\n'
        "from pathlib import Path\n\n"
        "from harnest import compile_application\n\n"
        "from harnest.agent_plugin_runtime import plugin_installation\n\n"
        f"with plugin_installation({scope!r}):\n"
        "    application = compile_application(\n"
        '        Path(__file__).parent / "source",\n'
        f"        entrypoint={source_entrypoint!r},\n"
        f"        framework={framework!r},\n"
        f"        mode={mode!r},\n"
        "    )\n"
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
        '"""Run this compiled agent with ``python <artifact> serve|run``."""\n\n'
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
    """Record deterministic file identity for artifact verification."""

    records = []
    paths = sorted(
        directory.rglob("*"),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    for path in paths:
        if not path.is_file():
            continue
        if path == directory / SERVER_CONFIG_FILENAME:
            # Deployment may replace server.yaml without changing compiled
            # agent identity; authored policy remains hashed under source/.
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


def _artifact_digest(
    records: Sequence[dict[str, Any]],
    plugins: Sequence[dict[str, Any]] = (),
    tasks: Sequence[dict[str, Any]] = (),
    crons: Sequence[dict[str, Any]] = (),
    runtime_dependencies: Sequence[str] = (),
    *,
    interfaces: Mapping[str, bool],
) -> str:
    """Bind files, capabilities, and explicit interface policy into one identity."""

    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\n")
    _update_artifact_digest_field(
        digest,
        "interface.cli",
        str(interfaces["cli"]).lower(),
    )
    for plugin in plugins:
        # Plugin records live in the manifest rather than the file set, so bind
        # their canonical compiler output explicitly to prevent provenance drift.
        _update_artifact_digest_field(digest, "plugin.name", plugin["name"])
        _update_artifact_digest_field(digest, "plugin.version", plugin["version"])
        _update_artifact_digest_field(digest, "plugin.digest", plugin["digest"])
        requires = plugin["requires"]
        _update_artifact_digest_field(digest, "plugin.requires", str(len(requires)))
        for dependency in requires:
            _update_artifact_digest_field(digest, "plugin.require", dependency)
        capabilities = plugin["capabilities"]
        _update_artifact_digest_field(
            digest, "plugin.capabilities", str(len(capabilities))
        )
        for capability in capabilities:
            _update_artifact_digest_field(digest, "plugin.capability", capability)
        dependencies = plugin["dependencies"]
        _update_artifact_digest_field(
            digest, "plugin.dependencies", str(len(dependencies))
        )
        for requirement in dependencies:
            _update_artifact_digest_field(
                digest, "plugin.dependency", requirement
            )
    for task_record in tasks:
        _update_artifact_digest_field(digest, "task.name", task_record["name"])
        _update_artifact_digest_field(digest, "task.source", task_record["source"])
        _update_artifact_digest_field(digest, "task.queue", task_record["queue"])
        _update_artifact_digest_field(
            digest, "task.max_retries", str(task_record["maxRetries"])
        )
    for cron_record in crons:
        _update_artifact_digest_field(digest, "cron.name", cron_record["name"])
        _update_artifact_digest_field(digest, "cron.source", cron_record["source"])
        _update_artifact_digest_field(
            digest, "cron.schedule", cron_record["schedule"]
        )
        _update_artifact_digest_field(
            digest, "cron.timezone", cron_record["timezone"]
        )
        _update_artifact_digest_field(digest, "cron.task", cron_record["task"])
    for requirement in runtime_dependencies:
        _update_artifact_digest_field(digest, "runtime.dependency", requirement)
    return f"sha256:{digest.hexdigest()}"


def _update_artifact_digest_field(digest: Any, label: str, value: str) -> None:
    """Frame one provenance field identically across Python and Go."""

    encoded = value.encode("utf-8")
    digest.update(label.encode("ascii"))
    digest.update(b"\0")
    digest.update(str(len(encoded)).encode("ascii"))
    digest.update(b":")
    digest.update(encoded)
    digest.update(b"\n")


def _replace_artifact(staging: Path, output: Path) -> None:
    """Publish a complete staged artifact without leaving partial output."""

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
    with _bundle_library(validated_anchor.parent), independent_sandbox_catalog():
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
        "sandbox",
        "skills",
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


def _reject_advanced_plugin_resources(
    bundle_root: Path,
    runtime_plugins: Sequence[RuntimePluginDescriptor],
    framework: str,
) -> None:
    """Allow only Harnest-owned runtime boundaries in advanced mode."""

    agent_plugins = _discover_capability_plugins(bundle_root / "plugins")
    if agent_plugins:
        raise BundleConventionError(
            f"advanced {framework} mode does not compose agent-plugins"
        )
    for plugin in runtime_plugins:
        populated = tuple(
            directory
            for directory in _RUNTIME_PLUGIN_CONTENT_CAPABILITIES
            if _has_public_entries(
                plugin.directory / directory,
                kind=f"runtime plugin {plugin.name} {directory}",
            )
        )
        if populated:
            raise BundleConventionError(
                f"advanced {framework} mode cannot auto-compose runtime plugin "
                f"{plugin.name!r} content: " + ", ".join(populated)
            )


def _reject_advanced_plugin_files(bundle_root: Path, framework: str) -> None:
    """Preserve the advanced-mode diagnostic for legacy flat plugin files."""

    directory = bundle_root / "plugins"
    if directory.is_symlink():
        raise BundleConventionError(
            f"plugins directory cannot be a symlink: {directory}"
        )
    if not directory.exists():
        return
    if not directory.is_dir():
        raise BundleConventionError(f"plugins path must be a directory: {directory}")
    for path in directory.iterdir():
        if path.is_symlink():
            raise BundleConventionError(
                f"plugin resource cannot be a symlink: {path}"
            )
        if _is_ignored(path) or path.is_dir():
            continue
        raise BundleConventionError(
            f"advanced {framework} mode does not compose filesystem resources from: "
            "plugins/; wire them into Agent.advanced(...) in agent.py or remove them"
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


def _with_sandbox_catalog(function: Callable[..., Any]) -> Callable[..., Any]:
    """Establish the registry before recursively composing agents or graph nodes."""
    @wraps(function)
    def compose(anchor: Path, *args: Any, **kwargs: Any) -> Any:
        """Restore compiler scope even when declaration loading or assignment fails."""
        try:
            with sandbox_catalog_scope(anchor.parent, _discover_sandboxes):
                return function(anchor, *args, **kwargs)
        except SandboxCatalogError as error:
            raise BundleConventionError(str(error)) from None
    return compose


@_with_sandbox_catalog
def _resolve_graph_resources(
    anchor: Path,
    graph: Graph,
    *,
    framework: str,
    runtime_plugins: Sequence[RuntimePluginDescriptor] = (),
    skill_sources: Mapping[str, SkillSource] | None = None,
    skill_scopes: dict[str, SkillScope] | None = None,
) -> Graph:
    """Resolve string graph nodes from filesystem-discovered tools/subagents."""

    resources = _discover_folder_resources(
        anchor.parent,
        framework,
        is_root=True,
        capability_scope="",
        runtime_plugins=runtime_plugins,
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )
    has_root_agent_node = _graph_contains_agent(graph)
    referenced = _graph_string_references(graph)
    registry, resource_kinds = _graph_resource_registry(resources)
    _validate_graph_resource_consumption(
        graph, resources, resource_kinds, referenced, has_root_agent_node, framework
    )
    return replace(
        graph,
        nodes={
            name: _resolve_graph_node(
                value,
                graph.name,
                registry,
                anchor,
                framework,
                runtime_plugins,
                skill_sources,
                skill_scopes,
            )
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
    """Fail when graph structure would leave discovered capabilities unused."""

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
    else:
        # Registry entries are definitions, not enabled capabilities. A graph
        # may deliberately leave some or all sandbox declarations unassigned.
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
    runtime_plugins: Sequence[RuntimePluginDescriptor] = (),
    skill_sources: Mapping[str, SkillSource] | None = None,
    skill_scopes: dict[str, SkillScope] | None = None,
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
                name: _resolve_graph_node(
                    node,
                    graph_name,
                    registry,
                    anchor,
                    framework,
                    runtime_plugins,
                    skill_sources,
                    skill_scopes,
                )
                for name, node in value.nodes.items()
            },
        )
    if isinstance(value, AgentDefinition):
        return _compose_definition(
            anchor,
            value,
            framework=framework,
            graph_resource_context=True,
            runtime_plugins=runtime_plugins,
            skill_sources=skill_sources,
            skill_scopes=skill_scopes,
        )
    return value


@_with_sandbox_catalog
def _compose_definition(
    anchor: Path,
    root: AgentDefinition,
    *,
    is_root: bool = True,
    framework: str = "adk",
    graph_resource_context: bool = False,
    capability_scope: str = "",
    runtime_plugins: Sequence[RuntimePluginDescriptor] = (),
    skill_sources: Mapping[str, SkillSource] | None = None,
    skill_scopes: dict[str, SkillScope] | None = None,
) -> AgentDefinition:
    """Attach only resources owned by this agent's filesystem scope."""

    bundle_root = anchor.parent
    _validate_folder_scope(bundle_root, is_root)
    instruction = _resolve_instruction(bundle_root, root.instruction)
    resources = _discover_folder_resources(
        bundle_root,
        framework,
        is_root=is_root,
        capability_scope=capability_scope,
        runtime_plugins=runtime_plugins if is_root else (),
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )
    tools = _merge_named_sources(
        "tool",
        ("explicit", root.tools),
        ("discovered", resources.tools),
        identity=_tool_name,
    )
    discovered_subagents = _agent_subagents_for_framework(
        resources.subagents, framework, graph_resource_context
    )
    explicit_subagents = tuple(
        _compose_explicit_subagent_skills(
            value,
            skill_sources=skill_sources,
            skill_scopes=skill_scopes,
        )
        for value in root.subagents
    )
    subagents = _merge_named_sources(
        "subagent",
        ("explicit", explicit_subagents),
        ("discovered", discovered_subagents),
        identity=_subagent_name,
    )
    mcp = _merge_mcp_sources(
        ("explicit", root.mcp),
        ("discovered", resources.mcp),
    )
    tools = _attach_skill_tools(
        resources.skill_directories,
        tools,
        agent_name=root.name,
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )
    return replace(
        root,
        instruction=instruction,
        tools=tools,
        subagents=subagents,
        mcp=mcp,
        _sandbox_bindings=resolve_sandbox_assignments(root.name, root.sandboxes),
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
        ("lifecycle", "application lifecycle"),
        ("lib", "authored libraries"),
        ("models", "authored models"),
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
    bundle_root: Path,
    framework: str,
    *,
    is_root: bool,
    capability_scope: str,
    runtime_plugins: Sequence[RuntimePluginDescriptor] = (),
    skill_sources: Mapping[str, SkillSource] | None = None,
    skill_scopes: dict[str, SkillScope] | None = None,
) -> _DiscoveredResources:
    tools = _merge_named_sources(
        "tool",
        ("root", _discover_tools(bundle_root / "tools")),
        *(
            (
                f"runtime plugin {plugin.name!r}",
                _discover_tools(plugin.directory / "tools"),
            )
            for plugin in runtime_plugins
        ),
        identity=_tool_name,
    )
    subagents = _merge_named_sources(
        "subagent",
        (
            "root",
            _discover_subagents(
                bundle_root / "subagents",
                framework=framework,
                capability_scope=capability_scope,
                skill_sources=skill_sources,
                skill_scopes=skill_scopes,
            ),
        ),
        *(
            (
                f"runtime plugin {plugin.name!r}",
                _discover_subagents(
                    plugin.directory / "subagents",
                    framework=framework,
                    capability_scope=f"plugin__{plugin.name}",
                    skill_sources=skill_sources,
                    skill_scopes=skill_scopes,
                ),
            )
            for plugin in runtime_plugins
        ),
        identity=_subagent_name,
    )
    discovered_mcp = _discover_mcp(
        bundle_root / "mcp", capability_scope=_mcp_scope(capability_scope)
    )
    plugins = _discover_capability_plugins(bundle_root / "plugins") if is_root else ()
    plugin_mcp = _discover_plugin_mcp(plugins)
    runtime_plugin_mcp = tuple(
        (
            plugin,
            _discover_mcp(
                plugin.directory / "mcp",
                capability_scope=f"plugin__{plugin.name}__mcp",
            ),
        )
        for plugin in runtime_plugins
    )
    mcp = _merge_mcp_sources(
        ("discovered", discovered_mcp),
        *((f"plugin {plugin.name!r}", clients) for plugin, clients in plugin_mcp),
        *(
            (f"runtime plugin {plugin.name!r}", clients)
            for plugin, clients in runtime_plugin_mcp
        ),
    )
    sandboxes = current_sandbox_catalog()
    skill_directories = _merge_skill_directories(
        _bundle_skill_directories(bundle_root, plugins),
        *(
            _skill_directories(plugin.directory / "skills")
            for plugin in runtime_plugins
        ),
    )
    _validate_framework_evals(bundle_root / "evals", framework)
    return _DiscoveredResources(
        tools=tools,
        subagents=subagents,
        mcp=mcp,
        sandboxes=sandboxes,
        skill_directories=skill_directories,
    )


def _validate_framework_evals(directory: Path, framework: str) -> None:
    """Validate the shared eval-set contract before either backend is lowered."""

    if framework in {"adk", "langgraph"}:
        _discover_evals(directory)


def _agent_subagents_for_framework(
    discovered: tuple[AgentDefinition | _AdvancedAgentDefinition, ...],
    framework: str,
    graph_resource_context: bool,
) -> tuple[AgentDefinition | _AdvancedAgentDefinition, ...]:
    """Return filesystem subagents consumable by the selected framework shape."""

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
    skill_directories: Sequence[Path],
    tools: tuple[Any, ...],
    *,
    agent_name: str,
    skill_sources: Mapping[str, SkillSource] | None,
    skill_scopes: dict[str, SkillScope] | None,
) -> tuple[Any, ...]:
    """Attach one shared progressive tool surface regardless of framework."""

    scope = _register_skill_scope(
        agent_name,
        skill_directories,
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )
    if not scope.source_names:
        return tools
    conflicts = sorted({_tool_name(item) for item in tools} & _SKILL_TOOL_NAMES)
    if conflicts:
        raise BundleDuplicateError(
            "tools conflict with Harnest skill tools: "
            + ", ".join(repr(name) for name in conflicts)
        )
    return (*tools, *create_skill_tools(scope))


def _register_skill_scope(
    agent_name: str,
    skill_directories: Sequence[Path],
    *,
    skill_sources: Mapping[str, SkillSource] | None,
    skill_scopes: dict[str, SkillScope] | None,
) -> SkillScope:
    """Build one scope through SkillSource and retain it for `context.skills`."""

    try:
        filesystem = (
            FilesystemSkillSource(skill_directories)
            if skill_directories
            else None
        )
        scope = scoped_skill_sources(skill_sources or {}, filesystem)
    except (TypeError, ValueError, SkillValidationError) as exc:
        raise BundleSkillError(str(exc)) from exc
    if skill_scopes is None:
        return scope
    existing = skill_scopes.get(agent_name)
    if existing is not None:
        if existing.routing_identity == scope.routing_identity:
            return existing
        raise BundleDuplicateError(f"duplicate skill scope for agent {agent_name!r}")
    skill_scopes[agent_name] = scope
    return scope


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
    """Load strict filename-matched tools in deterministic filesystem order."""

    tools = []
    for path in _resource_files(directory, kind="tools"):
        name = path.stem
        module, value = _load_export(path, name)
        if not callable(value) or not getattr(value, "__harnest_tool__", False):
            raise BundleExportError(
                f"tool module {path} must export @tool or @client_tool callable "
                f"{name!r}"
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


def _discover_tasks(directory: Path, application_name: str) -> tuple[CompiledTask, ...]:
    """Load strict task exports and assign artifact-stable native names."""

    tasks = []
    for path in _resource_files(directory, kind="tasks"):
        export_name = path.stem
        module, value = _load_export(path, export_name)
        definition = task_registration_for(value)
        if definition is None:
            raise BundleExportError(
                f"task module {path} must export @task callable {export_name!r}"
            )
        if getattr(value, "__name__", None) != export_name:
            raise BundleExportError(
                f"task exported by {path} must be named {export_name!r}; "
                f"got {getattr(value, '__name__', None)!r}"
            )
        _reject_extra_exports(
            module,
            path,
            export_name,
            kind="task",
            predicate=lambda item: task_registration_for(item) is not None,
        )
        stable_name = f"harnest.{application_name}.tasks.{export_name}"
        tasks.append(
            CompiledTask(
                name=stable_name,
                source=f"tasks/{path.name}",
                definition=definition,
                authored=value,
            )
        )
    return tuple(tasks)


def _discover_tasks_and_crons(
    bundle_root: Path, application_name: str
) -> tuple[tuple[CompiledTask, ...], tuple[CompiledCron, ...]]:
    """Resolve tasks first so cron imports bind to compiler-owned callables."""

    tasks = _discover_tasks(bundle_root / "tasks", application_name)
    with _compiled_task_imports(bundle_root / "tasks", tasks):
        crons = _discover_crons(
            bundle_root / "cron", bundle_root, application_name, tasks
        )
    return tasks, crons


def _discover_crons(
    directory: Path,
    bundle_root: Path,
    application_name: str,
    tasks: Sequence[CompiledTask],
) -> tuple[CompiledCron, ...]:
    """Load filename-matched schedules and resolve only discovered task targets."""

    crons = []
    for path in _resource_files(directory, kind="cron"):
        export_name = path.stem
        module, value = _load_export(path, export_name)
        if not isinstance(value, Cron):
            raise BundleExportError(
                f"cron module {path} must export Cron {export_name!r}"
            )
        _reject_extra_exports(
            module,
            path,
            export_name,
            kind="cron",
            predicate=lambda item: isinstance(item, Cron),
        )
        target = _resolve_cron_task(value.task, tasks, bundle_root)
        crons.append(
            CompiledCron(
                name=f"harnest.{application_name}.cron.{export_name}",
                source=f"cron/{path.name}",
                schedule=value.schedule,
                timezone=value.timezone,
                task=target,
                arguments=value.arguments,
            )
        )
    return tuple(crons)


def _resolve_cron_task(
    value: Any, tasks: Sequence[CompiledTask], bundle_root: Path
) -> CompiledTask:
    """Match identity first and source provenance second across Python imports."""

    exact = [item for item in tasks if item.authored is value]
    if len(exact) == 1:
        return exact[0]
    definition = task_registration_for(value)
    source = _callable_source(definition.function) if definition is not None else None
    matches = [
        item
        for item in tasks
        if source == (bundle_root / item.source).resolve()
        and getattr(value, "__name__", None) == Path(item.source).stem
    ]
    if len(matches) == 1:
        return matches[0]
    raise BundleExportError(
        "cron task must reference exactly one callable exported from the root tasks/ "
        "folder"
    )


def _callable_source(function: Any) -> Path | None:
    """Resolve task provenance without trusting an authored module name."""

    try:
        source = inspect.getsourcefile(function)
    except (TypeError, OSError):
        return None
    return None if source is None else Path(source).resolve()


@contextmanager
def _compiled_task_imports(
    directory: Path, tasks: Sequence[CompiledTask]
) -> Iterator[None]:
    """Expose a bounded ``tasks.<name>`` namespace while loading cron files."""

    if not tasks:
        yield
        return
    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == "tasks" or name.startswith("tasks.")
    }
    package = ModuleType("tasks")
    package.__package__ = "tasks"
    package.__path__ = [str(directory)]
    package.__spec__ = ModuleSpec("tasks", loader=None, is_package=True)
    sys.modules["tasks"] = package
    for compiled in tasks:
        export_name = Path(compiled.source).stem
        module_name = f"tasks.{export_name}"
        module = ModuleType(module_name)
        module.__package__ = "tasks"
        module.__file__ = str((directory / f"{export_name}.py").resolve())
        setattr(module, export_name, compiled.authored)
        setattr(package, export_name, module)
        sys.modules[module_name] = module
    try:
        yield
    finally:
        # The namespace is compiler scaffolding, not a process-global user import.
        for name in tuple(sys.modules):
            if name == "tasks" or name.startswith("tasks."):
                sys.modules.pop(name, None)
        sys.modules.update(previous)


def _discover_subagents(
    directory: Path,
    *,
    framework: str = "adk",
    capability_scope: str = "",
    skill_sources: Mapping[str, SkillSource] | None = None,
    skill_scopes: dict[str, SkillScope] | None = None,
) -> tuple[AgentDefinition | _AdvancedAgentDefinition, ...]:
    """Discover managed and explicitly advanced filesystem subagents."""

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
    return _load_subagent_entries(
        entries,
        framework=framework,
        capability_scope=capability_scope,
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )


def _subagent_entry(path: Path) -> tuple[str, Path, bool] | None:
    """Classify a subagent entry and explain how to repair misplaced files."""
    if path.is_symlink():
        raise BundleConventionError(f"subagent resource cannot be a symlink: {path}")
    if _is_ignored(path):
        return None
    if path.is_file():
        if path.suffix != ".py":
            raise BundleConventionError(
                folder_entry_error(
                    f"unexpected file in subagents directory: {path}", path, kind="subagents"
                )
            )
        _validate_resource_name(path.stem, path)
        return path.stem, path, False
    if path.is_dir():
        return _nested_subagent_entry(path)
    raise BundleConventionError(f"unsupported subagent resource: {path}")


def _nested_subagent_entry(path: Path) -> tuple[str, Path, bool]:
    """Resolve flat versus folder-owned subagents without guessing ownership."""

    _validate_resource_name(path.name, path)
    nested_anchor = path / "agent.py"
    if nested_anchor.is_symlink():
        raise BundleConventionError(
            f"nested subagent agent.py cannot be a symlink: {nested_anchor}"
        )
    if not nested_anchor.is_file():
        raise BundleConventionError(
            authoring_guidance(
                f"nested subagent directory must contain agent.py: {path}",
                expected="each active subagent folder has its own agent.py entry file",
                fix=f"Create {nested_anchor} and define the subagent there. " + inactive_entry_hint(path),
            )
        )
    return path.name, nested_anchor, True


def _load_subagent_entries(
    entries: Sequence[tuple[str, Path, bool]],
    *,
    framework: str,
    capability_scope: str,
    skill_sources: Mapping[str, SkillSource] | None = None,
    skill_scopes: dict[str, SkillScope] | None = None,
) -> tuple[AgentDefinition | _AdvancedAgentDefinition, ...]:
    """Load, validate, and compose ordered filesystem subagent exports."""

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
        if not isinstance(value, (AgentDefinition, _AdvancedAgentDefinition)):
            raise BundleExportError(
                f"subagent module {path} must export Agent or Agent.advanced "
                f"as {export_name!r}; "
                f"got {type(value).__name__}"
            )
        if _subagent_name(value) != export_name:
            raise BundleExportError(
                f"subagent exported by {path} must have name {export_name!r}; "
                f"got {_subagent_name(value)!r}"
            )
        _reject_extra_exports(
            module,
            path,
            export_name,
            kind="subagent",
            predicate=lambda item: isinstance(
                item, (AgentDefinition, _AdvancedAgentDefinition)
            ),
        )
        if (
            isinstance(value, AgentDefinition)
            and not nested
            and value.instruction is None
        ):
            raise BundleExportError(
                f"flat subagent {path} must provide instruction explicitly; "
                "use a nested subagent folder to load instructions.md"
            )
        agents.append(
            _compose_subagent_entry(
                path,
                value,
                nested=nested,
                framework=framework,
                capability_scope=_agent_scope(capability_scope, export_name),
                export_name=export_name,
                skill_sources=skill_sources,
                skill_scopes=skill_scopes,
            )
        )
    return tuple(agents)


def _compose_subagent_entry(
    path: Path,
    value: AgentDefinition | _AdvancedAgentDefinition,
    *,
    nested: bool,
    framework: str,
    capability_scope: str,
    export_name: str,
    skill_sources: Mapping[str, SkillSource] | None,
    skill_scopes: dict[str, SkillScope] | None,
) -> AgentDefinition | _AdvancedAgentDefinition:
    """Compose a nested managed export or preserve one flat export."""

    if not nested:
        return _compose_explicit_subagent_skills(
            value,
            skill_sources=skill_sources,
            skill_scopes=skill_scopes,
        )
    # A nested folder promises Harnest-owned resource composition. Advanced
    # targets own native composition, so accepting that shape would silently
    # ignore sibling instructions and capabilities.
    if isinstance(value, _AdvancedAgentDefinition):
        raise BundleExportError(
            f"nested advanced subagent {path.parent} is not supported; "
            f"export Agent.advanced(...) from subagents/{export_name}.py"
        )
    return _compose_definition(
        path,
        value,
        is_root=False,
        framework=framework,
        capability_scope=capability_scope,
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )


def _compose_explicit_subagent_skills(
    value: AgentDefinition | _AdvancedAgentDefinition | Any,
    *,
    skill_sources: Mapping[str, SkillSource] | None,
    skill_scopes: dict[str, SkillScope] | None,
) -> AgentDefinition | _AdvancedAgentDefinition | Any:
    """Give code-owned subagents dynamic sources without claiming folder content."""

    if isinstance(value, _AdvancedAgentDefinition):
        _register_skill_scope(
            _subagent_name(value),
            (),
            skill_sources=skill_sources,
            skill_scopes=skill_scopes,
        )
        return value
    if not isinstance(value, AgentDefinition):
        return value
    children = tuple(
        _compose_explicit_subagent_skills(
            child,
            skill_sources=skill_sources,
            skill_scopes=skill_scopes,
        )
        for child in value.subagents
    )
    tools = _attach_skill_tools(
        (),
        tuple(value.tools),
        agent_name=value.name,
        skill_sources=skill_sources,
        skill_scopes=skill_scopes,
    )
    return replace(
        value, tools=tools, subagents=children,
        _sandbox_bindings=resolve_sandbox_assignments(value.name, value.sandboxes),
    )


def _discover_capability_plugins(directory: Path) -> tuple[PluginResources, ...]:
    try:
        return discover_plugins(directory)
    except PluginConventionError as exc:
        raise BundleConventionError(str(exc)) from exc


def _discover_runtime_plugins(
    directory: Path,
) -> tuple[RuntimePluginDescriptor, ...]:
    """Resolve canonical and legacy extension packages before importing agent code."""

    try:
        return discover_application_extensions(directory)
    except (RuntimePluginConventionError, ApplicationLayoutError) as exc:
        raise BundleConventionError(str(exc)) from exc


def _validate_runtime_plugin_layouts(
    plugins: Sequence[RuntimePluginDescriptor],
) -> None:
    """Validate secondary content roots and their declared capability surfaces."""

    for plugin in plugins:
        _validate_runtime_plugin_entries(plugin)
        for directory, capability in _RUNTIME_PLUGIN_CONTENT_CAPABILITIES.items():
            if _has_public_entries(
                plugin.directory / directory,
                kind=f"runtime plugin {plugin.name} {directory}",
            ):
                _require_plugin_capability(plugin, capability, directory)


def _validate_runtime_plugin_project_set(
    root: Path, plugins: Sequence[RuntimePluginDescriptor]
) -> None:
    """Match direct compiler behavior to the CLI's shared dependency policy."""

    try:
        validate_runtime_plugin_dependencies(root / "pyproject.toml", plugins)
    except RuntimePluginConventionError as exc:
        raise BundleConventionError(str(exc)) from exc


def _validate_runtime_plugin_entries(plugin: RuntimePluginDescriptor) -> None:
    """Reject roots that could masquerade as a second agent application."""

    entries = _RUNTIME_PLUGIN_ENTRIES
    if plugin.entrypoint == "extension:extension":
        entries = (entries - {"plugin.yaml", "plugin.py", "extensions"}) | {
            "extension.yaml", "extension.py", "lifecycle"
        }
    for path in sorted(plugin.directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise BundleConventionError(
                f"runtime plugin resource cannot be a symlink: {path}"
            )
        if _is_ignored(path):
            continue
        if path.name not in entries:
            raise BundleConventionError(
                f"unexpected runtime plugin resource: {path}"
            )
        expected_file = path.name in {
            "plugin.yaml", "plugin.py", "extension.yaml", "extension.py", "pyproject.toml"
        }
        if expected_file != path.is_file():
            expected = "file" if expected_file else "directory"
            raise BundleConventionError(
                f"runtime plugin resource must be a {expected}: {path}"
            )


def _require_plugin_capability(
    plugin: RuntimePluginDescriptor, capability: str, source: str
) -> None:
    """Require manifest consent before compiling one privileged contribution."""

    if capability not in plugin.capabilities:
        raise BundleConventionError(
            f"runtime plugin {plugin.name!r} must declare capability "
            f"{capability!r} for {source}"
        )


def _validate_plugin_extension_capabilities(
    plugins: Sequence[RuntimePluginDescriptor], listeners: Sequence[Any]
) -> None:
    """Match every plugin listener to its manifest-declared authority."""

    by_name = {plugin.name: plugin for plugin in plugins}
    for listener in listeners:
        plugin = _listener_plugin(listener, by_name)
        if plugin is None:
            continue
        capability = _RUNTIME_PLUGIN_PHASE_CAPABILITIES.get(listener.phase)
        if capability is None:
            raise BundleConventionError(
                f"runtime plugin listener {listener.identity} uses unsupported "
                f"phase {listener.phase!r}"
            )
        _require_plugin_capability(plugin, capability, listener.identity)
        if listener.context_name is not None:
            _require_plugin_capability(
                plugin, "context.resources", listener.identity
            )


def _listener_plugin(
    listener: Any, by_name: Mapping[str, RuntimePluginDescriptor]
) -> RuntimePluginDescriptor | None:
    """Resolve compiler-owned listener provenance without trusting callbacks."""

    parts = Path(listener.relative_path).parts
    if len(parts) < 3 or parts[0] not in {"plugins", "extensions"}:
        return None
    return by_name.get(parts[1])


def _discover_plugin_mcp(
    plugins: Sequence[PluginResources],
) -> tuple[tuple[PluginResources, tuple[MCPClient, ...]], ...]:
    """Use portable declarations directly; only legacy bundles import Python factories."""

    return tuple(
        (
            plugin,
            plugin.mcp_clients if plugin.manifest is not None else _discover_mcp(
                plugin.directory / "mcp",
                capability_scope=f"plugin__{plugin.name}__mcp",
            ),
        )
        for plugin in plugins
    )


def _discover_mcp(
    directory: Path, *, capability_scope: str = "mcp"
) -> tuple[MCPClient, ...]:
    clients = []
    for path in _resource_files(directory, kind="mcp"):
        name = path.stem
        module, factory = _load_export(path, "client")
        if not callable(factory):
            raise BundleExportError(
                f"MCP module {path} must export callable client(); "
                f"got {type(factory).__name__}"
            )
        value = _call_mcp_factory(path, factory)
        if not isinstance(value, MCPClient):
            raise BundleExportError(
                f"MCP client() factory in {path} must return MCPClient; "
                f"got {type(value).__name__}"
            )
        value = replace(
            value,
            identity=name,
            capability_id=f"{capability_scope}__{name}",
            approval=approval_policy(factory),
        )
        _reject_extra_exports(
            module,
            path,
            "client",
            kind="MCP client",
            predicate=lambda item: isinstance(item, MCPClient)
            or (
                callable(item)
                and getattr(item, "__module__", None) == module.__name__
            ),
        )
        clients.append(value)
    return tuple(clients)


def _call_mcp_factory(path: Path, factory: Callable[..., Any]) -> Any:
    try:
        signature = inspect.signature(factory)
    except Exception as exc:
        # Exotic callables can execute user code while exposing a signature;
        # redact that boundary for the same reason as factory invocation.
        raise BundleImportError(
            f"MCP client() factory in {path} signature failed with "
            f"{type(exc).__name__}"
        ) from None
    if signature.parameters:
        raise BundleExportError(
            f"MCP client() factory in {path} must accept no arguments and "
            "declare zero parameters"
        )
    try:
        return factory()
    except Exception as exc:
        # Factory exceptions may carry credentials or complete headers, so
        # diagnostics identify the failing boundary without echoing values.
        raise BundleImportError(
            f"MCP client() factory in {path} failed with {type(exc).__name__}"
        ) from None


def _mcp_scope(capability_scope: str) -> str:
    return f"{capability_scope}__mcp" if capability_scope else "mcp"


def _agent_scope(capability_scope: str, name: str) -> str:
    if capability_scope.startswith("agent__"):
        return f"{capability_scope}__{name}"
    return f"agent__{name}"


def _discover_sandboxes(directory: Path) -> Mapping[str, Sandbox]:
    """Discover filename-matched declarations without constructing or granting providers."""
    from .sandbox_assignments import validate_sandbox_name

    files = _resource_files(directory, kind="sandbox")
    declarations = {}
    for path in files:
        try:
            validate_sandbox_name(path.stem)
        except ValueError as error:
            raise BundleConventionError(f"invalid sandbox name at {path}: {error}") from None
        declarations[path.stem] = _load_sandbox_declaration(path)
    return declarations


def _load_sandbox_declaration(path: Path) -> Sandbox:
    """Validate one named declaration and explain how to repair a wrong export."""
    name = path.stem
    module, value = _load_export(path, name)
    if not isinstance(value, Sandbox):
        raise BundleExportError(
            authoring_guidance(
                f"sandbox module {path} must export Sandbox {name!r}; got {type(value).__name__}",
                expected=f"a variable named {name} whose value is a Harnest Sandbox declaration",
                fix=f"Import Sandbox from harnest.sandbox, then assign {name} = Sandbox.container(image='python:3.12-slim') "
                f"or {name} = Sandbox.provider(your_factory). Add {name!r} to the sandboxes=[...] list of each agent allowed to use it.",
            )
        )
    _reject_extra_exports(
        module,
        path,
        name,
        kind="sandbox",
        predicate=lambda item: isinstance(item, Sandbox),
    )
    return value


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
    """Discover skill folders and explain misplaced top-level files."""
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
        if not path.is_dir():
            raise BundleConventionError(
                folder_entry_error(f"unexpected resource in skills directory: {path}", path, kind="skills")
            )
        skill_directories.append(path)
    return tuple(skill_directories)


def _discover_evals(directory: Path) -> EvalSuite:
    """Load evaluation files with actionable guidance for incomplete suites."""
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
                authoring_guidance(
                    f"evals directory must contain at least one *.evalset.json: {directory}",
                    expected="test_config.json configures an existing evaluation set; it is not a set of test cases",
                    fix="Add an evaluation set such as quality.evalset.json. If evaluations are not ready, "
                    "rename test_config.json to _test_config.json to leave it out of discovery.",
                )
            )
        return EvalSuite(())
    _validate_eval_files(eval_paths, config_path)
    return EvalSuite(tuple(eval_paths), config_path)


def _eval_file_kind(path: Path) -> str:
    """Classify evaluation inputs without mistaking notes or nested folders for cases."""
    if path.is_symlink():
        raise BundleConventionError(f"eval resource cannot be a symlink: {path}")
    if _is_ignored(path):
        return "ignored"
    if not path.is_file():
        raise BundleConventionError(
            folder_entry_error(
                f"unexpected resource in evals directory: {path}; evals must use a flat file layout",
                path, kind="evals",
            )
        )
    if path.name == "test_config.json":
        return "config"
    if path.name.endswith(".evalset.json"):
        return "eval"
    raise BundleConventionError(
        folder_entry_error(
            f"unexpected eval file {path}; expected *.evalset.json or test_config.json", path, kind="evals"
        )
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
    """Validate feature files consistently without silently skipping authoring mistakes."""
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
                folder_entry_error(f"unexpected resource in {kind} directory: {path}", path, kind=kind)
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
    """Explain the Python naming rule used to connect files to their declarations."""
    if not name.isidentifier():
        raise BundleConventionError(
            authoring_guidance(
                f"resource name must be a valid Python identifier: {path}",
                expected="a name Python can use for a function or variable, without spaces or hyphens and not starting with a digit",
                fix="Use a name such as search_customer.py (or search_customer for a folder), "
                "and update the matching function or variable name inside. " + inactive_entry_hint(path),
            )
        )


def _load_export(path: Path, export_name: str) -> tuple[ModuleType, Any]:
    """Explain a missing export as a missing named Python function or variable."""
    module = _load_module(path)
    if not hasattr(module, export_name):
        raise BundleExportError(
            authoring_guidance(
                f"resource module {path} must export {export_name!r}",
                expected=f"this file defines a top-level function or variable named {export_name!r}; "
                "this is what 'export' means here",
                fix=f"Name the intended declaration {export_name!r}, not just the file. "
                "For example, tools/search.py defines a tool named search; "
                "MCP files define client(), and sandbox/research.py defines research. "
                "Put reusable helper code in lib/ instead of a feature folder.",
            )
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
    """Reject competing declarations and explain how to split or hide helpers."""
    extras = sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and name != expected_name and predicate(value)
    )
    if extras:
        rendered = ", ".join(repr(name) for name in extras)
        raise BundleExportError(
            authoring_guidance(
                f"resource module {path} exports additional {kind} resources: {rendered}; use one public resource per file",
                expected=f"only the intended {kind} declaration named {expected_name!r} is discovered from this file",
                fix="Move additional active declarations to their own supported files or folders. "
                "If an additional name is only an imported helper or alias, give that Python name an _ prefix "
                "(for example, _helper). Each sandbox file defines one Sandbox; agents explicitly assign the names they may use.",
            )
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
    """Resolve one stable public tool identity or fail before composition."""

    name = getattr(value, "__name__", None)
    if not isinstance(name, str) or not name:
        name = getattr(value, "name", None)
    if not isinstance(name, str) or not name:
        raise BundleDuplicateError(
            f"tool {value!r} has no stable __name__ or name; use a named tool"
        )
    return name


def _subagent_name(value: Any) -> str:
    """Return the stable name used to detect and report subagent conflicts."""

    name = getattr(value, "name", None)
    # Agent.advanced permits an omitted root-facing override, but filesystem
    # subagents still need the native target's name to match their export.
    if (not isinstance(name, str) or not name) and isinstance(
        value, _AdvancedAgentDefinition
    ):
        name = getattr(value.target, "name", None)
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
    origins: dict[tuple[Any, ...], str] = {}
    for source, values in sources:
        for client in values:
            configuration = _mcp_connection_configuration(client)
            previous = origins.get(configuration)
            if previous is not None:
                raise BundleDuplicateError(
                    "duplicate MCP client configuration: "
                    f"{previous} and {source} resources"
                )
            merged.append(client)
            origins[configuration] = source
    return tuple(merged)

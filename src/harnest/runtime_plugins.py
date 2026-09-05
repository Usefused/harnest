"""Strict filesystem descriptors for application-owned runtime plugins."""

from __future__ import annotations

import hashlib
import heapq
import keyword
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .authoring_errors import folder_entry_error

from .source_tree import ignored_source_path
from ._runtime_plugin_project import (
    RuntimePluginProject,
    RuntimePluginProjectError,
    load_runtime_plugin_project,
    resolve_runtime_plugin_dependencies,
)


RUNTIME_PLUGIN_CAPABILITIES = frozenset(
    {
        "context.assets",
        "context.credentials",
        "context.continuations",
        "context.mcp",
        "context.resources",
        "context.session",
        "context.skills",
        "context.storage",
        "content.mcp",
        "content.skills",
        "content.subagents",
        "content.tools",
        "http.routes",
        "lifecycle.agent",
        "lifecycle.http",
        "lifecycle.mcp",
        "lifecycle.model",
        "lifecycle.skills",
        "lifecycle.tool",
        "native.adk",
        "native.langgraph",
        "policy.output",
        "sandbox.provider",
        "storage.assets",
        "storage.checkpoints",
        "storage.custom",
        "storage.sessions",
        "telemetry.exporter",
    }
)
_API_VERSION = "harnest.dev/v1alpha1"
_KIND = "RuntimePlugin"
_ENTRYPOINT = "plugin:plugin"
_MAX_MANIFEST_BYTES = 1024 * 1024
_DIGEST_CHUNK_BYTES = 1024 * 1024
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class RuntimePluginConventionError(ValueError):
    """An authored runtime-plugin descriptor violates the safe contract."""


@dataclass(frozen=True, slots=True)
class RuntimePluginDescriptor:
    """Immutable identity and declared authority for one runtime plugin."""

    name: str
    version: str
    directory: Path
    entrypoint: str
    requires: tuple[str, ...]
    capabilities: tuple[str, ...]
    digest: str
    dependencies: tuple[str, ...] = ()

    @property
    def source(self) -> Path:
        """Return the validated Python entrypoint without resolving outside the root."""

        return self.directory / (self.entrypoint.partition(":")[0] + ".py")

    @property
    def namespace(self) -> str:
        """Return the controlled public module installed during activation."""

        parent = "extensions" if self.entrypoint == "extension:extension" else "plugins"
        return f"harnest.{parent}.{self.name}"

    @property
    def lifecycle_directory(self) -> Path:
        """Keep legacy plugin hooks distinct from canonical extension hooks."""

        name = "lifecycle" if self.entrypoint == "extension:extension" else "extensions"
        return self.directory / name

    @property
    def lifecycle_origin(self) -> str:
        """Preserve provenance for capability checks across both package formats."""

        parent = self.namespace.split(".")[1]
        return f"{parent}/{self.name}/{self.lifecycle_directory.name}"


class _UniqueKeyLoader(yaml.SafeLoader):
    """Keep duplicate-key policy local to runtime-plugin manifests."""


def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
    """Reject ambiguous mappings before PyYAML can overwrite a declaration."""

    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, str):
            raise RuntimePluginConventionError(
                "runtime plugin manifest mapping keys must be strings"
            )
        if key in seen:
            raise RuntimePluginConventionError(
                f"duplicate runtime plugin manifest key: {key}"
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def discover_runtime_plugins(
    directory: str | Path,
) -> tuple[RuntimePluginDescriptor, ...]:
    """Discover runtime plugins in dependency order with lexical tie-breaking."""

    root = Path(directory)
    _require_optional_directory(root, label="plugins")
    if not root.exists():
        return ()
    descriptors = tuple(
        descriptor
        for plugin_directory in _plugin_directories(root)
        if (descriptor := _load_optional_descriptor(plugin_directory)) is not None
    )
    _validate_casefold_names(descriptors)
    _validate_dependency_projects(descriptors)
    return _dependency_order(descriptors)


def discover_application_extensions(root: str | Path) -> tuple[RuntimePluginDescriptor, ...]:
    """Resolve both layouts into one dependency graph before executing any code."""

    from .application_layout import lifecycle_directory

    root = Path(root)
    lifecycle = lifecycle_directory(root)
    descriptors = []
    for folder, canonical in (("plugins", False), ("extensions", True)):
        directory = root / folder
        if canonical and lifecycle == directory:
            continue
        _require_optional_directory(directory, label=folder)
        if not directory.exists():
            continue
        for child in _plugin_directories(directory):
            descriptor = _load_optional_descriptor(child, canonical=canonical)
            if descriptor is not None:
                descriptors.append(descriptor)
    _validate_casefold_names(descriptors)
    _validate_dependency_projects(descriptors)
    return _dependency_order(descriptors)


def validate_runtime_plugin_dependencies(
    agent_pyproject: Path | None,
    descriptors: Sequence[RuntimePluginDescriptor],
) -> None:
    """Validate one root/plugin dependency set without importing plugin code."""

    try:
        resolve_runtime_plugin_dependencies(
            agent_pyproject,
            tuple(
                (
                    descriptor.name,
                    RuntimePluginProject(
                        descriptor.name,
                        descriptor.version,
                        descriptor.dependencies,
                    ),
                )
                for descriptor in descriptors
            ),
        )
    except RuntimePluginProjectError as exc:
        raise RuntimePluginConventionError(str(exc)) from exc


def verify_runtime_plugin(descriptor: RuntimePluginDescriptor) -> None:
    """Fail if plugin content changed after its descriptor was discovered."""

    if not isinstance(descriptor, RuntimePluginDescriptor):
        raise TypeError("runtime plugin activation requires discovered descriptors")
    actual = runtime_plugin_digest(descriptor.directory)
    if actual != descriptor.digest:
        raise RuntimePluginConventionError(
            f"runtime plugin {descriptor.name!r} changed after discovery"
        )


def runtime_plugin_digest(directory: str | Path) -> str:
    """Hash every durable plugin file with stable relative-path framing."""

    root = Path(directory)
    _require_optional_directory(root, label="runtime plugin")
    if not root.exists():
        raise RuntimePluginConventionError(
            f"runtime plugin directory does not exist: {root}"
        )
    digest = hashlib.sha256()
    for path in _digest_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        _update_digest_file(digest, path)
    return f"sha256:{digest.hexdigest()}"


def _plugin_directories(root: Path) -> tuple[Path, ...]:
    """Return direct plugin children while rejecting unsafe root entries."""

    directories: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise RuntimePluginConventionError(
                f"plugin directory cannot be a symlink: {path}"
            )
        if (
            path.name in _IGNORED_FILE_NAMES
            or path.name.startswith(".")
            or path.name.startswith("_")
        ):
            continue
        if not path.is_dir():
            raise RuntimePluginConventionError(
                folder_entry_error(f"unexpected resource in plugins directory: {path}", path, kind="plugins")
            )
        directories.append(path)
    return tuple(directories)


def _load_optional_descriptor(
    directory: Path, *, canonical: bool = False
) -> RuntimePluginDescriptor | None:
    """Load explicit extension manifests and preserve legacy agent-plugin discovery."""

    # A portable manifest owns this package, even if invalid. Never fall through
    # to executable legacy content that an unimplemented namespace could carry.
    portable = directory / "plugin.json"
    if not canonical and (portable.exists() or portable.is_symlink()):
        return None
    path = directory / ("extension.yaml" if canonical else "plugin.yaml")
    if path.is_symlink():
        raise RuntimePluginConventionError(
            f"runtime plugin manifest cannot be a symlink: {path}"
        )
    if not path.exists():
        if canonical:
            raise RuntimePluginConventionError(
                f"Harnest Extension {directory} needs extension.yaml and extension.py. "
                "Put hook-only Python files in lifecycle/ instead."
            )
        return None
    if not path.is_file():
        raise RuntimePluginConventionError(
            f"runtime plugin manifest must be a regular file: {path}"
        )
    document = _load_manifest(path)
    return _descriptor_from_document(directory, document, canonical=canonical)


def _load_manifest(path: Path) -> Mapping[str, Any]:
    """Decode exactly one safe YAML mapping without retaining parser failures."""

    failure: str | None = None
    documents: list[Any] = []
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise RuntimePluginConventionError(
                f"runtime plugin manifest exceeds {_MAX_MANIFEST_BYTES} bytes: {path}"
            )
        documents = list(
            yaml.load_all(path.read_text("utf-8"), Loader=_UniqueKeyLoader)
        )
    except RuntimePluginConventionError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        failure = type(error).__name__
    if failure is not None:
        raise RuntimePluginConventionError(
            f"cannot read runtime plugin manifest {path} with {failure}"
        )
    if len(documents) != 1:
        raise RuntimePluginConventionError(
            f"runtime plugin manifest must contain exactly one YAML document: {path}"
        )
    return _require_mapping(documents[0], label="runtime plugin manifest")


def _descriptor_from_document(
    directory: Path, document: Mapping[str, Any], *, canonical: bool = False
) -> RuntimePluginDescriptor:
    """Validate the closed manifest schema before hashing executable content."""

    _reject_unknown(
        document,
        {"apiVersion", "kind", "metadata", "runtime", "requires", "capabilities"},
        label="runtime plugin manifest",
    )
    _require_literal(document, "apiVersion", _API_VERSION)
    _require_literal(document, "kind", "Extension" if canonical else _KIND)
    metadata = _require_mapping(document.get("metadata"), label="metadata")
    runtime = _require_mapping(document.get("runtime"), label="runtime")
    _reject_unknown(metadata, {"name", "version"}, label="metadata")
    _reject_unknown(runtime, {"entrypoint"}, label="runtime")
    name = _require_identifier(metadata.get("name"), label="metadata.name")
    if name != directory.name:
        raise RuntimePluginConventionError(
            f"runtime plugin metadata.name {name!r} must match folder {directory.name!r}"
        )
    version = _require_semver(metadata.get("version"))
    entrypoint = _require_nonempty_string(
        runtime.get("entrypoint"), label="runtime.entrypoint"
    )
    expected_entrypoint = "extension:extension" if canonical else _ENTRYPOINT
    if entrypoint != expected_entrypoint:
        raise RuntimePluginConventionError(
            f"Harnest Extension entrypoint must be {expected_entrypoint!r}"
        )
    requires = _parse_requires(document.get("requires"), canonical=canonical)
    capabilities = _parse_capabilities(document.get("capabilities"))
    _validate_entrypoint(directory, filename=entrypoint.partition(":")[0] + ".py")
    try:
        project = load_runtime_plugin_project(
            directory,
            expected_name=name,
            expected_version=version,
            canonical_extension=canonical,
        )
    except RuntimePluginProjectError as exc:
        raise RuntimePluginConventionError(str(exc)) from exc
    return RuntimePluginDescriptor(
        name=name,
        version=version,
        directory=directory.resolve(),
        entrypoint=entrypoint,
        requires=requires,
        capabilities=capabilities,
        digest=runtime_plugin_digest(directory),
        dependencies=project.dependencies,
    )


def _validate_dependency_projects(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> None:
    """Reject cross-plugin conflicts during filesystem-only discovery."""

    validate_runtime_plugin_dependencies(None, descriptors)


def _parse_requires(value: Any, *, canonical: bool = False) -> tuple[str, ...]:
    """Validate optional dependency names independently from graph resolution."""

    if value is None:
        return ()
    mapping = _require_mapping(value, label="requires")
    key = "extensions" if canonical else "plugins"
    _reject_unknown(mapping, {key}, label="requires")
    names = _require_string_list(mapping.get(key, []), label=f"requires.{key}")
    validated = tuple(
        _require_identifier(name, label=f"requires.{key}") for name in names
    )
    _reject_duplicates(validated, label="runtime plugin dependencies")
    return tuple(sorted(validated))


def _parse_capabilities(value: Any) -> tuple[str, ...]:
    """Validate declarations against the centralized authority vocabulary."""

    names = _require_string_list([] if value is None else value, label="capabilities")
    _reject_duplicates(names, label="runtime plugin capabilities")
    unknown = sorted(set(names) - RUNTIME_PLUGIN_CAPABILITIES)
    if unknown:
        raise RuntimePluginConventionError(
            "unknown runtime plugin capabilities: " + ", ".join(unknown)
        )
    return tuple(sorted(names))


def _validate_entrypoint(directory: Path, *, filename: str = "plugin.py") -> None:
    """Require the format's contained regular entrypoint before activation."""

    source = directory / filename
    if source.is_symlink():
        raise RuntimePluginConventionError(
            f"runtime plugin entrypoint cannot be a symlink: {source}"
        )
    if not source.is_file():
        raise RuntimePluginConventionError(
            f"runtime plugin entrypoint does not exist: {source}"
        )


def _validate_casefold_names(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> None:
    """Reject plugin identities that vary by filesystem case behavior."""

    seen: dict[str, str] = {}
    for descriptor in descriptors:
        key = descriptor.name.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise RuntimePluginConventionError(
                f"runtime plugin name collision: {previous!r} and {descriptor.name!r}"
            )
        seen[key] = descriptor.name


def _dependency_order(
    descriptors: Sequence[RuntimePluginDescriptor],
) -> tuple[RuntimePluginDescriptor, ...]:
    """Topologically order descriptors without filesystem-order dependence."""

    by_name = {descriptor.name: descriptor for descriptor in descriptors}
    _validate_dependencies(descriptors, by_name)
    dependents, remaining = _dependency_graph(descriptors, by_name)
    return _resolve_dependency_order(by_name, dependents, remaining)


def _dependency_graph(
    descriptors: Sequence[RuntimePluginDescriptor],
    by_name: Mapping[str, RuntimePluginDescriptor],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Build adjacency and indegree data without executing plugin content."""

    dependents = {name: [] for name in by_name}
    remaining = {item.name: len(item.requires) for item in descriptors}
    for descriptor in descriptors:
        for required in descriptor.requires:
            dependents[required].append(descriptor.name)
    return dependents, remaining


def _resolve_dependency_order(
    by_name: Mapping[str, RuntimePluginDescriptor],
    dependents: Mapping[str, list[str]],
    remaining: dict[str, int],
) -> tuple[RuntimePluginDescriptor, ...]:
    """Apply Kahn ordering with a heap for a stable lexical tie-break."""

    ready = [name for name, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[RuntimePluginDescriptor] = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(by_name[name])
        for dependent in sorted(dependents[name]):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(by_name):
        cyclic = sorted(name for name, count in remaining.items() if count > 0)
        raise RuntimePluginConventionError(
            "runtime plugin dependency cycle: " + ", ".join(cyclic)
        )
    return tuple(ordered)


def _validate_dependencies(
    descriptors: Sequence[RuntimePluginDescriptor],
    by_name: Mapping[str, RuntimePluginDescriptor],
) -> None:
    """Reject self and missing dependencies before partial graph ordering."""

    for descriptor in descriptors:
        if descriptor.name in descriptor.requires:
            raise RuntimePluginConventionError(
                f"runtime plugin {descriptor.name!r} cannot require itself"
            )
        missing = sorted(set(descriptor.requires) - by_name.keys())
        if missing:
            raise RuntimePluginConventionError(
                f"runtime plugin {descriptor.name!r} requires missing plugins: "
                + ", ".join(missing)
            )


def _digest_files(root: Path) -> tuple[Path, ...]:
    """Validate the whole tree and return durable files in canonical order."""

    files: list[Path] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root)
        if ignored_source_path(relative):
            continue
        if path.is_symlink():
            raise RuntimePluginConventionError(
                f"runtime plugin resource cannot be a symlink: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimePluginConventionError(
                f"runtime plugin resource must be a regular file: {path}"
            )
        if path.name in _IGNORED_FILE_NAMES or path.suffix in {".pyc", ".pyo"}:
            continue
        files.append(path)
    return tuple(files)


def _update_digest_file(digest: Any, path: Path) -> None:
    """Hash one regular file incrementally so plugin size cannot spike memory."""

    try:
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as source:
            while chunk := source.read(_DIGEST_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise RuntimePluginConventionError(
            f"cannot hash runtime plugin resource {path} with {type(error).__name__}"
        ) from None


def _require_optional_directory(path: Path, *, label: str) -> None:
    """Reject symlinked or non-directory roots without requiring existence."""

    if path.is_symlink():
        raise RuntimePluginConventionError(
            f"{label} directory cannot be a symlink: {path}"
        )
    if path.exists() and not path.is_dir():
        raise RuntimePluginConventionError(f"{label} path must be a directory: {path}")


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    """Return a string-keyed mapping or reject the schema node."""

    if not isinstance(value, dict):
        raise RuntimePluginConventionError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise RuntimePluginConventionError(f"{label} keys must be strings")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    """Keep descriptor evolution explicit instead of silently ignoring policy."""

    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RuntimePluginConventionError(
            f"unknown {label} fields: " + ", ".join(unknown)
        )


def _require_literal(document: Mapping[str, Any], key: str, expected: str) -> None:
    """Validate one required discriminator exactly."""

    if document.get(key) != expected:
        raise RuntimePluginConventionError(f"runtime plugin {key} must be {expected!r}")


def _require_nonempty_string(value: Any, *, label: str) -> str:
    """Reject coercion and whitespace-dependent descriptor identities."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimePluginConventionError(f"{label} must be a non-empty string")
    return value


def _require_identifier(value: Any, *, label: str) -> str:
    """Require names usable in ordinary dotted Python source syntax."""

    name = _require_nonempty_string(value, label=label)
    if not name.isidentifier() or keyword.iskeyword(name):
        raise RuntimePluginConventionError(
            f"{label} must be a non-keyword Python identifier"
        )
    return name


def _require_semver(value: Any) -> str:
    """Require canonical SemVer so dependency identity is portable."""

    version = _require_nonempty_string(value, label="metadata.version")
    if _SEMVER.fullmatch(version) is None:
        raise RuntimePluginConventionError(
            "metadata.version must be a valid semantic version"
        )
    return version


def _require_string_list(value: Any, *, label: str) -> tuple[str, ...]:
    """Validate exact YAML sequence types without accepting scalar strings."""

    if not isinstance(value, list):
        raise RuntimePluginConventionError(f"{label} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise RuntimePluginConventionError(f"{label} must contain only strings")
    return tuple(value)


def _reject_duplicates(values: Sequence[str], *, label: str) -> None:
    """Reject repeated declarations rather than normalizing ambiguous input."""

    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise RuntimePluginConventionError(
            f"duplicate {label}: " + ", ".join(duplicates)
        )


__all__ = [
    "RUNTIME_PLUGIN_CAPABILITIES",
    "RuntimePluginConventionError",
    "RuntimePluginDescriptor",
    "validate_runtime_plugin_dependencies",
    "discover_runtime_plugins",
    "runtime_plugin_digest",
    "verify_runtime_plugin",
]

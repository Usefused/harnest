"""Strict filesystem descriptors for application-owned Harnest Extensions."""

from __future__ import annotations

import hashlib
import heapq
import keyword
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import yaml

from .authoring_errors import folder_entry_error

from .source_tree import ignored_source_path
from ._extension_project import (
    ExtensionProject,
    ExtensionProjectError,
    load_extension_project,
    resolve_extension_dependencies,
)


EXTENSION_CAPABILITIES = frozenset(
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
        "storage.tasks",
        "storage.cron",
        "telemetry.exporter",
    }
)
_API_VERSION = "harnest.dev/v1alpha1"
_LEGACY_KIND = "RuntimePlugin"
_LEGACY_ENTRYPOINT = "plugin:plugin"
_MAX_MANIFEST_BYTES = 1024 * 1024
_DIGEST_CHUNK_BYTES = 1024 * 1024
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})
EXTENSION_CONTRIBUTION_KINDS = (
    "lifecycle",
    "mcp",
    "skills",
    "subagents",
    "tools",
)
EXTENSION_CONTENT_CAPABILITIES = {
    "mcp": "content.mcp",
    "skills": "content.skills",
    "subagents": "content.subagents",
    "tools": "content.tools",
}
_RESERVED_CONTRIBUTION_ROOTS = frozenset(
    {"README.md", "extension.py", "extension.yaml", "lib", "pyproject.toml"}
)
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CONTRIBUTION_PATH = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*$"
)


class ExtensionConventionError(ValueError):
    """An authored Harnest Extension violates the safe contract."""


@dataclass(frozen=True, slots=True)
class ExtensionDescriptor:
    """Immutable identity and declared authority for one extension."""

    name: str
    version: str
    directory: Path
    entrypoint: str
    requires: tuple[str, ...]
    capabilities: tuple[str, ...]
    digest: str
    dependencies: tuple[str, ...] = ()
    contributions: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def source(self) -> Path:
        """Return the validated Python entrypoint without resolving outside the root."""

        return self.directory / (self.entrypoint.partition(":")[0] + ".py")

    @property
    def namespace(self) -> str:
        """Return the controlled public module installed during activation."""

        return f"harnest.extensions.{self.name}"

    def contribution_paths(self, kind: str) -> tuple[Path, ...]:
        """Return declared package paths for one fixed contribution surface."""

        if kind not in EXTENSION_CONTRIBUTION_KINDS:
            raise ValueError(f"unknown Harnest Extension contribution kind: {kind}")
        values = dict(self.contributions).get(kind, ())
        return tuple(self.directory / value for value in values)

    def contribution_origin(self, path: Path) -> str:
        """Preserve a declared contribution's package-relative provenance."""

        return f"extensions/{self.name}/{path.relative_to(self.directory).as_posix()}"


class _UniqueKeyLoader(yaml.SafeLoader):
    """Keep duplicate-key policy local to extension manifests."""


def _construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
    """Reject ambiguous mappings before PyYAML can overwrite a declaration."""

    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, str):
            raise ExtensionConventionError(
                "Harnest Extension manifest mapping keys must be strings"
            )
        if key in seen:
            raise ExtensionConventionError(
                f"duplicate Harnest Extension manifest key: {key}"
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def discover_extensions(
    directory: str | Path,
) -> tuple[ExtensionDescriptor, ...]:
    """Discover extensions in dependency order with lexical tie-breaking."""

    root = Path(directory)
    _require_optional_directory(root, label="extensions")
    if not root.exists():
        return ()
    descriptors = tuple(
        descriptor
        for extension_directory in _extension_directories(root)
        if (descriptor := _load_optional_descriptor(extension_directory)) is not None
    )
    _validate_casefold_names(descriptors)
    _validate_dependency_projects(descriptors)
    return _dependency_order(descriptors)


def discover_application_extensions(root: str | Path) -> tuple[ExtensionDescriptor, ...]:
    """Discover the canonical extension directory without executing package code."""

    from .application_layout import lifecycle_directory

    root = Path(root)
    lifecycle = lifecycle_directory(root)
    directory = root / "extensions"
    if lifecycle == directory:
        return ()
    return discover_extensions(directory)


def discover_legacy_extensions(directory: str | Path) -> tuple[ExtensionDescriptor, ...]:
    """Read removed extension packages solely for ``harnest upgrade``."""

    root = Path(directory)
    _require_optional_directory(root, label="plugins")
    if not root.exists():
        return ()
    descriptors = tuple(
        descriptor
        for child in _extension_directories(root)
        if (descriptor := _load_optional_descriptor(child, legacy=True)) is not None
    )
    _validate_casefold_names(descriptors)
    _validate_dependency_projects(descriptors)
    return _dependency_order(descriptors)


def validate_extension_dependencies(
    agent_pyproject: Path | None,
    descriptors: Sequence[ExtensionDescriptor],
) -> None:
    """Validate one root/plugin dependency set without importing plugin code."""

    try:
        resolve_extension_dependencies(
            agent_pyproject,
            tuple(
                (
                    descriptor.name,
                    ExtensionProject(
                        descriptor.name,
                        descriptor.version,
                        descriptor.dependencies,
                    ),
                )
                for descriptor in descriptors
            ),
        )
    except ExtensionProjectError as exc:
        raise ExtensionConventionError(str(exc)) from exc


def verify_extension(descriptor: ExtensionDescriptor) -> None:
    """Fail if plugin content changed after its descriptor was discovered."""

    if not isinstance(descriptor, ExtensionDescriptor):
        raise TypeError("Harnest Extension activation requires discovered descriptors")
    actual = extension_digest(descriptor.directory)
    if actual != descriptor.digest:
        raise ExtensionConventionError(
            f"Harnest Extension {descriptor.name!r} changed after discovery"
        )


def extension_digest(directory: str | Path) -> str:
    """Hash every durable plugin file with stable relative-path framing."""

    root = Path(directory)
    _require_optional_directory(root, label="Harnest Extension")
    if not root.exists():
        raise ExtensionConventionError(
            f"Harnest Extension directory does not exist: {root}"
        )
    digest = hashlib.sha256()
    for path in _digest_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        _update_digest_file(digest, path)
    return f"sha256:{digest.hexdigest()}"


def _extension_directories(root: Path) -> tuple[Path, ...]:
    """Return direct package children while rejecting unsafe root entries."""

    directories: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise ExtensionConventionError(
                f"extension directory cannot be a symlink: {path}"
            )
        if (
            path.name in _IGNORED_FILE_NAMES
            or path.name.startswith(".")
            or path.name.startswith("_")
        ):
            continue
        if not path.is_dir():
            raise ExtensionConventionError(
                folder_entry_error(
                    f"unexpected resource in extension directory: {path}",
                    path,
                    kind="extensions",
                )
            )
        directories.append(path)
    return tuple(directories)


def _load_optional_descriptor(
    directory: Path, *, legacy: bool = False
) -> ExtensionDescriptor | None:
    """Load one canonical manifest or a legacy manifest for upgrade inspection."""

    # A portable manifest owns this package, even if invalid. Never fall through
    # to executable legacy content that an unimplemented namespace could carry.
    portable = directory / "plugin.json"
    if legacy and (portable.exists() or portable.is_symlink()):
        return None
    path = directory / ("plugin.yaml" if legacy else "extension.yaml")
    if path.is_symlink():
        raise ExtensionConventionError(
            f"Harnest Extension manifest cannot be a symlink: {path}"
        )
    if not path.exists():
        if not legacy:
            raise ExtensionConventionError(
                f"Harnest Extension {directory} needs extension.yaml and extension.py. "
                "Put hook-only Python files in lifecycle/ instead."
            )
        return None
    if not path.is_file():
        raise ExtensionConventionError(
            f"Harnest Extension manifest must be a regular file: {path}"
        )
    document = _load_manifest(path)
    return _descriptor_from_document(directory, document, legacy=legacy)


def _load_manifest(path: Path) -> Mapping[str, Any]:
    """Decode exactly one safe YAML mapping without retaining parser failures."""

    failure: str | None = None
    documents: list[Any] = []
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ExtensionConventionError(
                f"Harnest Extension manifest exceeds {_MAX_MANIFEST_BYTES} bytes: {path}"
            )
        documents = list(
            yaml.load_all(path.read_text("utf-8"), Loader=_UniqueKeyLoader)
        )
    except ExtensionConventionError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        failure = type(error).__name__
    if failure is not None:
        raise ExtensionConventionError(
            f"cannot read Harnest Extension manifest {path} with {failure}"
        )
    if len(documents) != 1:
        raise ExtensionConventionError(
            f"Harnest Extension manifest must contain exactly one YAML document: {path}"
        )
    return _require_mapping(documents[0], label="Harnest Extension manifest")


def _descriptor_from_document(
    directory: Path, document: Mapping[str, Any], *, legacy: bool = False
) -> ExtensionDescriptor:
    """Validate the closed manifest schema before hashing executable content."""

    _reject_unknown(
        document,
        {
            "apiVersion",
            "kind",
            "metadata",
            "runtime",
            "requires",
            "contributes",
            "capabilities",
        },
        label="Harnest Extension manifest",
    )
    _require_literal(document, "apiVersion", _API_VERSION)
    _require_literal(document, "kind", _LEGACY_KIND if legacy else "Extension")
    metadata = _require_mapping(document.get("metadata"), label="metadata")
    runtime = _require_mapping(document.get("runtime"), label="runtime")
    _reject_unknown(metadata, {"name", "version"}, label="metadata")
    _reject_unknown(runtime, {"entrypoint"}, label="runtime")
    name = _require_identifier(metadata.get("name"), label="metadata.name")
    if name != directory.name:
        raise ExtensionConventionError(
            f"Harnest Extension metadata.name {name!r} must match folder {directory.name!r}"
        )
    version = _require_semver(metadata.get("version"))
    entrypoint = _require_nonempty_string(
        runtime.get("entrypoint"), label="runtime.entrypoint"
    )
    expected_entrypoint = _LEGACY_ENTRYPOINT if legacy else "extension:extension"
    if entrypoint != expected_entrypoint:
        raise ExtensionConventionError(
            f"Harnest Extension entrypoint must be {expected_entrypoint!r}"
        )
    requires = _parse_requires(document.get("requires"), legacy=legacy)
    contributions = _parse_contributions(
        directory, None if legacy else document.get("contributes")
    )
    capabilities = _parse_capabilities(document.get("capabilities"))
    _validate_contribution_capabilities(contributions, capabilities)
    _validate_entrypoint(directory, filename=entrypoint.partition(":")[0] + ".py")
    try:
        project = load_extension_project(
            directory,
            expected_name=name,
            expected_version=version,
            canonical_extension=not legacy,
        )
    except ExtensionProjectError as exc:
        raise ExtensionConventionError(str(exc)) from exc
    return ExtensionDescriptor(
        name=name,
        version=version,
        directory=directory.resolve(),
        entrypoint=entrypoint,
        requires=requires,
        capabilities=capabilities,
        digest=extension_digest(directory),
        dependencies=project.dependencies,
        contributions=contributions,
    )


def _validate_dependency_projects(
    descriptors: Sequence[ExtensionDescriptor],
) -> None:
    """Reject cross-plugin conflicts during filesystem-only discovery."""

    validate_extension_dependencies(None, descriptors)


def _parse_requires(value: Any, *, legacy: bool = False) -> tuple[str, ...]:
    """Validate optional dependency names independently from graph resolution."""

    if value is None:
        return ()
    mapping = _require_mapping(value, label="requires")
    key = "plugins" if legacy else "extensions"
    _reject_unknown(mapping, {key}, label="requires")
    names = _require_string_list(mapping.get(key, []), label=f"requires.{key}")
    validated = tuple(
        _require_identifier(name, label=f"requires.{key}") for name in names
    )
    _reject_duplicates(validated, label="Harnest Extension dependencies")
    return tuple(sorted(validated))


def _parse_capabilities(value: Any) -> tuple[str, ...]:
    """Validate declarations against the centralized authority vocabulary."""

    names = _require_string_list([] if value is None else value, label="capabilities")
    _reject_duplicates(names, label="Harnest Extension capabilities")
    unknown = sorted(set(names) - EXTENSION_CAPABILITIES)
    if unknown:
        raise ExtensionConventionError(
            "unknown Harnest Extension capabilities: " + ", ".join(unknown)
        )
    return tuple(sorted(names))


def _parse_contributions(
    directory: Path, value: Any
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Validate explicit, non-overlapping package-relative content roots."""

    if value is None:
        return ()
    mapping = _require_mapping(value, label="contributes")
    _reject_unknown(mapping, set(EXTENSION_CONTRIBUTION_KINDS), label="contributes")
    if not mapping:
        raise ExtensionConventionError(
            "contributes must declare at least one contribution path"
        )
    contributions: list[tuple[str, tuple[str, ...]]] = []
    declared: list[tuple[str, PurePosixPath]] = []
    for kind in EXTENSION_CONTRIBUTION_KINDS:
        paths = _parse_contribution_kind(directory, mapping, kind)
        if not paths:
            continue
        contributions.append((kind, tuple(path.as_posix() for path in paths)))
        declared.extend((kind, path) for path in paths)
    _reject_overlapping_contributions(declared)
    return tuple((kind, paths) for kind, paths in contributions if paths)


def _parse_contribution_kind(
    directory: Path, mapping: Mapping[str, Any], kind: str
) -> tuple[PurePosixPath, ...]:
    """Parse one optional contribution list through the shared path contract."""

    if kind not in mapping:
        return ()
    raw_paths = _require_string_list(mapping[kind], label=f"contributes.{kind}")
    if not raw_paths:
        raise ExtensionConventionError(
            f"contributes.{kind} must contain at least one path"
        )
    paths = tuple(
        _validate_contribution_path(directory, kind, path) for path in raw_paths
    )
    _reject_duplicates(
        tuple(path.as_posix() for path in paths),
        label=f"Harnest Extension contributes.{kind}",
    )
    return paths


def _validate_contribution_capabilities(
    contributions: Sequence[tuple[str, tuple[str, ...]]],
    capabilities: Sequence[str],
) -> None:
    """Keep file projection separate from, and subordinate to, runtime authority."""

    for kind, _paths in contributions:
        required = EXTENSION_CONTENT_CAPABILITIES.get(kind)
        if required is not None and required not in capabilities:
            raise ExtensionConventionError(
                f"Harnest Extension must declare capability {required!r} "
                f"for contributes.{kind}"
            )


def _validate_contribution_path(
    directory: Path, kind: str, value: str
) -> PurePosixPath:
    """Require a contained, existing directory without platform-dependent syntax."""

    text = _require_nonempty_string(value, label=f"contributes.{kind}").rstrip("/")
    path = PurePosixPath(text)
    if not _safe_contribution_path(text, path):
        raise ExtensionConventionError(
            f"contributes.{kind} path must be a safe package-relative directory: {value!r}"
        )
    target = directory.joinpath(*path.parts)
    if target.is_symlink() or not target.is_dir():
        raise ExtensionConventionError(
            f"contributes.{kind} path must be an existing regular directory: {value!r}"
        )
    return path


def _safe_contribution_path(text: str, path: PurePosixPath) -> bool:
    """Keep path syntax deterministic before touching the package filesystem."""

    if not text or "\\" in text or path.is_absolute():
        return False
    if _CONTRIBUTION_PATH.fullmatch(text) is None:
        return False
    if path.parts[0] in _RESERVED_CONTRIBUTION_ROOTS:
        return False
    return not path.parts[0].startswith((".", "_"))


def _reject_overlapping_contributions(
    values: Sequence[tuple[str, PurePosixPath]],
) -> None:
    """Prevent one package tree from being interpreted through multiple contracts."""

    for index, (kind, path) in enumerate(values):
        for other_kind, other_path in values[index + 1 :]:
            if path == other_path or path in other_path.parents or other_path in path.parents:
                raise ExtensionConventionError(
                    "Harnest Extension contribution paths overlap: "
                    f"contributes.{kind}={path.as_posix()!r} and "
                    f"contributes.{other_kind}={other_path.as_posix()!r}"
                )


def _validate_entrypoint(directory: Path, *, filename: str = "extension.py") -> None:
    """Require the format's contained regular entrypoint before activation."""

    source = directory / filename
    if source.is_symlink():
        raise ExtensionConventionError(
            f"Harnest Extension entrypoint cannot be a symlink: {source}"
        )
    if not source.is_file():
        raise ExtensionConventionError(
            f"Harnest Extension entrypoint does not exist: {source}"
        )


def _validate_casefold_names(
    descriptors: Sequence[ExtensionDescriptor],
) -> None:
    """Reject plugin identities that vary by filesystem case behavior."""

    seen: dict[str, str] = {}
    for descriptor in descriptors:
        key = descriptor.name.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise ExtensionConventionError(
                f"Harnest Extension name collision: {previous!r} and {descriptor.name!r}"
            )
        seen[key] = descriptor.name


def _dependency_order(
    descriptors: Sequence[ExtensionDescriptor],
) -> tuple[ExtensionDescriptor, ...]:
    """Topologically order descriptors without filesystem-order dependence."""

    by_name = {descriptor.name: descriptor for descriptor in descriptors}
    _validate_dependencies(descriptors, by_name)
    dependents, remaining = _dependency_graph(descriptors, by_name)
    return _resolve_dependency_order(by_name, dependents, remaining)


def _dependency_graph(
    descriptors: Sequence[ExtensionDescriptor],
    by_name: Mapping[str, ExtensionDescriptor],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Build adjacency and indegree data without executing plugin content."""

    dependents = {name: [] for name in by_name}
    remaining = {item.name: len(item.requires) for item in descriptors}
    for descriptor in descriptors:
        for required in descriptor.requires:
            dependents[required].append(descriptor.name)
    return dependents, remaining


def _resolve_dependency_order(
    by_name: Mapping[str, ExtensionDescriptor],
    dependents: Mapping[str, list[str]],
    remaining: dict[str, int],
) -> tuple[ExtensionDescriptor, ...]:
    """Apply Kahn ordering with a heap for a stable lexical tie-break."""

    ready = [name for name, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[ExtensionDescriptor] = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(by_name[name])
        for dependent in sorted(dependents[name]):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(by_name):
        cyclic = sorted(name for name, count in remaining.items() if count > 0)
        raise ExtensionConventionError(
            "Harnest Extension dependency cycle: " + ", ".join(cyclic)
        )
    return tuple(ordered)


def _validate_dependencies(
    descriptors: Sequence[ExtensionDescriptor],
    by_name: Mapping[str, ExtensionDescriptor],
) -> None:
    """Reject self and missing dependencies before partial graph ordering."""

    for descriptor in descriptors:
        if descriptor.name in descriptor.requires:
            raise ExtensionConventionError(
                f"Harnest Extension {descriptor.name!r} cannot require itself"
            )
        missing = sorted(set(descriptor.requires) - by_name.keys())
        if missing:
            raise ExtensionConventionError(
                f"Harnest Extension {descriptor.name!r} requires missing extensions: "
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
            raise ExtensionConventionError(
                f"Harnest Extension resource cannot be a symlink: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ExtensionConventionError(
                f"Harnest Extension resource must be a regular file: {path}"
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
        raise ExtensionConventionError(
            f"cannot hash Harnest Extension resource {path} with {type(error).__name__}"
        ) from None


def _require_optional_directory(path: Path, *, label: str) -> None:
    """Reject symlinked or non-directory roots without requiring existence."""

    if path.is_symlink():
        raise ExtensionConventionError(
            f"{label} directory cannot be a symlink: {path}"
        )
    if path.exists() and not path.is_dir():
        raise ExtensionConventionError(f"{label} path must be a directory: {path}")


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    """Return a string-keyed mapping or reject the schema node."""

    if not isinstance(value, dict):
        raise ExtensionConventionError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ExtensionConventionError(f"{label} keys must be strings")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    """Keep descriptor evolution explicit instead of silently ignoring policy."""

    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExtensionConventionError(
            f"unknown {label} fields: " + ", ".join(unknown)
        )


def _require_literal(document: Mapping[str, Any], key: str, expected: str) -> None:
    """Validate one required discriminator exactly."""

    if document.get(key) != expected:
        raise ExtensionConventionError(f"Harnest Extension {key} must be {expected!r}")


def _require_nonempty_string(value: Any, *, label: str) -> str:
    """Reject coercion and whitespace-dependent descriptor identities."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ExtensionConventionError(f"{label} must be a non-empty string")
    return value


def _require_identifier(value: Any, *, label: str) -> str:
    """Require names usable in ordinary dotted Python source syntax."""

    name = _require_nonempty_string(value, label=label)
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ExtensionConventionError(
            f"{label} must be a non-keyword Python identifier"
        )
    return name


def _require_semver(value: Any) -> str:
    """Require canonical SemVer so dependency identity is portable."""

    version = _require_nonempty_string(value, label="metadata.version")
    if _SEMVER.fullmatch(version) is None:
        raise ExtensionConventionError(
            "metadata.version must be a valid semantic version"
        )
    return version


def _require_string_list(value: Any, *, label: str) -> tuple[str, ...]:
    """Validate exact YAML sequence types without accepting scalar strings."""

    if not isinstance(value, list):
        raise ExtensionConventionError(f"{label} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise ExtensionConventionError(f"{label} must contain only strings")
    return tuple(value)


def _reject_duplicates(values: Sequence[str], *, label: str) -> None:
    """Reject repeated declarations rather than normalizing ambiguous input."""

    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ExtensionConventionError(
            f"duplicate {label}: " + ", ".join(duplicates)
        )


__all__ = [
    "EXTENSION_CAPABILITIES",
    "EXTENSION_CONTRIBUTION_KINDS",
    "EXTENSION_CONTENT_CAPABILITIES",
    "ExtensionConventionError",
    "ExtensionDescriptor",
    "validate_extension_dependencies",
    "discover_extensions",
    "extension_digest",
    "verify_extension",
]

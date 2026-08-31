"""Provider-neutral progressive skill discovery and loading."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import yaml

from .tool import tool


_SOURCE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._~-]{0,63}$")
_MAX_SKILL_ID_CHARS = 256
_MAX_SKILL_NAME_CHARS = 128
_MAX_DESCRIPTION_CHARS = 1024
_MAX_TEXT_RESOURCE_BYTES = 1024 * 1024
_MAX_PAGE_SIZE = 100
_FILESYSTEM_SOURCE = "filesystem"


class SkillError(RuntimeError):
    """Base error for invalid or unavailable skill operations."""


class SkillValidationError(SkillError):
    """A source or skill value violates the portable contract."""


class SkillNotFoundError(SkillError, LookupError):
    """A requested source, skill, version, or resource is unavailable."""


class SkillResourceNotSupported(SkillError):
    """A source deliberately does not expose supporting resources."""


class SkillSourceExecutionError(SkillError):
    """A source operation failed across the governed runtime boundary."""


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    """Bounded metadata that lets a model select a skill progressively."""

    id: str
    name: str
    description: str
    version: str

    def __post_init__(self) -> None:
        """Keep model-visible routing fields non-empty and bounded."""

        _require_text(self.id, "skill id", maximum=_MAX_SKILL_ID_CHARS)
        _require_text(self.name, "skill name", maximum=_MAX_SKILL_NAME_CHARS)
        _require_text(
            self.description,
            "skill description",
            maximum=_MAX_DESCRIPTION_CHARS,
        )
        _require_text(self.version, "skill version", maximum=256)


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """One versioned instruction document returned by a skill source."""

    descriptor: SkillDescriptor
    instructions: str

    def __post_init__(self) -> None:
        """Require one validated descriptor and a bounded instruction body."""

        if not isinstance(self.descriptor, SkillDescriptor):
            raise TypeError("skill document descriptor must be SkillDescriptor")
        _require_text(
            self.instructions,
            "skill instructions",
            maximum=_MAX_TEXT_RESOURCE_BYTES,
        )
        if len(self.instructions.encode("utf-8")) > _MAX_TEXT_RESOURCE_BYTES:
            raise SkillValidationError("skill instructions exceed the size limit")


@dataclass(frozen=True, slots=True)
class SkillResource:
    """One bounded UTF-8 supporting resource for a selected skill version."""

    skill_id: str
    version: str
    path: str
    content: str

    def __post_init__(self) -> None:
        """Keep source resources textual and safe for a model tool response."""

        _require_text(self.skill_id, "skill id", maximum=_MAX_SKILL_ID_CHARS)
        _require_text(self.version, "skill version", maximum=256)
        _require_text(self.path, "skill resource path", maximum=1024)
        if not isinstance(self.content, str):
            raise TypeError("skill resource content must be text")
        if len(self.content.encode("utf-8")) > _MAX_TEXT_RESOURCE_BYTES:
            raise SkillValidationError("skill resource content exceeds the size limit")


@dataclass(frozen=True, slots=True)
class SkillPage:
    """One source-owned page that never requires loading skill bodies."""

    items: tuple[SkillDescriptor, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        """Freeze source output and bound its optional continuation token."""

        normalized = tuple(self.items)
        if any(not isinstance(item, SkillDescriptor) for item in normalized):
            raise TypeError("skill page items must be SkillDescriptor values")
        if self.next_cursor is not None:
            _require_text(self.next_cursor, "skill page cursor", maximum=1024)
        object.__setattr__(self, "items", normalized)


@dataclass(frozen=True, slots=True)
class CatalogSkill:
    """Attach registry routing identity to source-owned skill metadata."""

    source: str
    descriptor: SkillDescriptor

    def __post_init__(self) -> None:
        """Validate registry-owned source routing independently of metadata."""

        _validate_source_name(self.source)
        if not isinstance(self.descriptor, SkillDescriptor):
            raise TypeError("catalog skill descriptor must be SkillDescriptor")

    def as_dict(self) -> dict[str, str]:
        """Return the stable model-facing routing representation."""

        return {
            "name": self.descriptor.name,
            "description": self.descriptor.description,
            "source": self.source,
            "id": self.descriptor.id,
            "version": self.descriptor.version,
        }


@dataclass(frozen=True, slots=True)
class SkillCatalogPage:
    """A bounded catalog merged from one or more independently paged sources."""

    items: tuple[CatalogSkill, ...]
    next_cursors: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False

    def __post_init__(self) -> None:
        """Freeze merged pages before exposing them to tools or application code."""

        normalized = tuple(self.items)
        if any(not isinstance(item, CatalogSkill) for item in normalized):
            raise TypeError("skill catalog items must be CatalogSkill values")
        cursors = dict(self.next_cursors)
        for source, cursor in cursors.items():
            _validate_source_name(source)
            _require_text(cursor, "skill page cursor", maximum=1024)
        if type(self.truncated) is not bool:
            raise TypeError("skill catalog truncated must be boolean")
        object.__setattr__(self, "items", normalized)
        object.__setattr__(self, "next_cursors", MappingProxyType(cursors))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe catalog without including instruction bodies."""

        value: dict[str, Any] = {
            "skills": [item.as_dict() for item in self.items],
        }
        if self.next_cursors:
            value["nextCursors"] = dict(self.next_cursors)
        if self.truncated:
            value["truncated"] = True
        return value


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Revocable invocation identity passed to every dynamic skill source."""

    _active: Any = field(repr=False)

    @property
    def framework(self) -> str:
        """Return the framework active for this invocation."""

        self._active._require_active()
        return self._active.framework

    @property
    def agent_name(self) -> str:
        """Return the exact root or subagent currently requesting skills."""

        self._active._require_active()
        return self._active.agent_name

    @property
    def parent_agent_name(self) -> str | None:
        """Return the immediate parent identity when this is a subagent."""

        self._active._require_active()
        return self._active.parent_agent_name

    @property
    def depth(self) -> int:
        """Return the managed subagent nesting depth."""

        self._active._require_active()
        return self._active.depth

    @property
    def invocation_id(self) -> str:
        """Return the current invocation correlation identity."""

        self._active._require_active()
        return self._active.invocation_id

    @property
    def user_id(self) -> str:
        """Return the authenticated runtime user identity."""

        self._active._require_active()
        return self._active.user_id

    @property
    def claims(self) -> Mapping[str, Any]:
        """Return verified non-secret authentication claims when available."""

        self._active._require_active()
        from .runtime_auth import _active_authenticated_principal

        principal = _active_authenticated_principal()
        if principal is None:
            return MappingProxyType({})
        if principal.user_id != self._active.user_id:
            # A provider must never authorize against claims from a principal
            # different from the invocation identity used for session scope.
            raise SkillSourceExecutionError(
                "authenticated principal does not match invocation identity"
            )
        return principal.claims

    @property
    def session_id(self) -> str:
        """Return the framework-neutral session identity."""

        self._active._require_active()
        return self._active.session_id

    @property
    def metadata(self) -> Mapping[str, Any]:
        """Return immutable non-secret invocation metadata."""

        self._active._require_active()
        return self._active.metadata

    @property
    def credentials(self) -> Any:
        """Return private credential resolution without copying secret values."""

        self._active._require_active()
        from .credentials import credentials

        return credentials

    @property
    def storage(self) -> Any:
        """Return invocation-scoped access to named application storage."""

        self._active._require_active()
        from .context_storage import storage

        return storage

    def resource(self, name: str, expected_type: type[Any] | None = None) -> Any:
        """Resolve an explicitly exported application resource."""

        return self._active.resource(name, expected_type)


class SkillSource(ABC):
    """Base class for bounded, authorization-aware progressive skill providers."""

    @abstractmethod
    async def list(
        self,
        context: SkillContext,
        *,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SkillPage:
        """Return only descriptors visible to the active agent and principal."""

    @abstractmethod
    async def load(
        self,
        skill_id: str,
        context: SkillContext,
        *,
        version: str | None = None,
    ) -> SkillDocument:
        """Load one authorized versioned instruction document."""

    async def load_resource(
        self,
        skill_id: str,
        path: str,
        context: SkillContext,
        *,
        version: str | None = None,
    ) -> SkillResource:
        """Load one supporting resource when this source exposes them."""

        del skill_id, path, context, version
        raise SkillResourceNotSupported("this skill source has no resources")


@dataclass(frozen=True, slots=True)
class _FilesystemSkill:
    """Keep validated metadata beside its immutable artifact location."""

    descriptor: SkillDescriptor
    directory: Path


class FilesystemSkillSource(SkillSource):
    """Serve compiler-validated skills from one agent-owned filesystem scope."""

    def __init__(self, directories: Sequence[str | Path]) -> None:
        """Validate and index static skills without retaining instruction bodies."""

        entries = tuple(_filesystem_skill(Path(path)) for path in directories)
        by_id = {item.descriptor.id: item for item in entries}
        if len(by_id) != len(entries):
            raise SkillValidationError("filesystem skill ids must be unique")
        self._entries = entries
        self._by_id = MappingProxyType(by_id)

    @property
    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        """Expose compile-time routing metadata without loading instructions."""

        return tuple(item.descriptor for item in self._entries)

    @property
    def routing_identity(self) -> tuple[tuple[SkillDescriptor, Path], ...]:
        """Distinguish equal content owned by different agent directories."""

        return tuple((item.descriptor, item.directory) for item in self._entries)

    async def list(
        self,
        context: SkillContext,
        *,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SkillPage:
        """Page a bounded local catalog in deterministic directory order."""

        _require_skill_context(context)
        size = _page_size(limit)
        offset = _filesystem_cursor(cursor)
        candidates = self.descriptors
        if query:
            needle = query.casefold()
            candidates = tuple(
                item
                for item in candidates
                if needle in item.name.casefold()
                or needle in item.description.casefold()
            )
        items = candidates[offset : offset + size]
        next_offset = offset + len(items)
        next_cursor = str(next_offset) if next_offset < len(candidates) else None
        return SkillPage(items, next_cursor)

    async def load(
        self,
        skill_id: str,
        context: SkillContext,
        *,
        version: str | None = None,
    ) -> SkillDocument:
        """Read one static manifest after validating its pinned version."""

        _require_skill_context(context)
        entry = self._entry(skill_id, version)
        contents = _read_text(entry.directory / "SKILL.md", "skill manifest")
        return SkillDocument(entry.descriptor, contents)

    async def load_resource(
        self,
        skill_id: str,
        path: str,
        context: SkillContext,
        *,
        version: str | None = None,
    ) -> SkillResource:
        """Read one regular UTF-8 file without escaping its skill directory."""

        _require_skill_context(context)
        entry = self._entry(skill_id, version)
        candidate = _contained_resource(entry.directory, path)
        return SkillResource(
            skill_id,
            entry.descriptor.version,
            path,
            _read_text(candidate, "skill resource"),
        )

    def _entry(self, skill_id: str, version: str | None) -> _FilesystemSkill:
        """Resolve one exact local identity without revealing other directories."""

        _require_text(skill_id, "skill id", maximum=_MAX_SKILL_ID_CHARS)
        entry = self._by_id.get(skill_id)
        if entry is None:
            raise SkillNotFoundError(f"skill {skill_id!r} is not available")
        # The compiler records content identity once; runtime mutation must fail
        # closed instead of serving bytes under a stale durable version.
        if _directory_version(entry.directory) != entry.descriptor.version:
            raise SkillValidationError(
                f"skill {skill_id!r} content changed after compilation"
            )
        if version is not None and version != entry.descriptor.version:
            raise SkillNotFoundError(
                f"skill {skill_id!r} version is not available"
            )
        return entry


class SkillScope:
    """Compose named sources for one compiled agent or subagent identity."""

    def __init__(self, sources: Mapping[str, SkillSource] | None = None) -> None:
        """Freeze validated source routes for one compiled agent identity."""

        normalized = dict(sources or {})
        for name, source in normalized.items():
            _validate_source_name(name)
            if not isinstance(source, SkillSource):
                raise TypeError(f"skill source {name!r} must inherit SkillSource")
        self._sources = MappingProxyType(normalized)

    @property
    def source_names(self) -> tuple[str, ...]:
        """Return stable source identities without exposing provider objects."""

        return tuple(self._sources)

    @property
    def routing_identity(self) -> tuple[Any, ...]:
        """Compare repeated compiler composition without equating remote clients."""

        return tuple(
            (
                name,
                source.routing_identity
                if isinstance(source, FilesystemSkillSource)
                else id(source),
            )
            for name, source in self._sources.items()
        )

    async def list(
        self,
        context: SkillContext,
        *,
        source: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SkillCatalogPage:
        """List one source page or merge first pages without per-skill queries."""

        _require_skill_context(context)
        size = _page_size(limit)
        _optional_text(query, "skill query", maximum=512)
        _optional_text(cursor, "skill page cursor", maximum=1024)
        if source is not None:
            page = await self._list_one(source, context, query, cursor, size)
            return _catalog_page(((source, page),), size)
        if cursor is not None:
            raise ValueError("a skill cursor requires an explicit source")
        pages = await asyncio.gather(
            *(
                self._list_one(name, context, query, None, size)
                for name in self._sources
            )
        )
        return _catalog_page(tuple(zip(self._sources, pages)), size)

    async def load(
        self,
        skill_id: str,
        context: SkillContext,
        *,
        source: str | None = None,
        version: str | None = None,
    ) -> SkillDocument:
        """Load one document through an explicit or unambiguous source."""

        _require_skill_context(context)
        _require_text(skill_id, "skill id", maximum=_MAX_SKILL_ID_CHARS)
        _optional_text(version, "skill version", maximum=256)
        selected = self._selected_source(source)
        provider = self._sources[selected]
        try:
            value = await provider.load(skill_id, context, version=version)
        except SkillError:
            raise
        except Exception as error:
            raise _source_failure(selected, "load", error) from None
        if not isinstance(value, SkillDocument):
            raise SkillSourceExecutionError(
                f"skill source {selected!r} load returned {type(value).__name__}"
            )
        if value.descriptor.id != skill_id:
            raise SkillSourceExecutionError(
                f"skill source {selected!r} returned a different skill id"
            )
        if version is not None and value.descriptor.version != version:
            raise SkillSourceExecutionError(
                f"skill source {selected!r} returned a different skill version"
            )
        return value

    async def load_resource(
        self,
        skill_id: str,
        path: str,
        context: SkillContext,
        *,
        source: str | None = None,
        version: str | None = None,
    ) -> SkillResource:
        """Load one bounded resource through an exact source route."""

        _require_skill_context(context)
        _require_text(skill_id, "skill id", maximum=_MAX_SKILL_ID_CHARS)
        _require_text(path, "skill resource path", maximum=1024)
        _optional_text(version, "skill version", maximum=256)
        selected = self._selected_source(source)
        provider = self._sources[selected]
        try:
            value = await provider.load_resource(
                skill_id, path, context, version=version
            )
        except SkillError:
            raise
        except Exception as error:
            raise _source_failure(selected, "load_resource", error) from None
        if not isinstance(value, SkillResource):
            raise SkillSourceExecutionError(
                f"skill source {selected!r} load_resource returned "
                f"{type(value).__name__}"
            )
        if value.skill_id != skill_id:
            raise SkillSourceExecutionError(
                f"skill source {selected!r} returned a different skill id"
            )
        if version is not None and value.version != version:
            raise SkillSourceExecutionError(
                f"skill source {selected!r} returned a different skill version"
            )
        return value

    async def _list_one(
        self,
        source: str,
        context: SkillContext,
        query: str | None,
        cursor: str | None,
        limit: int,
    ) -> SkillPage:
        """Call one catalog boundary while redacting authored failure details."""

        provider = self._source(source)
        try:
            page = await provider.list(
                context, query=query, cursor=cursor, limit=limit
            )
        except SkillError:
            raise
        except Exception as error:
            raise _source_failure(source, "list", error) from None
        if not isinstance(page, SkillPage):
            raise SkillSourceExecutionError(
                f"skill source {source!r} list returned {type(page).__name__}"
            )
        if len(page.items) > limit:
            raise SkillSourceExecutionError(
                f"skill source {source!r} exceeded the requested page size"
            )
        return page

    def _selected_source(self, source: str | None) -> str:
        """Preserve filesystem shorthand while rejecting ambiguous remote loads."""

        if source is not None:
            self._source(source)
            return source
        if _FILESYSTEM_SOURCE in self._sources:
            return _FILESYSTEM_SOURCE
        if len(self._sources) == 1:
            return next(iter(self._sources))
        available = ", ".join(self._sources)
        raise SkillNotFoundError(
            "skill source is required; available sources: " + available
        )

    def selected_source(self, source: str | None = None) -> str:
        """Resolve the route used by context access and model-tool shorthand."""

        return self._selected_source(source)

    def _source(self, name: str) -> SkillSource:
        """Resolve a named source without making unknown identities enumerable."""

        _validate_source_name(name)
        source = self._sources.get(name)
        if source is None:
            raise SkillNotFoundError(f"skill source {name!r} is not available")
        return source


class SkillRegistry:
    """Route invocation-scoped access to each compiled agent's skill sources."""

    def __init__(self, scopes: Mapping[str, SkillScope] | None = None) -> None:
        """Freeze each agent-owned scope for safe invocation lookup."""

        normalized = dict(scopes or {})
        for name, scope in normalized.items():
            _require_text(name, "agent name", maximum=128)
            if not isinstance(scope, SkillScope):
                raise TypeError("skill registry scopes must be SkillScope values")
        self._scopes = MappingProxyType(normalized)

    @property
    def agent_names(self) -> tuple[str, ...]:
        """Return compiled identities for diagnostics without exposing sources."""

        return tuple(self._scopes)

    def access(self, active: Any) -> "SkillAccess":
        """Bind a registry view to one revocable invocation context."""

        active._require_active()
        scope = self._scopes.get(active.agent_name)
        if scope is None:
            raise SkillNotFoundError(
                f"skills are not configured for agent {active.agent_name!r}"
            )
        return SkillAccess(scope, active)


class SkillAccess:
    """Public `context.skills` operations bound to the current agent identity."""

    def __init__(self, scope: SkillScope, active: Any) -> None:
        """Retain a revocable context rather than copying invocation authority."""

        self._scope = scope
        self._active = active

    async def list(
        self,
        *,
        source: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SkillCatalogPage:
        """List metadata visible to this invocation without loading bodies."""

        return await self._scope.list(
            SkillContext(self._active),
            source=source,
            query=query,
            cursor=cursor,
            limit=limit,
        )

    async def load(
        self,
        skill_id: str,
        *,
        source: str | None = None,
        version: str | None = None,
    ) -> SkillDocument:
        """Load one selected skill through this invocation's governed scope."""

        selected = self._scope.selected_source(source)
        pinned = self._pinned_version(selected, skill_id, version)
        document = await self._scope.load(
            skill_id,
            SkillContext(self._active),
            source=selected,
            version=pinned,
        )
        self._pin(selected, skill_id, document.descriptor.version)
        return document

    async def load_resource(
        self,
        skill_id: str,
        path: str,
        *,
        source: str | None = None,
        version: str | None = None,
    ) -> SkillResource:
        """Load one resource through this invocation's governed scope."""

        selected = self._scope.selected_source(source)
        pinned = self._pinned_version(selected, skill_id, version)
        resource = await self._scope.load_resource(
            skill_id,
            path,
            SkillContext(self._active),
            source=selected,
            version=pinned,
        )
        self._pin(selected, skill_id, resource.version)
        return resource

    def _pinned_version(
        self, source: str, skill_id: str, requested: str | None
    ) -> str | None:
        """Keep one source version stable throughout an invocation and its children."""

        self._active._require_active()
        key = (self._active.agent_name, source, skill_id)
        pinned = self._active._skill_pins.get(key)
        if pinned is not None and requested is not None and requested != pinned:
            raise SkillValidationError(
                "a skill version cannot change during an active invocation"
            )
        return pinned if pinned is not None else requested

    def _pin(self, source: str, skill_id: str, version: str) -> None:
        """Record only immutable routing metadata, never loaded skill content."""

        key = (self._active.agent_name, source, skill_id)
        existing = self._active._skill_pins.setdefault(key, version)
        if existing != version:
            raise SkillSourceExecutionError(
                "a skill source changed version during an active invocation"
            )


def create_skill_tools(scope: SkillScope) -> tuple[Any, ...]:
    """Create the three stable model tools over one shared source scope."""

    if not isinstance(scope, SkillScope):
        raise TypeError("skill tools require SkillScope")

    @tool
    async def list_skills(
        source: str = "",
        query: str = "",
        cursor: str = "",
        limit: int = 50,
    ) -> str:
        """List available skill names, descriptions, sources, IDs, and versions."""

        active = _active_context()
        page = await SkillAccess(scope, active).list(
            source=source or None,
            query=query or None,
            cursor=cursor or None,
            limit=limit,
        )
        return json.dumps(page.as_dict())

    @tool
    async def load_skill(
        name: str,
        source: str = "",
        version: str = "",
    ) -> str:
        """Load the full instructions for one selected skill ID and version."""

        active = _active_context()
        document = await SkillAccess(scope, active).load(
            name,
            source=source or None,
            version=version or None,
        )
        return document.instructions

    @tool
    async def load_skill_resource(
        name: str,
        path: str,
        source: str = "",
        version: str = "",
    ) -> str:
        """Load one UTF-8 supporting file from a selected skill version."""

        active = _active_context()
        resource = await SkillAccess(scope, active).load_resource(
            name,
            path,
            source=source or None,
            version=version or None,
        )
        return resource.content

    return list_skills, load_skill, load_skill_resource


def filesystem_skill_source(
    directories: Sequence[str | Path],
) -> FilesystemSkillSource:
    """Construct the built-in source through the same public validation boundary."""

    return FilesystemSkillSource(directories)


def scoped_skill_sources(
    dynamic: Mapping[str, SkillSource],
    filesystem: FilesystemSkillSource | None,
) -> SkillScope:
    """Compose dynamic sources after static ownership without name ambiguity."""

    sources: dict[str, SkillSource] = {}
    if filesystem is not None:
        sources[_FILESYSTEM_SOURCE] = filesystem
    for name, source in dynamic.items():
        if name == _FILESYSTEM_SOURCE:
            # The reserved route preserves source-free loads for repository
            # skills and cannot change meaning when a folder is temporarily empty.
            raise SkillValidationError(
                "dynamic skill source name 'filesystem' is reserved"
            )
        sources[name] = source
    return SkillScope(sources)


def _active_context() -> Any:
    """Resolve model-tool authority lazily to avoid compiler-time context access."""

    from .context import context

    return context.current()


def _catalog_page(
    pages: Sequence[tuple[str, SkillPage]], limit: int
) -> SkillCatalogPage:
    """Merge bounded pages while retaining source-specific continuation cursors."""

    all_items = tuple(
        CatalogSkill(source, descriptor)
        for source, page in pages
        for descriptor in page.items
    )
    cursors = {
        source: page.next_cursor
        for source, page in pages
        if page.next_cursor is not None
    }
    return SkillCatalogPage(
        all_items[:limit],
        cursors,
        truncated=len(all_items) > limit,
    )


def _source_failure(source: str, operation: str, error: Exception) -> Exception:
    """Redact provider exception messages that may contain credentials or payloads."""

    return SkillSourceExecutionError(
        f"skill source {source!r} {operation} failed with {type(error).__name__}"
    )


def _filesystem_skill(directory: Path) -> _FilesystemSkill:
    """Validate one static skill and derive a content-addressed version."""

    _validate_filesystem_directory(directory)
    manifest = directory / "SKILL.md"
    contents = _read_text(manifest, "skill manifest")
    frontmatter = _frontmatter(contents, manifest)
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or name != directory.name:
        raise SkillValidationError(
            f"skill frontmatter name must match directory {directory.name!r}: {manifest}"
        )
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError(
            f"skill frontmatter description must be a non-empty string: {manifest}"
        )
    descriptor = SkillDescriptor(
        id=name,
        name=name,
        description=description.strip(),
        version=_directory_version(directory),
    )
    return _FilesystemSkill(descriptor, directory.resolve())


def _validate_filesystem_directory(directory: Path) -> None:
    """Apply the static source boundary even when used outside the compiler."""

    if directory.is_symlink() or not directory.is_dir():
        raise SkillValidationError(
            f"filesystem skill must be a regular directory: {directory}"
        )
    manifest = directory / "SKILL.md"
    if manifest.is_symlink() or not manifest.is_file():
        raise SkillValidationError(
            f"filesystem skill must contain uppercase SKILL.md: {directory}"
        )
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise SkillValidationError(f"skill resource cannot be a symlink: {path}")


def _frontmatter(contents: str, manifest: Path) -> Mapping[str, Any]:
    """Decode one explicit YAML frontmatter block without retaining its body."""

    lines = contents.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError(f"skill must start with YAML frontmatter: {manifest}")
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise SkillValidationError(f"skill frontmatter is not closed: {manifest}")
    try:
        value = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as error:
        raise SkillValidationError(
            f"skill frontmatter is invalid YAML: {type(error).__name__}: {manifest}"
        ) from error
    if not isinstance(value, Mapping):
        raise SkillValidationError(f"skill frontmatter must be a mapping: {manifest}")
    return value


def _directory_version(directory: Path) -> str:
    """Hash paths and bytes so durable loads can address immutable static content."""

    digest = hashlib.sha256()
    files = tuple(
        path
        for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    )
    for path in files:
        if path.is_symlink():
            raise SkillValidationError(f"skill resource cannot be a symlink: {path}")
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _contained_resource(directory: Path, path: str) -> Path:
    """Resolve a relative resource without permitting symlink or traversal escape."""

    _require_text(path, "skill resource path", maximum=1024)
    relative = Path(path)
    if relative.is_absolute():
        raise SkillValidationError("skill resource path must be relative")
    root = directory.resolve()
    unresolved = root / relative
    if any(part.is_symlink() for part in _resource_ancestry(root, unresolved)):
        raise SkillValidationError("skill resource path contains a symlink")
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SkillValidationError(
            "skill resource path escapes its skill directory"
        ) from error
    if candidate.is_symlink() or not candidate.is_file():
        raise SkillNotFoundError(f"skill resource does not exist: {path}")
    return candidate


def _resource_ancestry(root: Path, candidate: Path) -> tuple[Path, ...]:
    """Return only path components below a validated skill root."""

    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return (candidate,)
    current = root
    paths = []
    for component in relative.parts:
        current = current / component
        paths.append(current)
    return tuple(paths)


def _read_text(path: Path, kind: str) -> str:
    """Read one bounded regular UTF-8 file with source-safe diagnostics."""

    if path.is_symlink() or not path.is_file():
        raise SkillValidationError(f"{kind} must be a regular file: {path}")
    try:
        if path.stat().st_size > _MAX_TEXT_RESOURCE_BYTES:
            raise SkillValidationError(
                f"{kind} exceeds {_MAX_TEXT_RESOURCE_BYTES} bytes: {path}"
            )
        return path.read_text(encoding="utf-8")
    except SkillError:
        raise
    except UnicodeDecodeError as error:
        raise SkillValidationError(f"{kind} must be UTF-8 text: {path}") from error
    except OSError as error:
        raise SkillValidationError(
            f"unable to read {kind} with {type(error).__name__}: {path}"
        ) from error


def _filesystem_cursor(cursor: str | None) -> int:
    """Decode the local source's opaque non-negative offset cursor."""

    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise SkillValidationError("filesystem skill cursor is invalid")
    return int(cursor)


def _page_size(limit: int) -> int:
    """Apply one bounded catalog page policy across all source adapters."""

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise TypeError("skill page limit must be an integer")
    if limit < 1 or limit > _MAX_PAGE_SIZE:
        raise ValueError(f"skill page limit must be between 1 and {_MAX_PAGE_SIZE}")
    return limit


def _require_skill_context(value: Any) -> None:
    """Reject unscoped source calls and contexts retained after invocation."""

    if not isinstance(value, SkillContext):
        raise TypeError("skill operations require SkillContext")
    value._active._require_active()


def _validate_source_name(name: Any) -> None:
    """Keep source names portable across decorators, JSON, and tool arguments."""

    if not isinstance(name, str) or not _SOURCE_NAME.fullmatch(name):
        raise ValueError("skill source name must be a valid source identifier")


def _require_text(value: Any, label: str, *, maximum: int) -> None:
    """Bound public values before they enter prompts, errors, or routing keys."""

    if not isinstance(value, str) or not value.strip():
        raise SkillValidationError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise SkillValidationError(f"{label} must be at most {maximum} characters")


def _optional_text(value: Any, label: str, *, maximum: int) -> None:
    """Validate an optional source query or version without coercing payloads."""

    if value is not None:
        _require_text(value, label, maximum=maximum)


__all__ = [
    "CatalogSkill",
    "FilesystemSkillSource",
    "SkillAccess",
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
    "SkillScope",
    "SkillSource",
    "SkillSourceExecutionError",
    "SkillValidationError",
]

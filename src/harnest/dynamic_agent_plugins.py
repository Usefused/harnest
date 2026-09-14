"""Session-scoped loading for immutable portable Agent Plugin snapshots."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
import hashlib
import io
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any, Callable
import zipfile

from .agent import AgentDefinition
from .agent_plugin_loader import discover_portable_plugin
from .plugin import PluginResources
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from .session import _DYNAMIC_AGENT_PLUGINS_APPLICATION_KEY
from .skills import create_skill_tools


MAX_PLUGIN_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_PLUGIN_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_PLUGIN_FILES = 256
MAX_PLUGIN_FILE_BYTES = 4 * 1024 * 1024
MAX_SESSION_PLUGINS = 16
_MEDIA_TYPE = "application/zip"
_SESSION_PLUGIN_KEY = _DYNAMIC_AGENT_PLUGINS_APPLICATION_KEY
_SKILL_TOOL_NAMES = {
    "list_skills",
    "load_skill",
    "load_skill_resource",
    "run_skill_script",
}


class DynamicAgentPluginError(ValueError):
    """A session plugin snapshot violates the bounded runtime contract."""


@dataclass(frozen=True, slots=True)
class SessionPluginSnapshot:
    """One extracted package and its content-addressed runtime resources."""

    name: str
    digest: str
    resources: PluginResources


class DynamicAgentPluginRuntimeDriver(RuntimeDriver):
    """Route each fixed plugin set through an isolated managed backend target."""

    def __init__(self, driver: RuntimeDriver, application: Any) -> None:
        """Retain one base backend and defer package extraction until session create."""

        self._driver = driver
        self._application = application
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._root: Path | None = None
        self._sessions: dict[tuple[str, str], tuple[str, ...]] = {}
        self._snapshots: dict[str, SessionPluginSnapshot] = {}
        self._variants: dict[tuple[str, ...], RuntimeDriver] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def info(self) -> AgentInfo:
        return self._driver.info

    @property
    def session_context_store(self) -> Any | None:
        """Preserve the base backend's portable session lease authority."""

        return getattr(self._driver, "session_context_store", None)

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        """Create a session without dynamically supplied plugin capabilities."""

        return await self.create_session_with_plugins(
            session_id=session_id, user_id=user_id, state=state, plugins=()
        )

    async def create_session_with_plugins(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
        plugins: Sequence[Mapping[str, Any]],
    ) -> SessionRecord:
        """Validate plugins before creating a session and fix them for its lifetime."""

        self._ensure_open()
        snapshots = await self._load_snapshots(plugins)
        if snapshots and self.session_context_store is None:
            raise DynamicAgentPluginError(
                "dynamic Agent Plugins require Harnest session storage"
            )
        key = (user_id, session_id)
        try:
            session = await self._driver.create_session(
                session_id=session_id, user_id=user_id, state=state
            )
            await self._persist_plugins(session, plugins)
        except BaseException:
            if key in self._sessions:
                self._sessions.pop(key, None)
            raise
        self._sessions[key] = tuple(snapshot.digest for snapshot in snapshots)
        return _with_plugin_metadata(session, snapshots)

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None:
        session = await self._driver.get_session(
            session_id=session_id, user_id=user_id
        )
        if session is not None:
            await self._restore_plugins(session)
        return self._decorate(session, user_id=user_id, session_id=session_id)

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> Sequence[SessionRecord]:
        """Decorate a backend-owned page without changing its pagination order."""

        sessions = await self._driver.list_sessions(
            user_id=user_id, after=after, limit=limit
        )
        for session in sessions:
            await self._restore_plugins(session)
        return tuple(
            self._decorate(item, user_id=user_id, session_id=item.id) or item
            for item in sessions
        )

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> Sequence[SessionMessage] | None:
        return await self._driver.get_session_messages(
            session_id=session_id, user_id=user_id
        )

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        session = await self._driver.update_session(
            session_id=session_id, user_id=user_id, state_delta=state_delta
        )
        return self._decorate(session, user_id=user_id, session_id=session_id)

    async def delete_session(self, *, session_id: str, user_id: str) -> bool:
        deleted = await self._driver.delete_session(
            session_id=session_id, user_id=user_id
        )
        if deleted:
            self._sessions.pop((user_id, session_id), None)
        return deleted

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        await self._restore_session_plugins(request.user_id, request.session_id)
        driver = await self._driver_for(request.user_id, request.session_id)
        return await driver.invoke(request)

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[RuntimeEvent]:
        await self._restore_session_plugins(request.user_id, request.session_id)
        driver = await self._driver_for(request.user_id, request.session_id)
        async for event in driver.stream(request):
            yield event

    async def close(self) -> None:
        """Close dynamic backend variants before releasing their package roots."""

        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        for driver in reversed(tuple(self._variants.values())):
            try:
                await driver.close()
            except BaseException as error:
                if failure is None:
                    failure = error
        try:
            await self._driver.close()
        except BaseException as error:
            if failure is None:
                failure = error
        if self._temporary is not None:
            self._temporary.cleanup()
        if failure is not None:
            raise failure

    async def _load_snapshots(
        self, plugins: Sequence[Mapping[str, Any]]
    ) -> tuple[SessionPluginSnapshot, ...]:
        """Materialize each unique package once and reject ambiguous identities."""

        if isinstance(plugins, (str, bytes)) or not isinstance(plugins, Sequence):
            raise DynamicAgentPluginError("plugins must be a list")
        if len(plugins) > MAX_SESSION_PLUGINS:
            raise DynamicAgentPluginError("a session may contain at most 16 plugins")
        # Extraction and cache publication share one lock so concurrent session
        # creation cannot observe a partially materialized content digest.
        async with self._lock:
            snapshots = tuple(self._load_snapshot(item) for item in plugins)
        names = [item.name.casefold() for item in snapshots]
        if len(names) != len(set(names)):
            raise DynamicAgentPluginError("session plugins must have unique names")
        return snapshots

    def _load_snapshot(self, value: Mapping[str, Any]) -> SessionPluginSnapshot:
        """Decode one OpenAI-compatible inline ZIP descriptor by content digest."""

        archive, expected_name, expected_digest = _inline_archive(value)
        digest = hashlib.sha256(archive).hexdigest()
        if expected_digest is not None and expected_digest != digest:
            raise DynamicAgentPluginError("plugin sha256 does not match its ZIP data")
        cached = self._snapshots.get(digest)
        if cached is not None:
            if cached.name != expected_name:
                raise DynamicAgentPluginError("plugin name does not match cached content")
            return cached
        destination = self._snapshot_root() / digest
        try:
            _extract_archive(archive, destination)
            resources = _validated_plugin_resources(destination, expected_name)
        except DynamicAgentPluginError:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        except OSError as error:
            shutil.rmtree(destination, ignore_errors=True)
            raise DynamicAgentPluginError(
                "plugin ZIP does not contain a valid Agent Plugin"
            ) from error
        snapshot = SessionPluginSnapshot(resources.name, digest, resources)
        self._snapshots[digest] = snapshot
        return snapshot

    def _snapshot_root(self) -> Path:
        """Create private extraction storage only when a session supplies a plugin."""

        if self._root is None:
            self._temporary = tempfile.TemporaryDirectory(
                prefix="harnest-session-plugins-"
            )
            self._root = Path(self._temporary.name).resolve()
        return self._root

    async def _driver_for(self, user_id: str, session_id: str) -> RuntimeDriver:
        """Build one backend variant per immutable ordered plugin set."""

        digests = self._sessions.get((user_id, session_id), ())
        if not digests:
            return self._driver
        existing = self._variants.get(digests)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._variants.get(digests)
            if existing is not None:
                return existing
            snapshots = tuple(self._snapshots[digest] for digest in digests)
            variant = _plugin_application(self._application, snapshots)
            fork = getattr(self._driver, "fork_application", None)
            if not callable(fork):
                raise DynamicAgentPluginError(
                    "runtime backend cannot create dynamic plugin variants"
                )
            existing = fork(variant)
            self._variants[digests] = existing
            return existing

    def _decorate(
        self, session: SessionRecord | None, *, user_id: str, session_id: str
    ) -> SessionRecord | None:
        """Project only names and digests; archive bytes never enter session JSON."""

        if session is None:
            return None
        snapshots = tuple(
            self._snapshots[digest]
            for digest in self._sessions.get((user_id, session_id), ())
        )
        return _with_plugin_metadata(_without_private_data(session), snapshots)

    async def _persist_plugins(
        self,
        session: SessionRecord,
        plugins: Sequence[Mapping[str, Any]],
    ) -> None:
        """Persist validated snapshots with application data for restart recovery."""

        if not plugins:
            return
        store = self.session_context_store
        if store is None:  # pragma: no cover - rejected before session creation
            raise DynamicAgentPluginError(
                "dynamic Agent Plugins require Harnest session storage"
            )
        from ._json import json_value

        application_data = dict(session.application_data)
        application_data[_SESSION_PLUGIN_KEY] = json_value(plugins)
        try:
            async with store.acquire(
                session_id=session.id, user_id=session.user_id
            ) as lease:
                await lease.replace_application_data(application_data)
        except BaseException as failure:
            _audit_plugin_attachment(self.info.framework, "failed")
            # Session and plugin binding are one logical mutation. Remove the
            # new session when its immutable environment cannot be committed.
            try:
                await self._driver.delete_session(
                    session_id=session.id, user_id=session.user_id
                )
            except BaseException as cleanup_error:
                from ._exception_notes import add_exception_note

                add_exception_note(
                    failure,
                    "dynamic plugin session rollback also failed with "
                    f"{type(cleanup_error).__name__}",
                )
            raise
        _audit_plugin_attachment(self.info.framework, "committed")

    async def _restore_session_plugins(self, user_id: str, session_id: str) -> None:
        """Recover a durable plugin environment before routing an invocation."""

        key = (user_id, session_id)
        if key in self._sessions:
            return
        session = await self._driver.get_session(
            session_id=session_id, user_id=user_id
        )
        if session is not None:
            await self._restore_plugins(session)

    async def _restore_plugins(self, session: SessionRecord) -> None:
        """Rebuild process-local snapshots from private durable session data."""

        key = (session.user_id, session.id)
        if key in self._sessions:
            return
        stored = session.application_data.get(_SESSION_PLUGIN_KEY, ())
        snapshots = await self._load_snapshots(stored)
        self._sessions.setdefault(
            key, tuple(snapshot.digest for snapshot in snapshots)
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("dynamic Agent Plugin runtime is closed")


def _inline_archive(
    value: Mapping[str, Any],
) -> tuple[bytes, str, str | None]:
    """Validate one inline descriptor without accepting a server-side URL source."""

    name, source = _descriptor_fields(value)
    archive = _decode_archive_source(source)
    return archive, name, _expected_digest(value.get("sha256"))


def _descriptor_fields(
    value: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    """Select the fixed descriptor fields while rejecting URL-shaped variants."""

    allowed = {"type", "name", "description", "source", "sha256"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise DynamicAgentPluginError("invalid inline plugin descriptor")
    if value.get("type") != "inline":
        raise DynamicAgentPluginError("plugin type must be inline")
    name, source = value.get("name"), value.get("source")
    if not isinstance(name, str) or not name:
        raise DynamicAgentPluginError("plugin name must be non-empty")
    if not isinstance(source, Mapping):
        raise DynamicAgentPluginError("plugin source must be a base64 ZIP")
    return name, source


def _decode_archive_source(source: Mapping[str, Any]) -> bytes:
    """Decode only the OpenAI-style base64 ZIP source representation."""

    if set(source) != {"type", "media_type", "data"}:
        raise DynamicAgentPluginError("plugin source must be a base64 ZIP")
    if source.get("type") != "base64" or source.get("media_type") != _MEDIA_TYPE:
        raise DynamicAgentPluginError(
            "plugin source must use application/zip base64"
        )
    data = source.get("data")
    if not isinstance(data, str):
        raise DynamicAgentPluginError("plugin source data must be a base64 string")
    try:
        archive = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as error:
        raise DynamicAgentPluginError("plugin source data is invalid base64") from error
    if not archive or len(archive) > MAX_PLUGIN_ARCHIVE_BYTES:
        raise DynamicAgentPluginError("plugin ZIP must be between 1 byte and 8 MiB")
    return archive


def _expected_digest(value: Any) -> str | None:
    """Validate an optional client-computed lowercase SHA-256 digest."""

    valid = (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
    if value is not None and not valid:
        raise DynamicAgentPluginError("plugin sha256 must be 64 lowercase hex characters")
    return value


def _validated_plugin_resources(
    destination: Path, expected_name: str
) -> PluginResources:
    """Apply standard discovery and the dynamic no-process execution policy."""

    resources = discover_portable_plugin(destination)
    if resources is None:
        raise DynamicAgentPluginError("plugin ZIP has no valid root plugin.json")
    if resources.name != expected_name:
        raise DynamicAgentPluginError("plugin name does not match plugin.json")
    if any(client.transport == "stdio" for client in resources.mcp_clients):
        raise DynamicAgentPluginError(
            "dynamic stdio MCP servers require a configured sandbox"
        )
    return resources


def _extract_archive(archive: bytes, destination: Path) -> None:
    """Extract a bounded regular-file-only ZIP without path or link traversal."""

    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            files = _validated_members(package.infolist())
            for member, relative in files:
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with package.open(member) as source, target.open("xb") as output:
                    _copy_member(source, output, member.file_size)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise DynamicAgentPluginError("plugin source must be a safe ZIP archive") from error


def _validated_members(
    members: Sequence[zipfile.ZipInfo],
) -> tuple[tuple[zipfile.ZipInfo, PurePosixPath], ...]:
    """Apply archive-wide count, size, path, collision, and file-type bounds."""

    files: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    expanded = 0
    seen: set[str] = set()
    for member in members:
        relative = _member_path(member)
        if member.is_dir():
            continue
        expanded += member.file_size
        if member.file_size > MAX_PLUGIN_FILE_BYTES:
            raise DynamicAgentPluginError("plugin ZIP contains a file larger than 4 MiB")
        if expanded > MAX_PLUGIN_EXPANDED_BYTES:
            raise DynamicAgentPluginError("plugin ZIP expands beyond 32 MiB")
        key = relative.as_posix().casefold()
        if key in seen:
            raise DynamicAgentPluginError("plugin ZIP contains colliding paths")
        seen.add(key)
        files.append((member, relative))
        if len(files) > MAX_PLUGIN_FILES:
            raise DynamicAgentPluginError("plugin ZIP contains more than 256 files")
    if not files:
        raise DynamicAgentPluginError("plugin ZIP contains no files")
    return tuple(files)


def _copy_member(source: Any, output: Any, declared_size: int) -> None:
    """Copy exactly one declared member without trusting decompressor output size."""

    copied = 0
    while chunk := source.read(64 * 1024):
        copied += len(chunk)
        if copied > declared_size or copied > MAX_PLUGIN_FILE_BYTES:
            raise DynamicAgentPluginError("plugin ZIP member exceeds its declared size")
        output.write(chunk)
    if copied != declared_size:
        raise DynamicAgentPluginError("plugin ZIP member size is inconsistent")


def _member_path(member: zipfile.ZipInfo) -> PurePosixPath:
    """Reject non-portable archive names and every non-regular Unix member."""

    name = member.filename
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or member.flag_bits & 0x1
    ):
        raise DynamicAgentPluginError("plugin ZIP contains an unsafe path")
    mode = member.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise DynamicAgentPluginError("plugin ZIP may contain only regular files")
    return path


def _plugin_application(
    application: Any, snapshots: Sequence[SessionPluginSnapshot]
) -> Any:
    """Re-lower one managed root agent with its session's immutable resources."""

    definition = _managed_root_definition(application)
    resources = tuple(snapshot.resources for snapshot in snapshots)
    clients = tuple(client for item in resources for client in item.mcp_clients)
    skill_directories = tuple(
        directory for item in resources for directory in item.skill_directories
    )
    _validate_capability_identities(definition, resources, clients)
    tools = _dynamic_skill_tools(application, definition, skill_directories)
    dynamic = replace(
        definition,
        tools=tools,
        mcp=(*definition.mcp, *clients),
    )
    from .backends import get_backend

    backend = get_backend(application.framework)
    target = backend.lower_managed(
        dynamic,
        native_extensions=application.native_extensions,
        checkpointer=application.native_checkpointer,
    )
    native_app = backend.wrap_managed(
        target, native_extensions=application.native_extensions
    )
    return replace(
        application,
        target=target,
        native_app=native_app,
        managed_definition=dynamic,
    )


def _managed_root_definition(application: Any) -> AgentDefinition:
    """Require the composition boundary that Harnest can safely re-lower."""

    definition = application.managed_definition
    supported = (
        application.mode == "managed"
        and application.kind == "agent"
        and isinstance(definition, AgentDefinition)
    )
    if not supported:
        raise DynamicAgentPluginError(
            "dynamic Agent Plugins require a managed root Agent"
        )
    return definition


def _dynamic_skill_tools(
    application: Any,
    definition: AgentDefinition,
    directories: Sequence[Path],
) -> tuple[Any, ...]:
    """Replace the one stable skill tool surface with a combined session scope."""

    tools = tuple(
        tool
        for tool in definition.tools
        if getattr(tool, "__name__", "") not in _SKILL_TOOL_NAMES
    )
    scope = application.skill_registry.scope(application.name)
    scope = scope.with_filesystem_directories(directories)
    return (*tools, *create_skill_tools(scope)) if scope.source_names else tools


def _validate_capability_identities(
    definition: AgentDefinition,
    resources: Sequence[PluginResources],
    clients: Sequence[Any],
) -> None:
    """Reject package, MCP, and skill collisions before backend construction."""

    static_clients = tuple(definition.mcp)
    identities = [
        client.identity or client.tool_name_prefix or client.capability_id
        for client in (*static_clients, *clients)
    ]
    if len(identities) != len(set(identities)):
        raise DynamicAgentPluginError("dynamic plugin MCP identity conflicts with the agent")
    names = [item.name.casefold() for item in resources]
    if len(names) != len(set(names)):
        raise DynamicAgentPluginError("dynamic plugin names must be unique")


def _with_plugin_metadata(
    session: SessionRecord, snapshots: Sequence[SessionPluginSnapshot]
) -> SessionRecord:
    """Expose stable plugin identity without retaining source bytes or locations."""

    if not snapshots:
        return session
    metadata = dict(session.metadata)
    metadata["plugins"] = [
        {"name": item.name, "sha256": item.digest} for item in snapshots
    ]
    return replace(session, metadata=metadata)


def _without_private_data(session: SessionRecord) -> SessionRecord:
    """Remove archive bytes from every public session representation."""

    if _SESSION_PLUGIN_KEY not in session.application_data:
        return session
    application_data = dict(session.application_data)
    application_data.pop(_SESSION_PLUGIN_KEY, None)
    return replace(session, application_data=application_data)


def _audit_plugin_attachment(framework: str | None, outcome: str) -> None:
    """Emit a payload-free audit signal after the durable attachment attempt."""

    from .logging import get_logger

    event = "session.dynamic_plugins.attach"
    get_logger("session.audit").info(
        event,
        operation=event,
        trigger="user",
        outcome=outcome,
        framework=framework or "unknown",
    )


async def forward_session_plugins(
    driver: RuntimeDriver,
    *,
    session_id: str,
    user_id: str,
    state: Mapping[str, Any],
    plugins: Sequence[Mapping[str, Any]],
) -> SessionRecord:
    """Forward the optional capability through wrappers or reject unsupported roots."""

    creator: Callable[..., Any] | None = getattr(
        driver, "create_session_with_plugins", None
    )
    if not callable(creator):
        raise DynamicAgentPluginError(
            "dynamic Agent Plugins require a managed root Agent"
        )
    return await creator(
        session_id=session_id,
        user_id=user_id,
        state=state,
        plugins=plugins,
    )


__all__ = [
    "DynamicAgentPluginError",
    "DynamicAgentPluginRuntimeDriver",
    "forward_session_plugins",
    "MAX_PLUGIN_ARCHIVE_BYTES",
    "MAX_PLUGIN_EXPANDED_BYTES",
    "MAX_PLUGIN_FILE_BYTES",
    "MAX_PLUGIN_FILES",
    "MAX_SESSION_PLUGINS",
    "SessionPluginSnapshot",
]

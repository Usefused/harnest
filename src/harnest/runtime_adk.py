"""Google ADK driver for the framework-neutral Harnest runtime.

This module is intentionally limited to translating between the neutral runtime
contract and ADK.  HTTP validation, deadlines, concurrency limits, and public
wire formats belong to :mod:`harnest.neutral_runtime`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from ._json import json_value
from .application import CompiledApplication
from .assets import AssetScope, AssetStore, AssetURLStorage
from .asset_inspection import inspect_asset
from .checkpoint import CheckpointRecord, CheckpointStore, HarnestStore, RunScope
from .client_tool import current_transient_media
from .durable import NativeDurableSuspended, NativeResumeInput
from .model_lifecycle import close_litellm_lifecycles
from .mcp_lifecycle import (
    close_mcp_lifecycles,
    mcp_lifecycle_bindings,
    start_mcp_lifecycles,
)
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    SessionConflictError,
    SessionMessage,
    SessionRecord,
)
from .runtime_session import durable_completion_deferred
from .output import AgentMetadata, OutputPolicy, _reported_token_usage
from .structured import (
    framework_metadata_field,
    validate_runtime_output,
)
from .transient_media import (
    TransientMediaAccess,
    TransientMediaLease,
    is_transient_media_placeholder,
    matching_transient_leases,
    sanitize_transient_media,
    transient_media_lease_id,
    transient_media_placeholder,
    transient_media_placeholders,
)
from .stored_media import (
    sanitize_stored_media,
    stored_media_reference,
    stored_media_references,
    stage_stored_media,
)


_CONTENT_KINDS = frozenset(
    {"text", "image", "audio", "video", "file", "data", "asset"}
)
_CONTENT_MARKER = "harnestContent"


@dataclass(frozen=True, slots=True)
class _ADKNativeRequest:
    """Native invocation values for a fresh turn or persisted tool resume."""

    message: Any
    run_config: Any
    invocation_id: str | None
    state_delta: Mapping[str, Any] | None
    is_resume: bool


def _model_input_text(value: Any) -> str:
    """Render validated structured input for ADK's text content boundary."""

    if isinstance(value, str):
        return value
    return json.dumps(json_value(value), ensure_ascii=False)


async def _adk_input_parts(
    value: Any, stores: Mapping[str, AssetStore], types: Any
) -> list[Any]:
    """Build reference-only ADK input and retain authored content ordering."""

    payload = json_value(value)
    direct = _direct_content_sequence(payload)
    if direct is not None:
        return [await _adk_reference_part(item, stores, types) for item in direct]
    skeleton, content = _extract_content(payload)
    if not content:
        return [types.Part(text=_model_input_text(value))]
    # The structural JSON keeps field names meaningful while opaque markers
    # prevent model prompts and ADK sessions from acquiring asset identifiers.
    parts = [types.Part(text=json.dumps(skeleton, ensure_ascii=False))]
    parts.extend(
        [await _adk_reference_part(item, stores, types) for item in content]
    )
    return parts


def _direct_content_sequence(value: Any) -> list[Mapping[str, Any]] | None:
    """Recognize a root content sequence without imposing a field name."""

    candidate = value
    if isinstance(value, Mapping) and len(value) == 1:
        candidate = next(iter(value.values()))
    if isinstance(candidate, list) and candidate and all(
        _is_content_part(item) for item in candidate
    ):
        return list(candidate)
    if _is_content_part(candidate):
        return [candidate]
    return None


def _extract_content(value: Any) -> tuple[Any, list[Mapping[str, Any]]]:
    """Replace nested portable parts with ordered non-sensitive placeholders."""

    content: list[Mapping[str, Any]] = []

    def walk(item: Any) -> Any:
        if _is_content_part(item):
            content.append(item)
            return {"contentPart": len(content)}
        if isinstance(item, Mapping):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    return walk(value), content


def _is_content_part(value: Any) -> bool:
    """Identify the strict discriminated content dictionaries emitted by Pydantic."""

    return (
        isinstance(value, Mapping)
        and isinstance(value.get("type"), str)
        and value["type"] in _CONTENT_KINDS
    )


async def _adk_reference_part(
    part: Mapping[str, Any], stores: Mapping[str, AssetStore], types: Any
) -> Any:
    """Translate one portable part without reading asset bytes."""

    kind = part["type"]
    if kind == "text":
        return types.Part(text=part["text"])
    if kind == "data":
        marker = {"type": "data", "value": part.get("value")}
        return _marker_part(marker, types)
    if transient_media_lease_id(part) is not None or is_transient_media_placeholder(part):
        return _marker_part(part, types)
    asset_id = part.get("assetId")
    store_name = part.get("store", "default")
    if (
        not isinstance(asset_id, str)
        or not isinstance(store_name, str)
        or store_name not in stores
    ):
        raise RuntimeError("referenced ADK content requires an asset store")
    # Metadata is re-read from the scoped store at the model boundary. The
    # request marker deliberately contains no client-claimed MIME or size data.
    marker = {"type": kind, "assetId": asset_id}
    if "store" in part:
        marker["store"] = store_name
    policy = part.get("harnestStored")
    if policy is not None:
        marker["harnestStored"] = policy
    return _marker_part(marker, types)


def _marker_part(marker: Mapping[str, Any], types: Any) -> Any:
    """Create an ADK-session-safe placeholder understood by Harnest's plugin."""

    kind = marker.get("type", "asset")
    return types.Part(
        text=f"[Harnest {kind} content]",
        part_metadata={_CONTENT_MARKER: dict(marker)},
    )


def _validated_output_event(
    item: dict[str, Any], schema: Any, *, metadata: Any = None
) -> dict[str, Any]:
    """Revalidate native structured events before they reach public transports."""

    if schema is None or item.get("type") != "graph_output":
        return item
    model = validate_runtime_output(
        schema,
        item.get("output"),
        metadata=metadata,
        boundary="model output",
    )
    output = json_value(model)
    return {**item, "output": output, "result": output}


class _ADKTurnOutput:
    """Own buffering, validation, and runtime metadata for one ADK turn."""

    def __init__(self, schema: Any) -> None:
        self.schema = schema
        self.visible_text: list[str] = []
        self.has_structured_output = False
        self.structured_items: list[dict[str, Any]] = []
        self.native_events: list[Any] = []
        self.enriches_metadata = (
            schema is not None and framework_metadata_field(schema) is not None
        )

    def capture_native(self, event: Any) -> None:
        """Retain native events only when the authored result requests them."""

        if self.enriches_metadata:
            self.native_events.append(_safe_adk_value(json_value(event)))

    def accept(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Return an immediately public item or buffer runtime-owned output."""

        if self.enriches_metadata and item.get("type") == "graph_output":
            self.has_structured_output = True
            self.structured_items.append(item)
            return None
        public = _validated_output_event(item, self.schema)
        if public.get("type") == "message":
            self.visible_text.append(str(public.get("text", "")))
        elif public.get("type") == "graph_output":
            self.has_structured_output = True
        return public

    def complete(self) -> list[dict[str, Any]]:
        """Inject metadata into terminal output after every native event is known."""

        metadata = _adk_turn_metadata(self.native_events)
        items = [
            _validated_output_event(item, self.schema, metadata=metadata)
            for item in self.structured_items
        ]
        if self.schema is None or self.has_structured_output:
            return items
        model = validate_runtime_output(
            self.schema,
            "".join(self.visible_text),
            metadata=metadata,
            boundary="model output",
        )
        output = json_value(model)
        items.append({"type": "graph_output", "output": output, "result": output})
        return items


def _adk_agent_info(
    application: CompiledApplication,
    card: Mapping[str, Any] | None,
    extra_endpoints: Mapping[str, str] | None,
) -> AgentInfo:
    """Build public agent metadata without mixing it into runtime setup."""

    card_data = dict(card or {})
    description = card_data.get("description")
    if not isinstance(description, str):
        description = getattr(application.target, "description", "") or ""
    display_name = card_data.get("name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = application.name
    return AgentInfo(
        id=application.name,
        name=display_name,
        description=description,
        card=card_data,
        framework=application.framework,
        mode=application.mode,
        lifecycle_coverage=application.lifecycle_coverage.report(),
        extra_endpoints=dict(extra_endpoints or {}),
        input_schema=application.input_schema,
        output_schema=application.output_schema,
        # The application plugin manager observes managed ADK subagents too;
        # advanced roots do not expose an equivalent complete boundary.
        agent_principal_projection_complete=application.mode == "managed",
    )


def _adk_asset_stores(
    application: CompiledApplication,
    default: AssetStore | None,
    configured: Mapping[str, AssetStore] | None,
) -> dict[str, AssetStore]:
    """Merge explicit test injection with compiled named storage."""

    stores = dict(getattr(application, "asset_stores", {}))
    stores.update(configured or {})
    selected = default or getattr(application, "asset_store", None)
    if selected is not None:
        stores["default"] = selected
    return stores


class ADKRuntimeDriver(RuntimeDriver):
    """Run ADK with its development runner or an injected session service."""

    framework = "adk"

    def __init__(
        self,
        application: CompiledApplication,
        *,
        card: Mapping[str, Any] | None = None,
        extra_endpoints: Mapping[str, str] | None = None,
        session_service: Any | None = None,
        asset_store: AssetStore | None = None,
        asset_stores: Mapping[str, AssetStore] | None = None,
        plugin_manager: Any | None = None,
    ) -> None:
        """Create one runner without starting application-owned resources."""

        if application.framework != "adk":
            raise ValueError("ADKRuntimeDriver requires an ADK application")
        if application.native_app is None:
            raise ValueError("compiled ADK application does not contain an App")

        self.application = application
        self._info = _adk_agent_info(application, card, extra_endpoints)
        stores = _adk_asset_stores(application, asset_store, asset_stores)

        self._asset_stores = stores
        self._asset_store = stores.get("default")
        self._runner = _create_runner(
            application,
            session_service,
            asset_store=self._asset_store,
            asset_stores=self._asset_stores,
            plugin_manager=plugin_manager,
        )
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def app_name(self) -> str:
        return self.application.native_app.name

    @property
    def info(self) -> AgentInfo:
        return self._info

    @property
    def session_context_store(self) -> Any | None:
        """Expose only an explicitly portable store from the ADK service."""

        return getattr(self._runner.session_service, "session_context_store", None)

    async def create_session(
        self,
        *,
        user_id: str,
        session_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord:
        self._ensure_open()
        existing = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is not None:
            raise SessionConflictError(f"session already exists: {session_id}")
        try:
            session = await self._runner.session_service.create_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
                state=dict(state),
            )
        except ValueError as exc:
            # Preserve the portable conflict contract if another coroutine won
            # the race between the existence check and creation.
            raise SessionConflictError(
                f"session already exists: {session_id}"
            ) from exc
        return await _complete_session_record(
            self._runner.session_service, session
        )

    async def get_session(
        self, *, user_id: str, session_id: str
    ) -> SessionRecord | None:
        self._ensure_open()
        session = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            return None
        return await _complete_session_record(
            self._runner.session_service, session
        )

    async def list_sessions(
        self,
        *,
        user_id: str,
        after: str | None = None,
        limit: int | None = None,
    ) -> list[SessionRecord]:
        """Use Harnest store pagination or bound ADK's complete session list."""

        self._ensure_open()
        record_page = getattr(
            self._runner.session_service, "list_session_records_page", None
        )
        if callable(record_page):
            records = await record_page(
                user_id=user_id,
                after=after,
                limit=limit,
            )
            return [
                _stored_adk_session_record(item, self.app_name)
                for item in records
            ]
        paged = getattr(self._runner.session_service, "list_sessions_page", None)
        if callable(paged):
            sessions = await paged(
                app_name=self.app_name,
                user_id=user_id,
                after=after,
                limit=limit,
            )
            return [_session_record(session) for session in sessions]
        response = await self._runner.session_service.list_sessions(
            app_name=self.app_name,
            user_id=user_id,
        )
        records = sorted(
            (_session_record(session) for session in response.sessions),
            key=lambda item: item.id,
        )
        if after is not None:
            records = [item for item in records if item.id > after]
        return records if limit is None else records[:limit]

    async def get_session_messages(
        self, *, session_id: str, user_id: str
    ) -> list[SessionMessage] | None:
        """Return portable transcript items backed by complete native events."""

        self._ensure_open()
        session = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        return None if session is None else _adk_session_messages(session)

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None:
        self._ensure_open()
        session = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            return None
        try:
            from google.adk.events import Event, EventActions
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Google ADK is required to update a session") from exc
        event = Event(
            invocation_id=f"session-{uuid.uuid4().hex}",
            author="user",
            actions=EventActions(state_delta=dict(state_delta)),
        )
        await self._runner.session_service.append_event(session=session, event=event)
        updated = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if updated is None:
            return None
        return await _complete_session_record(
            self._runner.session_service, updated
        )

    async def delete_session(self, *, user_id: str, session_id: str) -> bool:
        self._ensure_open()
        existing = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is None:
            return False
        await self._runner.session_service.delete_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        return True

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Collect one native ADK event stream into a neutral result."""

        items = [item async for item in self.stream(request)]
        text = "".join(
            item["text"]
            for item in items
            if item.get("type") == "message"
            and isinstance(item.get("text"), str)
        )
        result = next(
            (
                item.get("output")
                for item in reversed(items)
                if item.get("type") == "graph_output"
            ),
            None,
        )
        return InvocationResult(
            text=text,
            events=tuple(items),
            result=result,
            session_id=request.session_id,
            metadata=dict(request.metadata),
        )

    async def stream(
        self, request: InvocationRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield privacy-safe neutral deltas from ADK's native event stream."""

        self._ensure_open()
        native_request = await self._prepare_stream_request(request)
        async with _session_execution_lease(
            self._runner.session_service,
            user_id=request.user_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
        ):
            if not native_request.is_resume:
                # The continuation claimant already moved this exact run back to
                # running. Re-beginning would confuse idempotency with a new turn.
                await self._begin_checkpoint(request)
            native_events = self._runner.run_async(
                user_id=request.user_id,
                session_id=request.session_id,
                invocation_id=native_request.invocation_id,
                new_message=native_request.message,
                state_delta=native_request.state_delta,
                run_config=native_request.run_config,
            )
            normalizer = _ADKEventNormalizer(
                self.application.output_policy,
                root_agent_name=getattr(self.application.target, "name", None),
            )
            output = _ADKTurnOutput(self.application.output_schema)
            unresolved_tools: set[str] = set()
            try:
                async with aclosing(native_events):
                    async for event in native_events:
                        # Persist before advancing the generator: its next step
                        # may enter a long-running tool and suspend this request.
                        await self._save_checkpoint_event(request, event)
                        _update_long_running_tools(unresolved_tools, event)
                        output.capture_native(event)
                        for item in normalizer.feed(event):
                            public = output.accept(item)
                            if public is not None:
                                yield public
                for item in normalizer.finish(complete_agents=not unresolved_tools):
                    public = output.accept(item)
                    if public is not None:
                        yield public
                for item in output.complete():
                    yield item
            except BaseException:
                await self._finish_checkpoint(request, "failed")
                raise
            if unresolved_tools:
                # The matching function call is now in ADK's session service.
                # Keep the portable run waiting until a replica injects its
                # FunctionResponse instead of misreporting an empty completion.
                raise NativeDurableSuspended
            await self._finish_checkpoint(request, "completed")

    async def _prepare_stream_request(
        self, request: InvocationRequest
    ) -> _ADKNativeRequest:
        """Build a fresh ADK turn or an exact persisted FunctionResponse resume."""

        try:
            from google.adk.agents.run_config import RunConfig
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Google ADK is required to run an ADK agent") from exc

        await start_mcp_lifecycles(
            mcp_lifecycle_bindings(self.application.target)
        )

        metadata = dict(request.metadata)
        run_config = RunConfig(custom_metadata=metadata) if metadata else None
        value = request.input
        if isinstance(value, NativeResumeInput):
            artifact = value.artifact
            if artifact.framework != "adk":
                raise ValueError("ADK runtime requires an ADK resume artifact")
            response = json_value(value.value)
            if not isinstance(response, Mapping):
                response = {"result": response}
            else:
                response = dict(response)
            # ADK correlates all three persisted identities. Submitting this as
            # a user FunctionResponse resumes its model loop without replaying
            # the Python tool coroutine on the claiming replica.
            message = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=artifact.tool_call_id,
                            name=artifact.tool_name,
                            response=response,
                        )
                    )
                ],
            )
            return _ADKNativeRequest(
                message=message,
                run_config=run_config,
                invocation_id=artifact.native_invocation_id,
                state_delta=None,
                is_resume=True,
            )

        access = current_transient_media()
        schema = self.application.input_schema
        if schema is not None:
            authored = value if isinstance(value, schema) else schema.model_validate(value)
            authored = await stage_stored_media(
                authored,
                stores=self._asset_stores,
                scope=AssetScope(request.user_id, request.session_id),
            )
            value = authored if access is None else access.stage(authored)
        parts = await _adk_input_parts(value, self._asset_stores, types)
        message = types.Content(role="user", parts=parts)
        # Let ADK allocate the provider invocation id for new turns; durable
        # tools capture that id from ToolContext for a later exact resume.
        return _ADKNativeRequest(
            message=message,
            run_config=run_config,
            invocation_id=None,
            state_delta=dict(request.state_delta),
            is_resume=False,
        )

    async def close(self) -> None:
        """Close ADK resources exactly once."""

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._runner.close()
            finally:
                await self._close_application_lifecycles()

    async def _close_application_lifecycles(self) -> None:
        """Close independent model and MCP resources even if one hook fails."""

        failure: BaseException | None = None
        try:
            await close_litellm_lifecycles(self.application.target)
        except BaseException as error:
            failure = error
        try:
            # The runner closes adapter-owned sessions before application-level
            # certificate, credential, and gateway resources are released.
            await close_mcp_lifecycles(
                mcp_lifecycle_bindings(self.application.target)
            )
        except BaseException as error:
            if failure is None:
                failure = error
        if failure is not None:
            raise failure

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ADK runtime driver is closed")

    async def _begin_checkpoint(self, request: InvocationRequest) -> None:
        """Claim portable run ownership before ADK begins producing events."""

        store = self._portable_checkpoints()
        if store is None:
            return
        await store.begin_run(
            application_id=self.application.name,
            user_id=request.user_id,
            session_id=request.session_id,
            run_id=request.invocation_id,
            framework="adk",
        )

    async def _save_checkpoint_event(
        self, request: InvocationRequest, event: Any
    ) -> None:
        """Persist complete ADK events while excluding unstable partial output."""

        store = self._portable_checkpoints()
        if store is None or getattr(event, "partial", False):
            return
        dump = getattr(event, "model_dump", None)
        if not callable(dump):
            return
        payload = json.dumps(
            _safe_adk_value(dump(mode="json", by_alias=True)),
            separators=(",", ":"),
        ).encode("utf-8")
        checkpoint_id = getattr(event, "id", None) or uuid.uuid4().hex
        existing = await store.get_checkpoint(
            scope=self._run_scope(request),
            checkpoint_id=checkpoint_id,
            namespace="events",
        )
        if existing is not None:
            return
        previous = await store.get_checkpoint(
            scope=self._run_scope(request), namespace="events"
        )
        record = CheckpointRecord(
            run_id=request.invocation_id,
            checkpoint_id=checkpoint_id,
            namespace="events",
            framework="adk",
            type_name="json",
            payload=payload,
            metadata_type="json",
            metadata=b"{}",
            versions_type="json",
            versions=b"{}",
            parent_checkpoint_id=(
                None if previous is None else previous.checkpoint_id
            ),
        )
        await store.put(
            record, scope=self._run_scope(request), expected_revision=None
        )

    async def _finish_checkpoint(
        self, request: InvocationRequest, status: str
    ) -> None:
        """Release portable run ownership after ADK reaches a terminal outcome."""

        if status == "completed" and durable_completion_deferred(
            request.invocation_id
        ):
            # The outer storage wrapper must first persist the lifecycle-final
            # public result, then make terminal completion visible atomically.
            return
        store = self._portable_checkpoints()
        if store is None:
            return
        scope = self._run_scope(request)
        record = await store.get_run(scope=scope)
        if record is not None and record.status == "running":
            await store.transition(
                scope=scope,
                expected_status="running",
                status=status,
            )

    def _run_scope(self, request: InvocationRequest) -> RunScope:
        return RunScope(
            self.application.name,
            request.user_id,
            request.session_id,
            request.invocation_id,
        )

    def _portable_checkpoints(self) -> CheckpointStore | None:
        """Use portable checkpoints only when Harnest, not ADK, owns storage."""

        provider = self.application.checkpointer
        return provider if isinstance(provider, HarnestStore) else None


class _ADKEventNormalizer:
    """Turn ADK cumulative/partial events into small neutral event deltas."""

    def __init__(
        self,
        output_policy: OutputPolicy | None = None,
        *,
        root_agent_name: str | None = None,
    ) -> None:
        """Keep partial child output private until its tool intent is known."""

        self._output_policy = output_policy or OutputPolicy()
        self._root_agent_name = root_agent_name
        self._text = ""
        self._thinking = ""
        self._author: str | None = None
        self._active_agents: list[str] = []
        self._pending_subagent_text = ""
        self._pending_subagent_author: str | None = None

    def feed(self, event: Any) -> list[dict[str, Any]]:
        """Normalize one event and apply the portable intermediate-output policy."""

        activities = self._start_event(event)
        normalized = self._normalize_event(event)
        self._finish_event_agents(normalized)
        self._replace_pending_subagent_message(event, normalized)
        if not self._filters_subagent_event(event):
            return [*activities, *normalized]
        return [*activities, *self._filter_subagent_messages(event, normalized)]

    def _start_event(self, event: Any) -> list[dict[str, Any]]:
        """Reset cumulative text and announce newly active native agents."""

        author = _event_agent(event)
        if author != self._author:
            self._text = ""
            self._thinking = ""
            self._author = author
        if author is None or author in self._active_agents:
            return []
        self._active_agents.append(author)
        return [_agent_activity(author, "started")]

    def _finish_event_agents(self, events: list[dict[str, Any]]) -> None:
        """Stop tracking agents when ADK explicitly reports a terminal activity."""

        for item in events:
            if item.get("type") != "agent_activity" or item.get("activity") not in {
                "completed",
                "failed",
                "interrupted",
            }:
                continue
            agent = item.get("agent")
            if agent in self._active_agents:
                self._active_agents.remove(agent)

    def _replace_pending_subagent_message(
        self, event: Any, normalized: list[dict[str, Any]]
    ) -> None:
        """Drop a held child answer only when another author replaces it visibly."""

        if not self._pending_subagent_text:
            return
        author = getattr(event, "author", None)
        has_message = any(item.get("type") == "message" for item in normalized)
        if author != self._pending_subagent_author and has_message:
            # Empty graph bookkeeping events do not supersede a child answer;
            # a later customer-facing message does and becomes canonical instead.
            self._pending_subagent_text = ""
            self._pending_subagent_author = None

    def _normalize_event(self, event: Any) -> list[dict[str, Any]]:
        """Convert content, output, and tool payloads into neutral deltas."""

        normalized: list[dict[str, Any]] = []
        for item in _event_items(event, self._output_policy):
            event_type = item["type"]
            if event_type == "thinking":
                delta = self._thinking_delta(event, str(item["text"]))
            elif event_type == "message":
                delta = self._message_delta(event, str(item["text"]))
            else:
                normalized.append(_event_with_agent(item, event))
                continue
            if delta:
                normalized.append(_event_with_agent({**item, "text": delta}, event))
            if not getattr(event, "partial", False):
                if event_type == "thinking":
                    self._thinking = ""
                else:
                    self._text = ""
        return normalized

    def _message_delta(self, event: Any, text: str) -> str:
        """Deduplicate ADK's cumulative final event against streamed partials."""

        partial = bool(getattr(event, "partial", False))
        if self._text and text.startswith(self._text):
            delta = text[len(self._text) :]
            self._text = text
            return delta
        if not partial and self._text == text:
            return ""
        self._text = self._text + text if partial else text
        return text

    def _thinking_delta(self, event: Any, text: str) -> str:
        """Deduplicate cumulative ADK reasoning without mixing it into answer text."""

        partial = bool(getattr(event, "partial", False))
        if self._thinking and text.startswith(self._thinking):
            delta = text[len(self._thinking) :]
            self._thinking = text
            return delta
        if not partial and self._thinking == text:
            return ""
        self._thinking = self._thinking + text if partial else text
        return text

    def _filters_subagent_event(self, event: Any) -> bool:
        """Limit buffering to child authors while preserving root token streaming."""

        author = getattr(event, "author", None)
        return (
            self._output_policy.subagent_messages == "suppress"
            and isinstance(author, str)
            and bool(author)
            and isinstance(self._root_agent_name, str)
            and author != self._root_agent_name
        )

    def _filter_subagent_messages(
        self, event: Any, normalized: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Buffer child deltas and discard them when the completed turn calls a tool."""

        messages = [item for item in normalized if item.get("type") == "message"]
        visible = [item for item in normalized if item.get("type") != "message"]
        self._pending_subagent_text += "".join(
            str(item.get("text", "")) for item in messages
        )
        self._pending_subagent_author = getattr(event, "author", None)
        if getattr(event, "partial", False):
            return visible
        has_tool_calls = any(item.get("type") == "tool_call" for item in visible)
        if has_tool_calls:
            self._pending_subagent_text = ""
            self._pending_subagent_author = None
        # Hold a tool-free complete child message until the stream ends. A
        # following tool-call event can then still classify it as narration.
        return visible

    def finish(self, *, complete_agents: bool = True) -> list[dict[str, Any]]:
        """Release held output and optionally close agents active at stream end."""

        text = self._pending_subagent_text
        author = self._pending_subagent_author
        self._pending_subagent_text = ""
        self._pending_subagent_author = None
        events = []
        if text:
            message = {"type": "message", "role": "assistant", "text": text}
            events.append({**message, **({"agent": author} if author else {})})
        if complete_agents:
            # Child activity is nested beneath its caller, so close the most
            # recently observed agent first when ADK has no explicit end marker.
            events.extend(
                _agent_activity(agent, "completed")
                for agent in reversed(self._active_agents)
            )
        self._active_agents.clear()
        return events


def _event_items(
    event: Any, output_policy: OutputPolicy
) -> list[dict[str, Any]]:
    """Collect public event projections in provider response order."""

    items, text = _content_items(event)
    items.extend(_output_items(event, text))
    items.extend(_function_call_items(event))
    items.extend(_function_response_items(event))
    items.extend(_agent_metadata_items(event, output_policy))
    items.extend(_agent_action_items(event))
    return items


def _content_items(event: Any) -> tuple[list[dict[str, Any]], str]:
    items: list[dict[str, Any]] = []
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    thinking = "".join(
        part.text
        for part in parts or ()
        if getattr(part, "thought", False)
        and isinstance(getattr(part, "text", None), str)
    )
    if thinking:
        items.append({"type": "thinking", "text": thinking})
    text = "".join(
        part.text
        for part in _customer_facing_parts(parts)
        if isinstance(getattr(part, "text", None), str)
    )
    if text:
        items.append({"type": "message", "role": "assistant", "text": text})
    return items, text


def _event_agent(event: Any) -> str | None:
    """Return ADK's public author identity while excluding synthetic user events."""

    author = getattr(event, "author", None)
    return author if isinstance(author, str) and author and author != "user" else None


def _event_with_agent(item: dict[str, Any], event: Any) -> dict[str, Any]:
    """Attribute normalized activity without retaining the native event object."""

    agent = _event_agent(event)
    return {**item, **({"agent": agent} if agent else {})}


def _agent_activity(agent: str, activity: str, **details: Any) -> dict[str, Any]:
    """Create one provider-neutral agent lifecycle event."""

    return {
        "type": "agent_activity",
        "agent": agent,
        "activity": activity,
        **details,
    }


def _agent_metadata_items(
    event: Any, output_policy: OutputPolicy
) -> list[dict[str, Any]]:
    """Normalize ADK model metadata under the configured disclosure policy."""

    usage_metadata = getattr(event, "usage_metadata", None)
    usage = _reported_token_usage(
        getattr(usage_metadata, "prompt_token_count", None),
        getattr(usage_metadata, "candidates_token_count", None),
        getattr(usage_metadata, "total_token_count", None),
    )
    model = _nonempty_adk_string(getattr(event, "model_version", None))
    finish_reason = _nonempty_adk_string(getattr(event, "finish_reason", None))
    raw = (
        _raw_adk_metadata(event)
        if output_policy.agent_metadata == "raw"
        else None
    )
    if usage is None and model is None and finish_reason is None and not raw:
        return []
    metadata = AgentMetadata(
        framework="adk",
        usage=usage,
        model=model,
        finish_reason=finish_reason,
        raw=raw,
    )
    return [metadata._as_runtime_event()]


def _nonempty_adk_string(value: Any) -> str | None:
    """Normalize ADK strings and string enums without inventing labels."""

    candidate = getattr(value, "value", value)
    return candidate if isinstance(candidate, str) and candidate else None


def _raw_adk_metadata(event: Any) -> Mapping[str, Any] | None:
    """Return JSON-normalized LlmResponse fields without generated content."""

    try:
        from google.adk.models import LlmResponse
    except ImportError:  # pragma: no cover - ADK driver requires this package
        return None
    fields = set(LlmResponse.model_fields) - {"content"}
    dump = getattr(event, "model_dump", None)
    if not callable(dump):
        return None
    # Restrict by the base response contract so Event actions, state, branch,
    # invocation identifiers, and workflow internals cannot enter raw metadata.
    value = dump(
        mode="python",
        by_alias=False,
        include=fields,
        exclude_none=True,
    )
    normalized = json_value(value, unsupported="string")
    return dict(normalized) if isinstance(normalized, Mapping) and normalized else None


def _agent_action_items(event: Any) -> list[dict[str, Any]]:
    """Project ADK lifecycle actions without exposing state or provider metadata."""

    agent = _event_agent(event)
    if agent is None:
        return []
    actions = getattr(event, "actions", None)
    items: list[dict[str, Any]] = []
    target = getattr(actions, "transfer_to_agent", None)
    if isinstance(target, str) and target:
        items.append(_agent_activity(agent, "handoff", target=target))
    if getattr(actions, "escalate", False):
        items.append(_agent_activity(agent, "escalated"))
    if getattr(event, "turn_complete", False):
        items.append(_agent_activity(agent, "turn_completed"))
    error_code = getattr(event, "error_code", None)
    if isinstance(error_code, str) and error_code:
        items.append(_agent_activity(agent, "failed", code=error_code))
    elif getattr(event, "interrupted", False):
        items.append(_agent_activity(agent, "interrupted"))
    elif getattr(actions, "end_of_agent", False):
        items.append(_agent_activity(agent, "completed"))
    return items


def _customer_facing_parts(parts: Any) -> tuple[Any, ...]:
    """Return parts safe for the public response and evaluation boundaries."""

    return tuple(part for part in parts or () if not getattr(part, "thought", False))


def _output_items(event: Any, text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    output = getattr(event, "output", None)
    if output is not None and _is_terminal_output(event):
        if isinstance(output, str):
            if not text:
                items.append(
                    {"type": "message", "role": "assistant", "text": output}
                )
        # Graph outputs can contain a client-tool result when an authored graph
        # forwards child output. Keep private continuation markers out of every
        # public transport just as we do for ordinary function responses.
        normalized_output = sanitize_stored_media(
            sanitize_transient_media(json_value(output))[0]
        )
        items.append(
            {
                "type": "graph_output",
                "output": normalized_output,
                "result": normalized_output,
            }
        )
    return items


def _function_call_items(event: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    get_calls = getattr(event, "get_function_calls", None)
    for call in get_calls() if callable(get_calls) else ():
        items.append(
            {
                "type": "tool_call",
                "id": getattr(call, "id", None),
                "name": getattr(call, "name", None),
                "arguments": json_value(getattr(call, "args", None)),
            }
        )
    return items


def _function_response_items(event: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    get_responses = getattr(event, "get_function_responses", None)
    for response in get_responses() if callable(get_responses) else ():
        items.append(
            {
                "type": "tool_result",
                "id": getattr(response, "id", None),
                "name": getattr(response, "name", None),
                "result": sanitize_stored_media(
                    sanitize_transient_media(
                        json_value(getattr(response, "response", None))
                    )[0]
                ),
            }
        )
    return items


def _is_terminal_output(event: Any) -> bool:
    node_info = getattr(event, "node_info", None)
    output_for = getattr(node_info, "output_for", None)
    if not output_for:
        return True
    return any(isinstance(path, str) and "/" not in path for path in output_for)


def _update_long_running_tools(pending: set[str], event: Any) -> None:
    """Track ADK long-running call IDs until their FunctionResponse is persisted."""

    long_running = getattr(event, "long_running_tool_ids", None)
    if isinstance(long_running, (list, tuple, set)):
        pending.update(value for value in long_running if isinstance(value, str))
    content = getattr(event, "content", None)
    for part in getattr(content, "parts", ()) or ():
        response = getattr(part, "function_response", None)
        response_id = getattr(response, "id", None)
        if isinstance(response_id, str):
            pending.discard(response_id)


async def _complete_session_record(service: Any, session: Any) -> SessionRecord:
    """Attach the private lane without placing it on ADK's native Session."""

    public = _session_record(session)
    reader = getattr(service, "get_session_record", None)
    if not callable(reader):
        return public
    stored = await reader(user_id=session.user_id, session_id=session.id)
    if stored is None:
        return public
    return replace(public, application_data=json_value(stored.application_data))


def _stored_adk_session_record(record: SessionRecord, app_name: str) -> SessionRecord:
    """Project one store page without issuing a query for every ADK session."""

    from .session_adk import _adk_session

    public = _session_record(_adk_session(record, app_name))
    return replace(public, application_data=json_value(record.application_data))


def _session_record(session: Any) -> SessionRecord:
    """Preserve native ADK event metadata beside portable session state."""

    updated_at = _timestamp(getattr(session, "last_update_time", 0.0))
    state = sanitize_stored_media(
        sanitize_transient_media(json_value(session.state))[0]
    )
    return SessionRecord(
        id=session.id,
        user_id=session.user_id,
        state=state,
        updated_at=updated_at,
        metadata=_adk_session_metadata(session),
    )


def _adk_session_messages(session: Any) -> list[SessionMessage]:
    """Project content-bearing ADK events while retaining each native event."""

    messages: list[SessionMessage] = []
    for index, event in enumerate(getattr(session, "events", ()) or ()):
        record = json_value(event)
        content = record.get("content") if isinstance(record, Mapping) else None
        if not isinstance(content, Mapping):
            continue
        role = _adk_message_role(record, content)
        messages.append(
            SessionMessage(
                id=str(record.get("id") or f"{session.id}:{index}"),
                role=role,
                content=_adk_message_content(content, role=role),
                created_at=_timestamp(record.get("timestamp")),
                metadata={"adk": _safe_adk_value(record)},
            )
        )
    return messages


def _adk_message_role(
    event: Mapping[str, Any], content: Mapping[str, Any]
) -> str:
    """Normalize ADK authorship while recognizing tool response events."""

    parts = content.get("parts")
    if isinstance(parts, list) and any(
        isinstance(part, Mapping) and part.get("functionResponse") is not None
        for part in parts
    ):
        return "tool"
    role = content.get("role")
    if role == "user" or event.get("author") == "user":
        return "user"
    if role == "system":
        return "system"
    return "assistant"


def _adk_message_content(content: Mapping[str, Any], *, role: str) -> Any:
    """Expose text, reference parts, or structured tool output portably."""

    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    portable = _adk_portable_content(parts)
    if portable is not None:
        return portable
    text = _adk_visible_text(parts)
    if text:
        return text
    return _adk_tool_response(parts) if role == "tool" else None


def _adk_portable_content(parts: list[Any]) -> list[dict[str, Any]] | None:
    """Restore ordered Harnest markers and their neighboring visible text."""

    if not any(_record_marker(part) is not None for part in parts):
        return None
    return [value for part in parts if (value := _portable_adk_part(part))]


def _portable_adk_part(part: Any) -> dict[str, Any] | None:
    """Project one non-private ADK part into portable session content."""

    if not isinstance(part, Mapping) or part.get("thought") is True:
        return None
    marker = _record_marker(part)
    if marker is not None:
        if transient_media_lease_id(marker) is not None:
            return None
        if is_transient_media_placeholder(marker):
            return None
        return sanitize_stored_media(dict(marker))
    text = part.get("text")
    return {"type": "text", "text": text} if isinstance(text, str) and text else None


def _record_marker(part: Any) -> Mapping[str, Any] | None:
    """Read a serialized marker without accepting provider-owned metadata."""

    if not isinstance(part, Mapping):
        return None
    metadata = part.get("partMetadata")
    marker = metadata.get(_CONTENT_MARKER) if isinstance(metadata, Mapping) else None
    if not isinstance(marker, Mapping) or marker.get("type") not in _CONTENT_KINDS:
        return None
    return marker


def _adk_visible_text(parts: list[Any]) -> str:
    """Join user-visible ADK text while excluding private thought parts."""

    texts = [
        part["text"]
        for part in parts
        if isinstance(part, Mapping)
        and part.get("thought") is not True
        and isinstance(part.get("text"), str)
    ]
    return "".join(texts)


def _adk_tool_response(parts: list[Any]) -> Any:
    """Return one tool response directly and preserve multiple responses."""

    responses = [
        part["functionResponse"].get("response")
        for part in parts
        if isinstance(part, Mapping)
        and isinstance(part.get("functionResponse"), Mapping)
    ]
    value = responses[0] if len(responses) == 1 else responses
    return sanitize_stored_media(sanitize_transient_media(value)[0])


def _adk_turn_metadata(events: list[Any]) -> dict[str, Any]:
    """Namespace complete native turn events for an authored metadata model."""

    return {"adk": {"events": events}}


def _adk_session_metadata(session: Any) -> dict[str, Any]:
    """Keep ADK-owned fields opaque while avoiding duplicate public state."""

    record = _safe_adk_value(json_value(session))
    if not isinstance(record, Mapping):
        return {"adk": record}
    return {
        "adk": {
            key: value
            for key, value in record.items()
            if key not in {"id", "userId", "state"}
        }
    }


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _safe_adk_value(value: Any) -> Any:
    """Remove provider bytes, URIs, and private marker data from native metadata."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, list):
        return [_safe_adk_value(item) for item in value]
    return _safe_adk_mapping(value) if isinstance(value, Mapping) else value


def _safe_adk_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize one native mapping while preserving non-content ADK fields."""

    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key in {
            "data",
            "fileData",
            "fileUri",
            "thoughtSignature",
            "harnestTransient",
            "harnestStored",
            "leaseId",
        }:
            continue
        if key == "partMetadata":
            # Portable content is already exposed through SessionMessage.content;
            # duplicating it in opaque native metadata risks accidental logging.
            filtered = (
                {
                    name: _safe_adk_value(child)
                    for name, child in item.items()
                    if name != _CONTENT_MARKER
                }
                if isinstance(item, Mapping)
                else {}
            )
            if filtered:
                safe[key] = filtered
            continue
        safe[key] = _safe_adk_value(item)
    return safe


def _create_runner(
    application: CompiledApplication,
    session_service: Any,
    *,
    asset_store: AssetStore | None = None,
    asset_stores: Mapping[str, AssetStore] | None = None,
    plugin_manager: Any | None = None,
) -> Any:
    """Create ADK's runner while preserving actionable advanced-mode logs."""

    try:
        from google.adk.runners import InMemoryRunner, Runner
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Google ADK is required to run an ADK agent") from exc
    from ._adk_warnings import suppress_managed_transfer_cache_warning
    from .credentials_adk import adk_credential_service

    credential_service = (
        adk_credential_service(application.credential_provider)
        if application.credential_provider is not None
        else None
    )

    if application.mode == "managed":
        # Managed authors cannot configure ADK's provider-specific App cache.
        with suppress_managed_transfer_cache_warning():
            result = _build_adk_runner(
                application,
                session_service,
                credential_service,
                InMemoryRunner,
                Runner,
            )
    else:
        result = _build_adk_runner(
            application,
            session_service,
            credential_service,
            InMemoryRunner,
            Runner,
        )
    if application.mode == "managed":
        _register_agent_context_plugins(
            result.plugin_manager,
            root_name=getattr(application.target, "name", None),
            plugin_manager=plugin_manager,
        )
        _register_mcp_context_plugins(
            result.plugin_manager,
            application.target,
            application.extensions,
        )
        _register_tool_lifecycle_plugin(
            result.plugin_manager, application.extensions
        )
    stores = dict(asset_stores or {})
    if asset_store is not None:
        stores.setdefault("default", asset_store)
    _register_asset_plugin(result.plugin_manager, stores)
    return result


def _register_agent_context_plugins(
    manager: Any,
    *,
    root_name: str | None = None,
    plugin_manager: Any | None = None,
) -> None:
    """Bracket authored callbacks with invocation-safe subagent identity."""

    from .context_adk import adk_agent_context_plugins

    enter, exit_plugin = adk_agent_context_plugins(root_name, plugin_manager)
    reserved = {enter.name, exit_plugin.name}
    if any(item.name in reserved for item in manager.plugins):
        raise ValueError("ADK application uses a reserved Harnest context plugin")
    # One adapter must precede authored callbacks and its pair must follow them;
    # a single plugin position cannot preserve child identity at both boundaries.
    manager.plugins.insert(0, enter)
    manager.plugins.append(exit_plugin)


def _register_mcp_context_plugins(
    manager: Any, target: Any, listeners: Any
) -> None:
    """Bracket the whole ADK run with its discovered MCP registry."""

    from .mcp_adk import adk_mcp_context_plugins

    enter, exit_plugin = adk_mcp_context_plugins(target, listeners)
    reserved = {enter.name, exit_plugin.name}
    if any(item.name in reserved for item in manager.plugins):
        raise ValueError("ADK application uses a reserved Harnest MCP plugin")
    enter_index = next(
        (
            index + 1
            for index, item in enumerate(manager.plugins)
            if item.name == "_harnest_agent_context_enter"
        ),
        0,
    )
    manager.plugins.insert(enter_index, enter)
    exit_index = next(
        (
            index
            for index, item in enumerate(manager.plugins)
            if item.name == "_harnest_agent_context_exit"
        ),
        len(manager.plugins),
    )
    manager.plugins.insert(exit_index, exit_plugin)


def _register_tool_lifecycle_plugin(manager: Any, listeners: Any) -> None:
    """Install full native tool interception before authored ADK plugins."""

    from .tool_adk import adk_tool_lifecycle_plugin

    plugin = adk_tool_lifecycle_plugin(listeners)
    if any(item.name == plugin.name for item in manager.plugins):
        raise ValueError("ADK application uses a reserved Harnest tool plugin")
    enter_index = next(
        (
            index + 1
            for index, item in enumerate(manager.plugins)
            if item.name == "_harnest_mcp_context_enter"
        ),
        0,
    )
    manager.plugins.insert(enter_index, plugin)


def _register_asset_plugin(
    manager: Any, stores: Mapping[str, AssetStore]
) -> None:
    """Put the reference boundary ahead of callbacks that may short-circuit."""

    plugin = _asset_content_plugin(stores)
    if any(item.name == plugin.name for item in manager.plugins):
        raise ValueError("ADK application reserves the harnest_asset_content plugin")
    # ADK stops callback traversal at the first replacement. Running this
    # boundary first guarantees no authored callback can persist inline bytes.
    manager.plugins.insert(0, plugin)


def _asset_content_plugin(
    stores: Mapping[str, AssetStore] | AssetStore | None,
) -> Any:
    """Create the optional ADK plugin that materializes only model requests."""

    from google.adk.plugins import BasePlugin

    if stores is None:
        storage_registry: dict[str, AssetStore] = {}
    elif isinstance(stores, AssetStore):
        storage_registry = {"default": stores}
    else:
        storage_registry = dict(stores)
    default_store = storage_registry.get("default")

    class _AssetContentPlugin(BasePlugin):
        """Keep ADK history reference-only while serving scoped model bytes."""

        def __init__(self) -> None:
            super().__init__(name="harnest_asset_content")
            self._transient_calls: dict[
                tuple[str, str, str, str, str, str],
                tuple[TransientMediaAccess, tuple[str, ...]],
            ] = {}
            self._transient_branches: dict[
                tuple[str, str, str, str, str, str],
                tuple[TransientMediaAccess, tuple[str, ...]],
            ] = {}

        async def after_tool_callback(
            self,
            *,
            tool: Any,
            tool_args: dict[str, Any],
            tool_context: Any,
            result: dict[str, Any],
        ) -> None:
            """Bind private bytes to the ADK branch that produced the placeholder."""

            del tool, tool_args
            access = current_transient_media()
            signatures = transient_media_placeholders(result)
            leases = matching_transient_leases(
                signatures, access.pending() if access is not None else ()
            )
            if access is not None and leases:
                self._transient_branches[_adk_model_call_key(tool_context)] = (
                    access,
                    tuple(lease.lease_id for lease in leases),
                )
            return None

        async def before_model_callback(
            self, *, callback_context: Any, llm_request: Any
        ) -> None:
            """Materialize stored and transient media on a detached request."""

            scope = AssetScope(
                user_id=callback_context.user_id,
                session_id=callback_context.session.id,
            )
            key = _adk_model_call_key(callback_context)
            branch = self._transient_branches.get(key)
            access = branch[0] if branch is not None else current_transient_media()
            candidates = _adk_branch_leases(branch)
            contents: list[Any] = []
            for item in llm_request.contents:
                materialized, _ = await _materialized_content(
                    item, storage_registry, scope
                )
                contents.append(materialized)
            leases = _adk_pending_leases(contents, access, candidates=candidates)
            contents = _attach_adk_pending(contents, leases)
            llm_request.contents = contents
            if access is not None and leases:
                # The key includes ADK's branch/node identity so parallel and
                # nested agent model calls cannot acknowledge each other's bytes.
                self._transient_calls[key] = (
                    access,
                    tuple(lease.lease_id for lease in leases),
                )
            return None

        async def after_model_callback(
            self, *, callback_context: Any, llm_response: Any
        ) -> None:
            """Consume only media acknowledged by a successful model call."""

            del llm_response
            pending = self._transient_calls.pop(
                _adk_model_call_key(callback_context), None
            )
            if pending is not None:
                access, lease_ids = pending
                access.commit(lease_ids)
                self._transient_branches.pop(
                    _adk_model_call_key(callback_context), None
                )
            return None

        async def on_model_error_callback(
            self, *, callback_context: Any, llm_request: Any, error: Exception
        ) -> None:
            """Retain media for framework retries and drop per-attempt bookkeeping."""

            del llm_request, error
            # Invocation cleanup owns terminal failure. Clearing here would
            # make ADK's next provider attempt silently lose its media bytes.
            self._transient_calls.pop(_adk_model_call_key(callback_context), None)
            return None

        async def after_run_callback(self, *, invocation_context: Any) -> None:
            """Release completed invocation bookkeeping after ADK finishes."""

            self._forget_invocation(invocation_context)

        async def on_run_error_callback(
            self, *, invocation_context: Any, error: Exception
        ) -> None:
            """Release failed invocation bookkeeping without exposing errors."""

            del error
            self._forget_invocation(invocation_context)

        def _forget_invocation(self, context: Any) -> None:
            """Drop callback state; the client-tool store owns byte cleanup."""

            prefix = _adk_invocation_key(context)
            self._transient_calls = {
                key: value
                for key, value in self._transient_calls.items()
                if key[:3] != prefix
            }
            self._transient_branches = {
                key: value
                for key, value in self._transient_branches.items()
                if key[:3] != prefix
            }

        async def on_event_callback(
            self, *, invocation_context: Any, event: Any
        ) -> Any | None:
            """Replace terminal model blobs before ADK persists or yields them."""

            if getattr(event, "partial", False):
                return None
            if default_store is None:
                return None
            scope = AssetScope(
                user_id=invocation_context.user_id,
                session_id=invocation_context.session.id,
            )
            return await _reference_only_event(event, default_store, scope)

    return _AssetContentPlugin()


def _adk_invocation_key(context: Any) -> tuple[str, str, str]:
    """Identify one principal-scoped ADK invocation for callback cleanup."""

    session = getattr(context, "session", None)
    return (
        str(getattr(context, "user_id", "") or ""),
        str(getattr(session, "id", "") or ""),
        str(getattr(context, "invocation_id", "") or ""),
    )


def _adk_model_call_key(
    callback_context: Any,
) -> tuple[str, str, str, str, str, str]:
    """Identify one ADK agent branch without exposing its values externally."""

    return (
        *_adk_invocation_key(callback_context),
        *(
            str(getattr(callback_context, name, "") or "")
            for name in ("agent_name", "branch", "node_path")
        ),
    )


def _adk_transient_part(lease: TransientMediaLease) -> Any:
    """Translate one private lease into ADK's provider-facing blob shape."""

    from google.genai import types

    return types.Part(
        inline_data=types.Blob(mime_type=lease.media_type, data=lease.data)
    )


def _adk_pending_leases(
    contents: list[Any],
    access: TransientMediaAccess | None,
    *,
    candidates: tuple[TransientMediaLease, ...] = (),
) -> tuple[TransientMediaLease, ...]:
    """Correlate safe request placeholders with this invocation's private bytes."""

    if access is None:
        return ()
    signatures = [
        signature
        for content in contents
        for signature in _adk_content_transient_signatures(content)
    ]
    return matching_transient_leases(signatures, candidates or access.pending())


def _adk_branch_leases(
    branch: tuple[TransientMediaAccess, tuple[str, ...]] | None,
) -> tuple[TransientMediaLease, ...]:
    """Resolve one branch binding without relying on callback task context."""

    if branch is None:
        return ()
    access, lease_ids = branch
    return tuple(
        lease
        for lease_id in lease_ids
        if (lease := access.peek(lease_id)) is not None
    )


def _adk_content_transient_signatures(content: Any) -> tuple[tuple[str, str], ...]:
    """Read safe signatures from native parts without requiring correlation IDs."""

    signatures: list[tuple[str, str]] = []
    for part in getattr(content, "parts", None) or ():
        marker = _part_marker(part)
        if marker is not None:
            safe_marker = sanitize_transient_media(marker)[0]
            signatures.extend(transient_media_placeholders(safe_marker))
        response = getattr(part, "function_response", None)
        value = getattr(response, "response", None) if response is not None else None
        safe_value = sanitize_transient_media(value)[0]
        signatures.extend(transient_media_placeholders(safe_value))
    return tuple(signatures)


def _attach_adk_pending(
    contents: list[Any], leases: tuple[TransientMediaLease, ...]
) -> list[Any]:
    """Append pending media only to the newest matching provider content."""

    if not leases:
        return contents
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        if not _adk_content_transient_signatures(content):
            continue
        parts = list(getattr(content, "parts", None) or ())
        replacement = content.model_copy(
            update={"parts": [*parts, *(_adk_transient_part(item) for item in leases)]}
        )
        return [*contents[:index], replacement, *contents[index + 1 :]]
    return contents


async def _materialized_content(
    content: Any,
    stores: Mapping[str, AssetStore],
    scope: AssetScope,
    transient: TransientMediaAccess | None = None,
) -> tuple[Any, list[str]]:
    """Copy one ADK content value and resolve model-only media bytes."""

    parts = getattr(content, "parts", None) or ()
    materialized: list[Any] = []
    lease_ids: list[str] = []
    for part in parts:
        projected, attached, discovered = await _materialized_parts(
            part, stores, scope, transient
        )
        materialized.append(projected)
        materialized.extend(attached)
        lease_ids.extend(discovered)
    if len(parts) == len(materialized) and all(
        left is right for left, right in zip(parts, materialized)
    ):
        return content, lease_ids
    return content.model_copy(update={"parts": materialized}), lease_ids


async def _materialized_parts(
    part: Any,
    stores: Mapping[str, AssetStore],
    scope: AssetScope,
    transient: TransientMediaAccess | None,
) -> tuple[Any, list[Any], list[str]]:
    """Materialize one native part plus transient function-result attachments."""

    marker = _part_marker(part)
    direct_id = transient_media_lease_id(marker)
    if direct_id is not None:
        from google.genai import types

        safe = transient_media_placeholder(
            str(marker["type"]), str(marker["mediaType"])
        )
        return _marker_part(safe, types), [], []
    projected = await _materialized_part(part, stores, scope)
    response = getattr(part, "function_response", None)
    value = getattr(response, "response", None) if response is not None else None
    skeleton, discovered = sanitize_transient_media(value)
    durable = stored_media_references(skeleton)
    if not discovered and not durable:
        return projected, [], []
    # Even stale markers are removed from the detached provider JSON. Only
    # leases still active for this invocation are attached to this model call.
    response = response.model_copy(update={"response": sanitize_stored_media(skeleton)})
    projected = part.model_copy(update={"function_response": response})
    attached = [
        await _adk_stored_part(reference, stores, scope) for reference in durable
    ]
    return projected, attached, list(discovered)


async def _materialized_part(
    part: Any, stores: Mapping[str, AssetStore], scope: AssetScope
) -> Any:
    """Load bytes only for the detached part passed to the ADK model adapter."""

    marker = _part_marker(part)
    if marker is None:
        return part
    kind = marker["type"]
    from google.genai import types

    if kind == "data":
        return types.Part(
            text=json.dumps(marker.get("value"), ensure_ascii=False)
        )
    if is_transient_media_placeholder(marker):
        return part
    durable = stored_media_reference(marker)
    if durable is not None:
        return await _adk_stored_part(durable, stores, scope)
    asset_id = marker.get("assetId")
    store_name = marker.get("store", "default")
    store = stores.get(store_name) if isinstance(store_name, str) else None
    if not isinstance(asset_id, str) or store is None:
        raise RuntimeError("ADK content reference is invalid")
    return await _adk_inline_asset_part(store, scope, asset_id, types)


async def _adk_inline_asset_part(
    store: AssetStore, scope: AssetScope, asset_id: str, types: Any
) -> Any:
    """Read one ordinary stored reference into an ADK inline-data part."""

    record = await store.stat(scope=scope, asset_id=asset_id)
    if record is None:
        raise RuntimeError("ADK content asset is unavailable")
    data = bytearray()
    async for chunk in store.open(scope=scope, asset_id=asset_id):
        data.extend(chunk)
        if len(data) > record.size_bytes:
            raise RuntimeError("ADK content asset changed while reading")
    if len(data) != record.size_bytes:
        raise RuntimeError("ADK content asset changed while reading")
    return types.Part(
        inline_data=types.Blob(
            mime_type=record.media_type,
            data=bytes(data),
        )
    )


async def _adk_stored_part(
    reference: Any,
    stores: Mapping[str, AssetStore],
    scope: AssetScope,
) -> Any:
    """Generate one explicit temporary URL immediately before an ADK call."""

    storage = stores.get(reference.store)
    if storage is None or not isinstance(storage, AssetURLStorage):
        raise RuntimeError("declared asset storage cannot generate model URLs")
    record = await storage.stat(scope=scope, asset_id=reference.asset_id)
    if record is None:
        raise RuntimeError("ADK content asset is unavailable")
    url = await storage.signed_url(
        scope=scope,
        asset_id=reference.asset_id,
        expires_in=reference.expires_in,
    )
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("declared asset storage returned an invalid model URL")
    from google.genai import types

    return types.Part(
        file_data=types.FileData(file_uri=url, mime_type=record.media_type)
    )


async def _reference_only_event(
    event: Any, store: AssetStore, scope: AssetScope
) -> Any | None:
    """Stage final ADK media output and return a reference-only event copy."""

    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    if not parts or not any(getattr(part, "inline_data", None) for part in parts):
        return None
    replaced = [await _stored_output_part(part, store, scope) for part in parts]
    return event.model_copy(
        update={"content": content.model_copy(update={"parts": replaced})}
    )


async def _stored_output_part(part: Any, store: AssetStore, scope: AssetScope) -> Any:
    """Persist one output blob and replace it with an opaque Harnest marker."""

    blob = getattr(part, "inline_data", None)
    data = getattr(blob, "data", None) if blob is not None else None
    media_type = getattr(blob, "mime_type", None) if blob is not None else None
    if not isinstance(data, bytes) or not isinstance(media_type, str):
        return part
    metadata, inspected_type = inspect_asset(data, media_type)
    record = await store.save(
        scope=scope,
        media_type=inspected_type,
        chunks=_one_chunk(data),
        metadata=metadata,
    )
    kind = _kind_for_media_type(record.media_type)
    marker = {
        "type": kind,
        "assetId": record.asset_id,
        "mediaType": record.media_type,
        "sizeBytes": record.size_bytes,
    }
    from google.genai import types

    return _marker_part(marker, types)


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    """Adapt one provider blob to the AssetStore streaming write contract."""

    yield data


def _kind_for_media_type(media_type: str) -> str:
    """Map a concrete MIME family onto one portable content discriminator."""

    family = media_type.partition("/")[0]
    return family if family in {"image", "audio", "video"} else "file"


def _part_marker(part: Any) -> Mapping[str, Any] | None:
    """Read only the private marker shape created by this adapter."""

    metadata = getattr(part, "part_metadata", None)
    marker = metadata.get(_CONTENT_MARKER) if isinstance(metadata, Mapping) else None
    if not isinstance(marker, Mapping) or marker.get("type") not in _CONTENT_KINDS:
        return None
    return marker


def _build_adk_runner(
    application: CompiledApplication,
    session_service: Any,
    credential_service: Any,
    in_memory_runner: Any,
    runner: Any,
) -> Any:
    """Preserve native ephemeral artifacts when selecting a session-aware runner."""

    if session_service is None and credential_service is None:
        return in_memory_runner(
            app=application.native_app,
            app_name=application.native_app.name,
        )
    if session_service is None:
        # InMemoryRunner does not expose ADK's credential-service seam, so use
        # the general runner with the equivalent ephemeral session service.
        from google.adk.sessions.in_memory_session_service import (
            InMemorySessionService,
        )

        session_service = InMemorySessionService()
    _validate_session_service(session_service)
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService

    # ADK's code-result processor requires an artifact service even without
    # output files. Match InMemoryRunner's native default when custom session
    # or credential services require Runner; this is not Harnest asset storage.
    return runner(
        app=application.native_app,
        app_name=application.native_app.name,
        session_service=session_service,
        credential_service=credential_service,
        artifact_service=InMemoryArtifactService(),
    )


def _validate_session_service(session_service: Any) -> None:
    required = (
        "create_session",
        "get_session",
        "list_sessions",
        "append_event",
        "delete_session",
        "flush",
    )
    if any(not callable(getattr(session_service, name, None)) for name in required):
        raise TypeError(
            "ADK session_service must implement BaseSessionService operations"
        )


@asynccontextmanager
async def _session_execution_lease(
    session_service: Any,
    *,
    user_id: str,
    session_id: str,
    invocation_id: str,
) -> AsyncIterator[None]:
    acquire = getattr(session_service, "execution_lease", None)
    if not callable(acquire):
        yield
        return
    arguments = {"user_id": user_id, "session_id": session_id}
    # Advanced-mode services predate Harnest's invocation-aware lease hook, so
    # pass correlation only where the authored signature explicitly accepts it.
    if _accepts_invocation_id(acquire):
        arguments["invocation_id"] = invocation_id
    async with acquire(**arguments):
        yield


def _accepts_invocation_id(acquire: Any) -> bool:
    """Keep opaque advanced-mode callables on their legacy lease contract."""

    try:
        return "invocation_id" in inspect.signature(acquire).parameters
    except (TypeError, ValueError):
        return False


__all__ = ["ADKRuntimeDriver"]

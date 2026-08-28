"""Google ADK driver for the framework-neutral Harnest runtime.

This module is intentionally limited to translating between the neutral runtime
contract and ADK.  HTTP validation, deadlines, concurrency limits, and public
wire formats belong to :mod:`harnest.neutral_runtime`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from ._json import json_value
from .application import CompiledApplication
from .checkpoint import CheckpointRecord, CheckpointStore, HarnestStore
from .model_lifecycle import close_litellm_lifecycles
from .mcp_lifecycle import (
    close_mcp_lifecycles,
    mcp_lifecycle_bindings,
    start_mcp_lifecycles,
)
from .neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    SessionConflictError,
    SessionMessage,
    SessionRecord,
)
from .output import OutputPolicy
from .structured import (
    framework_metadata_field,
    validate_runtime_output,
)


def _model_input_text(value: Any) -> str:
    """Render validated structured input for ADK's text content boundary."""

    if isinstance(value, str):
        return value
    return json.dumps(json_value(value), ensure_ascii=False)


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
            self.native_events.append(json_value(event))

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
    ) -> None:
        if application.framework != "adk":
            raise ValueError("ADKRuntimeDriver requires an ADK application")
        if application.native_app is None:
            raise ValueError("compiled ADK application does not contain an App")

        self.application = application
        card_data = dict(card or {})
        description = card_data.get("description")
        if not isinstance(description, str):
            description = getattr(application.target, "description", "") or ""
        display_name = card_data.get("name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = application.name
        self._info = AgentInfo(
            id=application.name,
            name=display_name,
            description=description,
            card=card_data,
            framework=application.framework,
            mode=application.mode,
            extra_endpoints=dict(extra_endpoints or {}),
            input_schema=application.input_schema,
            output_schema=application.output_schema,
        )
        self._runner = _create_runner(application, session_service)
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def app_name(self) -> str:
        return self.application.native_app.name

    @property
    def info(self) -> AgentInfo:
        return self._info

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
        return _session_record(session)

    async def get_session(
        self, *, user_id: str, session_id: str
    ) -> SessionRecord | None:
        self._ensure_open()
        session = await self._runner.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        return None if session is None else _session_record(session)

    async def list_sessions(self, *, user_id: str) -> list[SessionRecord]:
        self._ensure_open()
        response = await self._runner.session_service.list_sessions(
            app_name=self.app_name,
            user_id=user_id,
        )
        return [_session_record(session) for session in response.sessions]

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
        return None if updated is None else _session_record(updated)

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
        message, run_config = await self._prepare_stream_request(request)
        async with _session_execution_lease(
            self._runner.session_service,
            user_id=request.user_id,
            session_id=request.session_id,
        ):
            await self._begin_checkpoint(request)
            native_events = self._runner.run_async(
                user_id=request.user_id,
                session_id=request.session_id,
                # ADK interprets an explicit id as a request to resume an
                # existing invocation. New Harnest runs let ADK allocate its
                # native id, which is persisted in the checkpointed events.
                invocation_id=None,
                new_message=message,
                state_delta=dict(request.state_delta),
                run_config=run_config,
            )
            normalizer = _ADKEventNormalizer(
                self.application.output_policy,
                root_agent_name=getattr(self.application.target, "name", None),
            )
            output = _ADKTurnOutput(self.application.output_schema)
            try:
                async with aclosing(native_events):
                    async for event in native_events:
                        await self._save_checkpoint_event(request, event)
                        output.capture_native(event)
                        for item in normalizer.feed(event):
                            public = output.accept(item)
                            if public is not None:
                                yield public
                for item in normalizer.finish():
                    public = output.accept(item)
                    if public is not None:
                        yield public
                for item in output.complete():
                    yield item
            except BaseException:
                await self._finish_checkpoint(request.invocation_id, "failed")
                raise
            await self._finish_checkpoint(request.invocation_id, "completed")

    async def _prepare_stream_request(
        self, request: InvocationRequest
    ) -> tuple[Any, Any]:
        """Build native request values after application resources are ready."""

        try:
            from google.adk.agents.run_config import RunConfig
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Google ADK is required to run an ADK agent") from exc

        await start_mcp_lifecycles(
            mcp_lifecycle_bindings(self.application.target)
        )

        message = types.Content(
            role="user", parts=[types.Part(text=_model_input_text(request.input))]
        )
        metadata = dict(request.metadata)
        run_config = RunConfig(custom_metadata=metadata) if metadata else None
        return message, run_config

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
            dump(mode="json", by_alias=True), separators=(",", ":")
        ).encode("utf-8")
        checkpoint_id = getattr(event, "id", None) or uuid.uuid4().hex
        existing = await store.get_checkpoint(
            run_id=request.invocation_id,
            checkpoint_id=checkpoint_id,
            namespace="events",
        )
        if existing is not None:
            return
        previous = await store.get_checkpoint(
            run_id=request.invocation_id, namespace="events"
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
        await store.put(record, expected_revision=None)

    async def _finish_checkpoint(self, run_id: str, status: str) -> None:
        """Release portable run ownership after ADK reaches a terminal outcome."""

        store = self._portable_checkpoints()
        if store is None:
            return
        record = await store.get_run(run_id=run_id)
        if record is not None and record.status == "running":
            await store.transition(
                run_id=run_id,
                expected_status="running",
                status=status,
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
        self._author: str | None = None
        self._pending_subagent_text = ""
        self._pending_subagent_author: str | None = None

    def feed(self, event: Any) -> list[dict[str, Any]]:
        """Normalize one event and apply the portable intermediate-output policy."""

        self._start_event(event)
        normalized = self._normalize_event(event)
        self._replace_pending_subagent_message(event, normalized)
        if not self._filters_subagent_event(event):
            return normalized
        return self._filter_subagent_messages(event, normalized)

    def _start_event(self, event: Any) -> None:
        """Reset cumulative state when native event authorship changes."""

        author = getattr(event, "author", None)
        if author != self._author:
            self._text = ""
            self._author = author

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
        for item in _event_items(event):
            if item["type"] != "message":
                normalized.append(item)
                continue
            delta = self._message_delta(event, str(item["text"]))
            if delta:
                normalized.append(
                    {"type": "message", "role": "assistant", "text": delta}
                )
            if not getattr(event, "partial", False):
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

    def finish(self) -> list[dict[str, Any]]:
        """Release a terminal child answer once no later tool call can follow it."""

        text = self._pending_subagent_text
        self._pending_subagent_text = ""
        self._pending_subagent_author = None
        if not text:
            return []
        return [{"type": "message", "role": "assistant", "text": text}]


def _event_items(event: Any) -> list[dict[str, Any]]:
    items, text = _content_items(event)
    items.extend(_output_items(event, text))
    items.extend(_function_call_items(event))
    items.extend(_function_response_items(event))
    return items


def _content_items(event: Any) -> tuple[list[dict[str, Any]], str]:
    items: list[dict[str, Any]] = []
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    text = "".join(
        part.text
        for part in _customer_facing_parts(parts)
        if isinstance(getattr(part, "text", None), str)
    )
    if text:
        items.append({"type": "message", "role": "assistant", "text": text})
    return items, text


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
        normalized_output = json_value(output)
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
                "result": json_value(getattr(response, "response", None)),
            }
        )
    return items


def _is_terminal_output(event: Any) -> bool:
    node_info = getattr(event, "node_info", None)
    output_for = getattr(node_info, "output_for", None)
    if not output_for:
        return True
    return any(isinstance(path, str) and "/" not in path for path in output_for)


def _session_record(session: Any) -> SessionRecord:
    """Preserve native ADK event metadata beside portable session state."""

    updated_at = _timestamp(getattr(session, "last_update_time", 0.0))
    return SessionRecord(
        id=session.id,
        user_id=session.user_id,
        state=json_value(session.state),
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
                metadata={"adk": record},
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
    """Expose visible text or structured tool output as portable content."""

    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    text = _adk_visible_text(parts)
    if text:
        return text
    return _adk_tool_response(parts) if role == "tool" else None


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
    return responses[0] if len(responses) == 1 else responses


def _adk_turn_metadata(events: list[Any]) -> dict[str, Any]:
    """Namespace complete native turn events for an authored metadata model."""

    return {"adk": {"events": events}}


def _adk_session_metadata(session: Any) -> dict[str, Any]:
    """Keep ADK-owned fields opaque while avoiding duplicate public state."""

    record = json_value(session)
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


def _create_runner(application: CompiledApplication, session_service: Any) -> Any:
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
            return _build_adk_runner(
                application,
                session_service,
                credential_service,
                InMemoryRunner,
                Runner,
            )
    return _build_adk_runner(
        application,
        session_service,
        credential_service,
        InMemoryRunner,
        Runner,
    )


def _build_adk_runner(
    application: CompiledApplication,
    session_service: Any,
    credential_service: Any,
    in_memory_runner: Any,
    runner: Any,
) -> Any:
    """Select the ADK runner that owns the configured session service."""

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
    return runner(
        app=application.native_app,
        app_name=application.native_app.name,
        session_service=session_service,
        credential_service=credential_service,
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
    session_service: Any, *, user_id: str, session_id: str
) -> AsyncIterator[None]:
    acquire = getattr(session_service, "execution_lease", None)
    if not callable(acquire):
        yield
        return
    async with acquire(user_id=user_id, session_id=session_id):
        yield


__all__ = ["ADKRuntimeDriver"]

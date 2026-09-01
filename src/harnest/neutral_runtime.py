"""Framework-neutral HTTP, SSE, and WebSocket runtime.

Backend drivers translate framework events into the deliberately small internal
event vocabulary in this module.  Everything that is part of Harnest's public
wire protocol lives here so ADK and LangGraph cannot drift independently.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from typing import Any, AsyncIterator, Mapping, Sequence
import uuid

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocket, WebSocketDisconnect

from .assets import (
    DEFAULT_MAX_ASSET_BYTES,
    AssetNotFoundError,
    AssetQuotaError,
    AssetRecord,
    AssetScope,
    AssetStore,
    MemoryAssetStore,
)
from .runtime_auth import (
    ANONYMOUS_USER_ID,
    Authenticator,
    _active_authenticated_principal,
    install_authentication,
    principal_for,
)
from .approval import (
    ApprovalDenied,
    ApprovalEnforcementError,
    ApprovalExpired,
    InMemoryApprovalStore,
)
from .client_tool import (
    ClientToolError,
    InMemoryClientToolStore,
)
from .server_config import format_byte_size, validate_max_request_bytes
from .http_routes import (
    HTTPRouteExtension,
    mount_http_route_extensions,
)
from .runtime_contract import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    NoCustomerFacingOutputError,
    ResponseRequest,
    RuntimeDriver,
    RuntimeEvent,
    SessionConflictError,
    SessionMessage,
    SessionRecord,
    require_customer_facing_output,
    response_request_model as _response_request_model,
)
from .runtime_continuation import (
    completed_payload as _completed_payload,
    json_client_requires_action as _json_client_requires_action,
    json_requires_action as _json_requires_action,
    next_non_event as _next_non_event,
)
from .runtime_invocation import (
    InvocationCoordinator,
    resolved_transport_input as _resolved_transport_input,
)
from .runtime_session_wire import (
    decode_session_cursor as _decode_session_cursor,
    encode_session_cursor as _encode_session_cursor,
    paginate_session_messages as _paginate_session_messages,
    session_message_payload as _session_message_payload,
    session_payload as _session_payload,
)
from .runtime_live import (
    live_session as _live_session,
    serve_live_frame as _serve_live_frame,
    validated_live_frame as _validated_live_frame,
)
from .runtime_sse import (
    commit_approval_decision as _commit_approval_decision,
    parse_approval_decision as _parse_approval_decision,
    resume_approval_run as _resume_approval_run,
    resumed_action_payload as _resumed_action_payload,
    sse_approval_run as _sse_approval_run,
)


MAX_REQUEST_BYTES = 1024 * 1024
MAX_LIVE_FRAMES = 1024
MAX_SESSION_PAGE_SIZE = 100
MAX_TRANSCRIPT_PAGE_SIZE = 100
NEUTRAL_USER_ID = ANONYMOUS_USER_ID


async def _read_request_body(request: Request, max_request_bytes: int) -> bytes:
    """Read a request incrementally and stop before an oversized body lands."""

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_request_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Request body exceeds "
                    f"{format_byte_size(max_request_bytes)}"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _asset_record_payload(record: AssetRecord) -> dict[str, Any]:
    """Render only reference and trusted metadata after a successful upload."""

    metadata = record.metadata
    value: dict[str, Any] = {
        "assetId": record.asset_id,
        "mediaType": record.media_type,
        "sizeBytes": record.size_bytes,
    }
    if metadata is not None:
        for public, internal in (
            ("width", "width"),
            ("height", "height"),
            ("durationSeconds", "duration_seconds"),
            ("pageCount", "page_count"),
            ("frameCount", "frame_count"),
            ("channels", "channel_count"),
            ("sampleRateHz", "sample_rate_hz"),
        ):
            item = getattr(metadata, internal, None)
            if item is not None:
                value[public] = item
    return value


def _parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one bounded inclusive byte range or reject ambiguous forms."""

    if value is None:
        return None
    if not _single_byte_range(value):
        raise ValueError("invalid asset range")
    start_text, separator, end_text = value[6:].partition("-")
    if not separator or not start_text.isdigit() or (
        end_text and not end_text.isdigit()
    ):
        raise ValueError("invalid asset range")
    start = int(start_text)
    end = size - 1 if not end_text else int(end_text)
    if start >= size or end < start:
        raise ValueError("invalid asset range")
    return start, min(end, size - 1)


def _single_byte_range(value: str) -> bool:
    """Reject range units and multipart forms the endpoint does not support."""

    return value.startswith("bytes=") and "," not in value


async def _asset_stream(
    store: AssetStore,
    scope: AssetScope,
    asset_id: str,
    *,
    start: int,
    end: int,
) -> AsyncIterator[bytes]:
    """Yield only the selected range without exposing identifiers on failure."""

    offset = 0
    async for chunk in store.open(scope=scope, asset_id=asset_id):
        chunk_end = offset + len(chunk) - 1
        if chunk_end >= start and offset <= end:
            local_start = max(0, start - offset)
            local_end = min(len(chunk), end - offset + 1)
            yield chunk[local_start:local_end]
        offset += len(chunk)
        if offset > end:
            return


def create_neutral_router(
    driver: RuntimeDriver,
    *,
    request_timeout: float = 300,
    max_concurrency: int = 8,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    approval_store: InMemoryApprovalStore | None = None,
    client_tool_store: InMemoryClientToolStore | None = None,
    asset_store: AssetStore | None = None,
    asset_stores: Mapping[str, AssetStore] | None = None,
    a2a_task_store: Any | None = None,
    http_routes: Sequence[HTTPRouteExtension] = (),
) -> Any:
    """Create the one Harnest router shared by every runtime backend."""

    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    if max_concurrency < 1:
        raise ValueError("max concurrency must be at least one")
    max_request_bytes = validate_max_request_bytes(max_request_bytes)
    try:
        from fastapi import APIRouter, HTTPException, Query
        from fastapi.responses import StreamingResponse
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The neutral runtime requires FastAPI") from exc

    router = APIRouter()
    semaphore = asyncio.Semaphore(max_concurrency)
    approvals = approval_store or InMemoryApprovalStore()
    assets = asset_store or MemoryAssetStore()
    stores = dict(asset_stores or {})
    stores.setdefault("default", assets)
    client_tools = client_tool_store or InMemoryClientToolStore(
        asset_stores=stores
    )
    external_continuations = getattr(driver, "external_continuations", None)
    from .runtime_content import ContentRuntimeDriver

    driver = ContentRuntimeDriver(driver, assets, stores)
    response_request_model = _response_request_model(driver.info.input_schema)
    coordinator = InvocationCoordinator(
        driver=driver,
        approvals=approvals,
        client_tools=client_tools,
        assets=assets,
        asset_stores=stores,
        semaphore=semaphore,
        request_timeout=request_timeout,
        max_request_bytes=max_request_bytes,
        external_continuations=external_continuations,
    )

    async def read_json(request: Request) -> dict[str, Any]:
        """Read one bounded request body as a strict JSON object."""

        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type.strip().lower() != "application/json":
            raise HTTPException(
                status_code=415, detail="Content-Type must be application/json"
            )
        body = await _read_request_body(request, max_request_bytes)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        return value

    def parse_response(payload: Mapping[str, Any]) -> BaseModel:
        """Validate the public envelope without changing established HTTP errors."""

        try:
            parsed = response_request_model.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid response request"
            ) from exc
        return parsed

    async def asset_scope(session_id: str, request: Request) -> AssetScope:
        """Authorize an asset route through the same session ownership boundary."""

        user_id = principal_for(request).user_id
        session = await driver.get_session(session_id=session_id, user_id=user_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return AssetScope(user_id=user_id, session_id=session_id)

    def streaming_invocation(
        text: str,
        session_id: str,
        response_id: str,
        metadata: Mapping[str, Any],
        user_id: str,
        transport: str | None = None,
    ) -> InvocationRequest:
        """Adapt legacy streaming call sites to the shared request factory."""

        return coordinator.create_request(
            text,
            user_id=user_id,
            session_id=session_id,
            invocation_id=response_id,
            metadata=metadata,
            transport=transport or "response",
        )

    async def invoke_from_http_route(
        connection: Any,
        input_value: Any,
        session_id: str | None,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Invoke custom routes through the same policy path as `/responses`."""

        principal = principal_for(connection)
        active_principal = _active_authenticated_principal()
        if principal.user_id != ANONYMOUS_USER_ID and active_principal is not principal:
            # Request.state outlives authentication's revocable ContextVar. Do
            # not let detached tasks retain authority by holding the Request.
            raise HTTPException(
                status_code=401, detail="Authentication context is no longer active"
            )
        user_id = principal.user_id
        run = await coordinator.prepare_request(
            input_value,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata,
            transport="custom_http",
            validate_before_session=False,
        )
        return await coordinator.invoke_json(run)

    @router.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Expose a dependency-free process liveness signal."""

        return {"status": "ok"}

    @router.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> Mapping[str, Any]:
        """Return the compiled agent card without framework translation."""

        return driver.info.card

    @router.get("/agent")
    async def agent_info() -> dict[str, Any]:
        """Describe the portable agent and its enabled transport endpoints."""

        info = driver.info
        value: dict[str, Any] = {
            "id": info.id,
            "name": info.name,
            "description": info.description,
            "card": dict(info.card),
            "endpoints": {
                "responses": "/responses",
                "responseStatus": "/responses/{responseId}",
                "sessions": "/sessions",
                "sessionMessages": "/sessions/{sessionId}/messages",
                "assets": "/sessions/{sessionId}/assets",
                "asset": "/sessions/{sessionId}/assets/{assetId}",
                "live": "/live",
                "approvals": "/approvals/{approvalId}",
                "clientTools": "/client-tools/{requestId}",
                **dict(info.extra_endpoints),
            },
        }
        if info.framework is not None:
            value["framework"] = info.framework
        if info.mode is not None:
            value["mode"] = info.mode
        if info.lifecycle_coverage:
            value["lifecycleCoverage"] = dict(info.lifecycle_coverage)
        return value

    @router.post("/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        """Create one caller-owned session with optional initial state."""

        payload = await read_json(request)
        if not set(payload) <= {"id", "state"}:
            raise HTTPException(status_code=400, detail="Invalid session request")
        session_id = payload.get("id", uuid.uuid4().hex)
        state = payload.get("state", {})
        if not isinstance(session_id, str) or not session_id.strip():
            raise HTTPException(status_code=400, detail="id must be non-empty")
        if not isinstance(state, dict):
            raise HTTPException(status_code=400, detail="state must be an object")
        try:
            session = await driver.create_session(
                session_id=session_id,
                user_id=principal_for(request).user_id,
                state=state,
            )
        except SessionConflictError as exc:
            raise HTTPException(status_code=409, detail="Session already exists") from exc
        return _session_payload(session)

    @router.get("/sessions")
    async def list_sessions(
        request: Request,
        limit: int | None = Query(
            default=None, ge=1, le=MAX_SESSION_PAGE_SIZE
        ),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one bounded user-scoped session keyset page."""

        user_id = principal_for(request).user_id
        try:
            after = (
                None
                if cursor is None
                else _decode_session_cursor(cursor, user_id=user_id)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid session cursor"
            ) from exc
        page_size = limit or MAX_SESSION_PAGE_SIZE
        sessions = await driver.list_sessions(
            user_id=user_id,
            after=after,
            limit=page_size + 1,
        )
        page = tuple(sessions[:page_size])
        next_cursor = None
        if len(sessions) > page_size:
            next_cursor = _encode_session_cursor(
                after=page[-1].id, user_id=user_id
            )
        return {
            "sessions": [_session_payload(session) for session in page],
            "nextCursor": next_cursor,
        }

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        """Return a session only when it belongs to the active principal."""

        session = await driver.get_session(
            session_id=session_id,
            user_id=principal_for(request).user_id,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_payload(session)

    @router.get("/sessions/{session_id}/messages")
    async def get_session_messages(
        session_id: str,
        request: Request,
        limit: int | None = Query(
            default=None, ge=1, le=MAX_TRANSCRIPT_PAGE_SIZE
        ),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one bounded, cursor-based transcript page."""

        principal = principal_for(request)
        messages = await driver.get_session_messages(
            session_id=session_id,
            user_id=principal.user_id,
        )
        if messages is None:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            page, next_cursor = _paginate_session_messages(
                messages,
                limit=limit or MAX_TRANSCRIPT_PAGE_SIZE,
                cursor=cursor,
                session_id=session_id,
                user_id=principal.user_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid transcript cursor"
            ) from exc
        return {
            "sessionId": session_id,
            "userId": principal.user_id,
            "messages": [_session_message_payload(message) for message in page],
            "nextCursor": next_cursor,
        }

    @router.post("/sessions/{session_id}/assets", status_code=201)
    async def upload_asset(session_id: str, request: Request) -> dict[str, Any]:
        """Inspect and atomically store one authenticated session asset."""

        scope = await asset_scope(session_id, request)
        media_type = request.headers.get("content-type", "").partition(";")[0].strip()
        if not media_type:
            raise HTTPException(status_code=415, detail="Asset Content-Type is required")
        body = await _read_request_body(request, DEFAULT_MAX_ASSET_BYTES)
        try:
            from .asset_inspection import inspect_asset

            inspected_metadata, inspected_type = inspect_asset(body, media_type)

            async def chunks() -> AsyncIterator[bytes]:
                """Yield the inspected body once to the streaming store API."""

                yield body

            record = await assets.save(
                scope=scope,
                media_type=inspected_type,
                chunks=chunks(),
                metadata=inspected_metadata,
            )
        except AssetQuotaError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        return _asset_record_payload(record)

    @router.head("/sessions/{session_id}/assets/{asset_id}")
    async def stat_asset(session_id: str, asset_id: str, request: Request) -> Response:
        """Return trusted asset metadata without reading its content."""

        scope = await asset_scope(session_id, request)
        record = await assets.stat(scope=scope, asset_id=asset_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        return Response(
            status_code=200,
            media_type=record.media_type,
            headers={
                "Content-Length": str(record.size_bytes),
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/sessions/{session_id}/assets/{asset_id}")
    async def download_asset(session_id: str, asset_id: str, request: Request) -> Any:
        """Stream one authenticated asset with single-range support."""

        scope = await asset_scope(session_id, request)
        record = await assets.stat(scope=scope, asset_id=asset_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Asset not found")
        try:
            selected = _parse_byte_range(request.headers.get("range"), record.size_bytes)
        except ValueError as exc:
            raise HTTPException(
                status_code=416,
                detail="Invalid asset range",
                headers={"Content-Range": f"bytes */{record.size_bytes}"},
            ) from exc
        start, end = selected or (0, record.size_bytes - 1)
        headers = {
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if selected is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{record.size_bytes}"
        return StreamingResponse(
            _asset_stream(assets, scope, asset_id, start=start, end=end),
            status_code=206 if selected is not None else 200,
            media_type=record.media_type,
            headers=headers,
        )

    @router.delete("/sessions/{session_id}/assets/{asset_id}", status_code=204)
    async def delete_asset(session_id: str, asset_id: str, request: Request) -> Response:
        """Delete one owned asset without revealing cross-scope existence."""

        scope = await asset_scope(session_id, request)
        if not await assets.delete(scope=scope, asset_id=asset_id):
            raise HTTPException(status_code=404, detail="Asset not found")
        return Response(status_code=204)

    @router.patch("/sessions/{session_id}")
    async def update_session(session_id: str, request: Request) -> dict[str, Any]:
        """Apply one state delta within the caller's session scope."""

        payload = await read_json(request)
        if set(payload) != {"stateDelta"} or not isinstance(
            payload.get("stateDelta"), dict
        ):
            raise HTTPException(status_code=400, detail="Expected stateDelta object")
        session = await driver.update_session(
            session_id=session_id,
            user_id=principal_for(request).user_id,
            state_delta=payload["stateDelta"],
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_payload(session)

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, request: Request) -> Response:
        """Delete a caller-owned session and its scoped assets together."""

        user_id = principal_for(request).user_id
        deleted = await driver.delete_session(
            session_id=session_id,
            user_id=user_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        await assets.delete_scope(scope=AssetScope(user_id, session_id))
        return Response(status_code=204)

    @router.post(
        "/responses",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": response_request_model.model_json_schema(by_alias=True)
                    }
                },
            }
        },
    )
    async def responses(request: Request) -> Any:
        """Adapt one public response request to the shared invocation coordinator."""

        payload = await read_json(request)
        parsed = parse_response(payload)
        stream, metadata = parsed.stream, parsed.metadata
        user_id = principal_for(request).user_id
        run = await coordinator.prepare_request(
            parsed.input,
            user_id=user_id,
            session_id=parsed.session_id,
            metadata=metadata,
            transport="stream" if stream else "response",
        )
        if not stream:
            return await coordinator.invoke_json(run)

        return StreamingResponse(
            _sse_approval_run(
                store=approvals,
                client_tools=client_tools,
                driver=driver,
                request=run,
                semaphore=semaphore,
                request_timeout=request_timeout,
                response_id=run.invocation_id,
                session_id=run.session_id,
                metadata=metadata,
                external_continuations=external_continuations,
            ),
            media_type="text/event-stream",
        )

    @router.get("/responses/{response_id}")
    async def response_status(
        response_id: str,
        request: Request,
        session_id: str = Query(alias="sessionId"),
    ) -> dict[str, Any]:
        """Poll one external wait without revealing cross-scope existence."""

        if not session_id.strip():
            raise HTTPException(status_code=400, detail="sessionId must be non-empty")
        return await coordinator.poll_json(
            response_id=response_id,
            user_id=principal_for(request).user_id,
            session_id=session_id,
        )

    @router.post("/client-tools/{tool_request_id}")
    async def submit_client_tool(tool_request_id: str, request: Request) -> Any:
        """Resume the suspended run owning one client-tool request."""

        payload = await read_json(request)
        if set(payload) != {"output"}:
            raise HTTPException(status_code=400, detail="Expected client tool output")
        try:
            pending = await client_tools.submit(
                tool_request_id,
                user_id=principal_for(request).user_id,
                output=payload["output"],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Client tool request not found") from exc
        except ClientToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            deadline = asyncio.get_running_loop().time() + request_timeout
            async with semaphore:
                kind, value = await _next_non_event(pending.run, deadline=deadline)
            return _resumed_action_payload(
                kind,
                value,
                response_id=pending.call_id,
                session_id=pending.session_id,
                client_tool_id=pending.id,
            )
        except asyncio.TimeoutError as exc:
            approvals.cancel_run(pending.run)
            raise HTTPException(status_code=504, detail="Response timed out") from exc

    @router.post("/approvals/{approval_id}")
    async def decide_approval(approval_id: str, request: Request) -> Any:
        """Record one approval decision and resume its exact suspended run."""

        payload = await read_json(request)
        decision = _parse_approval_decision(payload)
        pending = _commit_approval_decision(
            approvals,
            approval_id,
            user_id=principal_for(request).user_id,
            decision=decision,
        )
        if decision == "deny":
            return {"id": pending.id, "status": "denied"}
        approval_run = approvals.run_for(pending)
        if approval_run is None:
            raise HTTPException(status_code=409, detail="Approval execution is unavailable")
        try:
            kind, value = await _resume_approval_run(
                approvals,
                pending,
                approval_run,
                semaphore=semaphore,
                request_timeout=request_timeout,
            )
            if kind == "approval":
                required = _json_requires_action(
                    value,
                    response_id=pending.call_id,
                    session_id=pending.session_id,
                )
                required["approvalId"] = value.id
                return required
            if kind == "client_tool":
                return _json_client_requires_action(
                    value,
                    response_id=pending.call_id,
                    session_id=pending.session_id,
                )
            if kind == "error":
                raise value
            if kind != "result":
                raise RuntimeError("unexpected approval run notification")
            result = value
            require_customer_facing_output(result.text, result.result)
        except asyncio.CancelledError:
            approvals.cancel_run(approval_run)
            raise
        except (ApprovalDenied, ApprovalExpired, ApprovalEnforcementError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except asyncio.TimeoutError as exc:
            approvals.cancel_run(approval_run)
            raise HTTPException(status_code=504, detail="Response timed out") from exc
        completed = _completed_payload(
            response_id=pending.call_id,
            session_id=result.session_id,
            sequence=0,
            events=result.events,
            text=result.text,
            metadata=result.metadata,
            result=result.result,
        )
        completed.pop("type")
        completed.pop("sequence")
        completed["id"] = completed.pop("responseId")
        completed["approvalId"] = pending.id
        return completed

    @router.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        """Serve live frames through the coordinator's shared request contract."""

        await websocket.accept()
        try:
            user_id = principal_for(websocket).user_id
            session = await _live_session(
                websocket,
                lambda payload: coordinator.resolve_session(
                    user_id=user_id,
                    session_id=payload.get("sessionId"),
                ),
            )
            if session is None:
                return
            frame_count = 0
            while True:
                frame = await websocket.receive_json()
                frame_count += 1
                if frame_count > MAX_LIVE_FRAMES:
                    await websocket.close(code=1008, reason="Frame limit exceeded")
                    return
                if frame == {"type": "session.close"}:
                    await websocket.close(code=1000)
                    return
                validated_frame, error = _validated_live_frame(
                    frame,
                    max_request_bytes,
                    driver.info.input_schema,
                )
                if error is not None:
                    await websocket.send_json({"type": "error", "error": error})
                    continue
                try:
                    resolved_input = await _resolved_transport_input(
                        validated_frame["input"],
                        driver.info.input_schema,
                        assets,
                        AssetScope(user_id=user_id, session_id=session.id),
                        stores=stores,
                    )
                except HTTPException:
                    await websocket.send_json(
                        {"type": "error", "error": "Invalid content input"}
                    )
                    continue
                validated_frame = {**validated_frame, "input": resolved_input}
                await _serve_live_frame(
                    websocket,
                    validated_frame,
                    session,
                    invocation=streaming_invocation,
                    semaphore=semaphore,
                    driver=driver,
                    approval_store=approvals,
                    client_tool_store=client_tools,
                    external_continuations=external_continuations,
                    request_timeout=request_timeout,
                )
        except WebSocketDisconnect:
            pass

    # Factories capture an unbound invoker during compilation. Bind only after
    # the final wrapped driver and shared continuation stores are available.
    mount_http_route_extensions(router, http_routes, invoke_from_http_route)
    from .runtime_a2a import mount_a2a_routes

    mount_a2a_routes(
        router,
        driver=driver,
        coordinator=coordinator,
        task_store=a2a_task_store,
    )
    return router


def create_neutral_app(
    driver: RuntimeDriver,
    *,
    request_timeout: float = 300,
    max_concurrency: int = 8,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    playground_enabled: bool = True,
    authenticator: Authenticator | None = None,
    approval_store: InMemoryApprovalStore | None = None,
    client_tool_store: InMemoryClientToolStore | None = None,
    asset_store: AssetStore | None = None,
    a2a_task_store: Any | None = None,
    http_routes: Sequence[HTTPRouteExtension] = (),
    lifecycle_extensions: Sequence[Any] = (),
    playground_eval_service: Any | None = None,
) -> Any:
    """Convenience application for drivers that do not mount native routes."""

    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The neutral runtime requires FastAPI") from exc

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        """Eagerly start and close the fully wrapped application runtime."""

        from .runtime_pipeline import start_runtime_pipeline

        try:
            # Host startup surfaces configuration failures before the process
            # accepts traffic, while direct invocation retains safe lazy start.
            await start_runtime_pipeline(driver)
            yield
        finally:
            await runtime_driver.close()

    runtime_driver = driver
    trace_store = None
    if playground_enabled:
        from .playground_trace import (
            PlaygroundTraceRuntimeDriver,
            PlaygroundTraceStore,
        )

        trace_store = PlaygroundTraceStore()
        runtime_driver = PlaygroundTraceRuntimeDriver(driver, trace_store)

    app = FastAPI(title=f"Harnest: {runtime_driver.info.name}", lifespan=lifespan)
    from .playground import create_playground_router
    from .http_lifecycle import install_http_lifecycle
    from .server_limits import install_request_size_limit

    install_request_size_limit(app, max_request_bytes)
    if playground_enabled:
        app.include_router(
            create_playground_router(trace_store, playground_eval_service)
        )
    app.include_router(
        create_neutral_router(
            runtime_driver,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            approval_store=approval_store,
            client_tool_store=client_tool_store,
            asset_store=asset_store,
            a2a_task_store=a2a_task_store,
            http_routes=http_routes,
        )
    )
    # Authentication is added last and therefore runs outside this middleware,
    # allowing HTTP lifecycle contexts to observe only the verified user ID.
    install_http_lifecycle(app, lifecycle_extensions)
    install_authentication(app, authenticator)
    return app


__all__ = [
    "AgentInfo",
    "InvocationRequest",
    "InvocationResult",
    "MAX_LIVE_FRAMES",
    "MAX_REQUEST_BYTES",
    "NEUTRAL_USER_ID",
    "NoCustomerFacingOutputError",
    "ResponseRequest",
    "RuntimeDriver",
    "RuntimeEvent",
    "SessionConflictError",
    "SessionMessage",
    "SessionRecord",
    "create_neutral_app",
    "create_neutral_router",
    "require_customer_facing_output",
]

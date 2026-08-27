"""Framework-neutral HTTP, SSE, and WebSocket runtime.

Backend drivers translate framework events into the deliberately small internal
event vocabulary in this module.  Everything that is part of Harnest's public
wire protocol lives here so ADK and LangGraph cannot drift independently.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
import json
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence, runtime_checkable
import uuid

from starlette.requests import Request
from starlette.responses import Response
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocket, WebSocketDisconnect


MAX_REQUEST_BYTES = 1024 * 1024
MAX_LIVE_FRAMES = 1024
NEUTRAL_USER_ID = "_harnest_neutral"

RuntimeEvent = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """Portable metadata needed by the neutral server."""

    id: str
    name: str
    description: str
    card: Mapping[str, Any]
    framework: str | None = None
    mode: str | None = None
    extra_endpoints: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A backend-independent view of a persisted agent session."""

    id: str
    user_id: str
    state: Mapping[str, Any]
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    """The complete input passed from a transport to a runtime driver."""

    input: str
    user_id: str
    session_id: str
    invocation_id: str
    metadata: Mapping[str, Any]
    state_delta: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """The normalized result of a non-streaming invocation."""

    text: str
    events: Sequence[RuntimeEvent]
    result: Any
    session_id: str
    metadata: Mapping[str, Any]


@runtime_checkable
class RuntimeDriver(Protocol):
    """The only interface the neutral server requires from a framework."""

    @property
    def info(self) -> AgentInfo: ...

    async def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state: Mapping[str, Any],
    ) -> SessionRecord: ...

    async def get_session(
        self, *, session_id: str, user_id: str
    ) -> SessionRecord | None: ...

    async def list_sessions(self, *, user_id: str) -> Sequence[SessionRecord]: ...

    async def update_session(
        self,
        *,
        session_id: str,
        user_id: str,
        state_delta: Mapping[str, Any],
    ) -> SessionRecord | None: ...

    async def delete_session(self, *, session_id: str, user_id: str) -> bool: ...

    async def invoke(self, request: InvocationRequest) -> InvocationResult: ...

    def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]: ...

    async def close(self) -> None: ...


class SessionConflictError(RuntimeError):
    """Raised by a driver when a requested session id already exists."""


class NoCustomerFacingOutputError(RuntimeError):
    """Raised when a completed invocation contains no public answer or result."""


def require_customer_facing_output(text: str, result: Any) -> None:
    """Reject reasoning-only completions without exposing their hidden content."""

    if text.strip() or result is not None:
        return
    # Returning an empty successful response makes provider failures look like
    # valid agent behavior. The error remains provider-neutral and keeps hidden
    # chain-of-thought out of every public transport.
    raise NoCustomerFacingOutputError(
        "Agent completed without customer-facing output"
    )


def _session_payload(session: SessionRecord) -> dict[str, Any]:
    return {"id": session.id, "state": dict(session.state)}


def _public_output_item(event: RuntimeEvent) -> dict[str, Any]:
    event_type = event.get("type")
    if event_type == "message":
        return {
            "type": "message",
            "role": event.get("role", "assistant"),
            "content": [{"type": "output_text", "text": event.get("text", "")}],
        }
    if event_type == "tool_call":
        return {
            "type": "tool_call",
            "id": event.get("id"),
            "name": event.get("name"),
            "arguments": event.get("arguments"),
        }
    if event_type == "tool_result":
        return {
            "type": "tool_result",
            "callId": event.get("id", event.get("callId")),
            "name": event.get("name"),
            "output": event.get("result", event.get("output")),
        }
    if event_type == "graph_output":
        return {"type": "output", "value": event.get("output")}
    if event_type == "output":
        return {"type": "output", "value": event.get("value")}
    raise ValueError(f"unsupported runtime event type: {event_type!r}")


def _public_output(events: Sequence[RuntimeEvent]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for event in events:
        item = _public_output_item(event)
        if item["type"] == "message" and output and output[-1]["type"] == "message":
            output[-1]["content"][0]["text"] += item["content"][0]["text"]
        else:
            output.append(item)
    return output


def _stream_frame(
    event: RuntimeEvent,
    *,
    sequence: int,
    response_id: str,
    session_id: str,
    request_id: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    common: dict[str, Any] = {
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
    }
    if request_id is not None:
        common["requestId"] = request_id
    event_type = event.get("type")
    if event_type == "message":
        return "response.text.delta", {
            **common,
            "type": "response.text.delta",
            "delta": event.get("text", ""),
        }
    if event_type == "tool_call":
        return "response.tool_call", {
            **common,
            "type": "response.tool_call",
            "id": event.get("id"),
            "name": event.get("name"),
            "arguments": event.get("arguments"),
        }
    if event_type == "tool_result":
        return "response.tool_result", {
            **common,
            "type": "response.tool_result",
            "callId": event.get("id", event.get("callId")),
            "name": event.get("name"),
            "output": event.get("result", event.get("output")),
        }
    # Graph/output events belong in the completed response, not a transport-
    # specific incremental frame.
    if event_type in {"graph_output", "output"}:
        return None
    raise ValueError(f"unsupported runtime event type: {event_type!r}")


def _completed_payload(
    *,
    response_id: str,
    session_id: str,
    sequence: int,
    events: Sequence[RuntimeEvent],
    text: str,
    metadata: Mapping[str, Any],
    result: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "response.completed",
        "sequence": sequence,
        "responseId": response_id,
        "sessionId": session_id,
        "status": "completed",
        "outputText": text,
        "output": _public_output(events),
        "metadata": dict(metadata),
    }
    if request_id is not None:
        payload["requestId"] = request_id
    if result is not None:
        payload["result"] = result
    return payload


def _final_event_result(events: Sequence[RuntimeEvent]) -> Any:
    """Read the portable result carried by the final output event."""

    for event in reversed(events):
        event_type = event.get("type")
        if event_type not in {"graph_output", "output"}:
            continue
        if "result" in event:
            return event["result"]
        return event.get("output") if event_type == "graph_output" else event.get("value")
    return None


def _sse(event_name: str, payload: Mapping[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@dataclass(slots=True)
class _LiveStreamState:
    """Retain sequence progress so mid-stream errors keep the wire contract."""

    events: list[RuntimeEvent] = field(default_factory=list)
    sequence: int = 1


async def _live_session(websocket: Any, response_session: Any) -> SessionRecord | None:
    envelope = await websocket.receive_json()
    if (
        not isinstance(envelope, dict)
        or not set(envelope) <= {"type", "sessionId"}
        or envelope.get("type") != "connect"
    ):
        await websocket.send_json(
            {"type": "error", "error": "First frame must be a connect request"}
        )
        await websocket.close(code=1008)
        return None
    try:
        session = await response_session(envelope)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "error": exc.detail})
        await websocket.close(code=1008)
        return None
    await websocket.send_json({"type": "session.connected", "sessionId": session.id})
    return session


def _live_frame_error(frame: Any) -> str | None:
    if (
        not isinstance(frame, dict)
        or not set(frame) <= {"type", "input", "requestId", "metadata"}
        or frame.get("type") != "response.create"
        or not isinstance(frame.get("input"), str)
        or not frame["input"].strip()
    ):
        return "Invalid live input frame"
    if len(frame["input"].encode("utf-8")) > MAX_REQUEST_BYTES:
        return "Input exceeds 1 MiB"
    if not isinstance(frame.get("metadata", {}), dict):
        return "metadata must be an object"
    request_id = frame.get("requestId")
    if request_id is not None and not isinstance(request_id, str):
        return "requestId must be a string"
    return None


async def _live_stream_events(
    websocket: Any,
    source: AsyncIterator[RuntimeEvent],
    with_deadline: Any,
    state: _LiveStreamState,
    *,
    response_id: str,
    session_id: str,
    request_id: str | None,
) -> None:
    async with aclosing(source) as stream:
        async for event in with_deadline(stream):
            state.events.append(event)
            normalized = _stream_frame(
                event,
                sequence=state.sequence,
                response_id=response_id,
                session_id=session_id,
                request_id=request_id,
            )
            if normalized is None:
                continue
            _event_name, data = normalized
            await websocket.send_json(data)
            state.sequence += 1


async def _serve_live_frame(
    websocket: Any,
    frame: Mapping[str, Any],
    session: SessionRecord,
    *,
    invocation: Any,
    with_deadline: Any,
    semaphore: asyncio.Semaphore,
    driver: RuntimeDriver,
) -> None:
    response_id = f"resp_{uuid.uuid4().hex}"
    request_id = frame.get("requestId")
    metadata = frame.get("metadata", {})
    await websocket.send_json(
        {
            "type": "response.created",
            "sequence": 0,
            "responseId": response_id,
            "sessionId": session.id,
            "requestId": request_id,
            "metadata": metadata,
        }
    )
    run = invocation(frame["input"], session.id, response_id, metadata)
    state = _LiveStreamState()
    try:
        async with semaphore:
            await _live_stream_events(
                websocket,
                driver.stream(run),
                with_deadline,
                state,
                response_id=response_id,
                session_id=session.id,
                request_id=request_id,
            )
        text_output = "".join(
            str(event.get("text", ""))
            for event in state.events
            if event.get("type") == "message"
        )
        result_value = _final_event_result(state.events)
        require_customer_facing_output(text_output, result_value)
        await websocket.send_json(
            _completed_payload(
                response_id=response_id,
                session_id=session.id,
                sequence=state.sequence,
                events=state.events,
                text=text_output,
                metadata=metadata,
                result=result_value,
                request_id=request_id,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await websocket.send_json(
            {
                "type": "error",
                "sequence": state.sequence,
                "responseId": response_id,
                "sessionId": session.id,
                "requestId": request_id,
                "error": (
                    "Response timed out"
                    if isinstance(exc, asyncio.TimeoutError)
                    else str(exc)
                ),
            }
        )


def create_neutral_router(
    driver: RuntimeDriver,
    *,
    request_timeout: float = 300,
    max_concurrency: int = 8,
) -> Any:
    """Create the one Harnest router shared by every runtime backend."""

    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    if max_concurrency < 1:
        raise ValueError("max concurrency must be at least one")
    try:
        from fastapi import APIRouter, HTTPException
        from fastapi.responses import StreamingResponse
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The neutral runtime requires FastAPI") from exc

    router = APIRouter()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def read_json(request: Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type.strip().lower() != "application/json":
            raise HTTPException(
                status_code=415, detail="Content-Type must be application/json"
            )
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Request body exceeds 1 MiB")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        return value

    def parse_response(payload: Mapping[str, Any]) -> tuple[str, bool, dict[str, Any]]:
        if not set(payload) <= {"input", "sessionId", "stream", "metadata"}:
            raise HTTPException(status_code=400, detail="Invalid response request")
        text = payload.get("input")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="input must be non-empty")
        if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Input exceeds 1 MiB")
        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            raise HTTPException(status_code=400, detail="stream must be boolean")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=400, detail="metadata must be an object")
        return text, stream, metadata

    async def response_session(payload: Mapping[str, Any]) -> SessionRecord:
        session_id = payload.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise HTTPException(status_code=400, detail="sessionId must be non-empty")
        if session_id is None:
            return await driver.create_session(
                session_id=uuid.uuid4().hex, user_id=NEUTRAL_USER_ID, state={}
            )
        session = await driver.get_session(
            session_id=session_id, user_id=NEUTRAL_USER_ID
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def invocation(
        text: str,
        session_id: str,
        response_id: str,
        metadata: Mapping[str, Any],
    ) -> InvocationRequest:
        return InvocationRequest(
            input=text,
            user_id=NEUTRAL_USER_ID,
            session_id=session_id,
            invocation_id=response_id,
            metadata=dict(metadata),
            state_delta={},
        )

    async def with_deadline(
        source: AsyncIterator[RuntimeEvent],
    ) -> AsyncIterator[RuntimeEvent]:
        """Apply one total deadline while consuming the source in one task.

        Calling ``wait_for(source.__anext__())`` repeatedly creates a new task
        for every event. Frameworks such as ADK keep OpenTelemetry context over
        the lifetime of their async generator, so hopping tasks corrupts their
        context tokens. A single producer owns the generator; only queue waits
        are timed.
        """

        queue: asyncio.Queue[tuple[bool, Any]] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for event in source:
                    await queue.put((True, event))
            except BaseException as exc:
                queue.put_nowait((False, exc))
            else:
                queue.put_nowait((False, None))

        producer = asyncio.create_task(produce())
        deadline = asyncio.get_running_loop().time() + request_timeout
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                is_event, value = await asyncio.wait_for(
                    queue.get(), timeout=remaining
                )
                if is_event:
                    yield value
                elif value is None:
                    return
                else:
                    raise value
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    @router.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/.well-known/agent-card.json", include_in_schema=False)
    async def agent_card() -> Mapping[str, Any]:
        return driver.info.card

    @router.get("/agent")
    async def agent_info() -> dict[str, Any]:
        info = driver.info
        value: dict[str, Any] = {
            "id": info.id,
            "name": info.name,
            "description": info.description,
            "card": dict(info.card),
            "endpoints": {
                "responses": "/responses",
                "sessions": "/sessions",
                "live": "/live",
                **dict(info.extra_endpoints),
            },
        }
        if info.framework is not None:
            value["framework"] = info.framework
        if info.mode is not None:
            value["mode"] = info.mode
        return value

    @router.post("/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
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
                session_id=session_id, user_id=NEUTRAL_USER_ID, state=state
            )
        except SessionConflictError as exc:
            raise HTTPException(status_code=409, detail="Session already exists") from exc
        return _session_payload(session)

    @router.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        sessions = await driver.list_sessions(user_id=NEUTRAL_USER_ID)
        return {
            "sessions": [
                _session_payload(session)
                for session in sorted(sessions, key=lambda item: item.id)
            ]
        }

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = await driver.get_session(
            session_id=session_id, user_id=NEUTRAL_USER_ID
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_payload(session)

    @router.patch("/sessions/{session_id}")
    async def update_session(session_id: str, request: Request) -> dict[str, Any]:
        payload = await read_json(request)
        if set(payload) != {"stateDelta"} or not isinstance(
            payload.get("stateDelta"), dict
        ):
            raise HTTPException(status_code=400, detail="Expected stateDelta object")
        session = await driver.update_session(
            session_id=session_id,
            user_id=NEUTRAL_USER_ID,
            state_delta=payload["stateDelta"],
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return _session_payload(session)

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> Response:
        deleted = await driver.delete_session(
            session_id=session_id, user_id=NEUTRAL_USER_ID
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        return Response(status_code=204)

    @router.post("/responses")
    async def responses(request: Request) -> Any:
        payload = await read_json(request)
        text, stream, metadata = parse_response(payload)
        session = await response_session(payload)
        response_id = f"resp_{uuid.uuid4().hex}"
        run = invocation(text, session.id, response_id, metadata)
        if not stream:
            try:
                async with semaphore:
                    result = await asyncio.wait_for(
                        driver.invoke(run), timeout=request_timeout
                    )
                    require_customer_facing_output(result.text, result.result)
            except asyncio.TimeoutError as exc:
                raise HTTPException(status_code=504, detail="Response timed out") from exc
            except NoCustomerFacingOutputError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            completed = _completed_payload(
                response_id=response_id,
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
            return completed

        async def event_stream() -> AsyncIterator[str]:
            events: list[RuntimeEvent] = []
            sequence = 0
            created = {
                "type": "response.created",
                "sequence": sequence,
                "responseId": response_id,
                "sessionId": session.id,
                "metadata": metadata,
            }
            yield _sse("response.created", created)
            sequence += 1
            try:
                async with semaphore:
                    async with aclosing(driver.stream(run)) as source:
                        async for event in with_deadline(source):
                            events.append(event)
                            frame = _stream_frame(
                                event,
                                sequence=sequence,
                                response_id=response_id,
                                session_id=session.id,
                            )
                            if frame is None:
                                continue
                            event_name, data = frame
                            yield _sse(event_name, data)
                            sequence += 1
                text_output = "".join(
                    str(event.get("text", ""))
                    for event in events
                    if event.get("type") == "message"
                )
                result_value = _final_event_result(events)
                require_customer_facing_output(text_output, result_value)
                completed = _completed_payload(
                    response_id=response_id,
                    session_id=session.id,
                    sequence=sequence,
                    events=events,
                    text=text_output,
                    metadata=metadata,
                    result=result_value,
                )
                yield _sse("response.completed", completed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = {
                    "type": "error",
                    "sequence": sequence,
                    "responseId": response_id,
                    "sessionId": session.id,
                    "error": (
                        "Response timed out"
                        if isinstance(exc, asyncio.TimeoutError)
                        else str(exc)
                    ),
                }
                yield _sse("error", error)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            session = await _live_session(websocket, response_session)
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
                error = _live_frame_error(frame)
                if error is not None:
                    await websocket.send_json({"type": "error", "error": error})
                    continue
                await _serve_live_frame(
                    websocket,
                    frame,
                    session,
                    invocation=invocation,
                    with_deadline=with_deadline,
                    semaphore=semaphore,
                    driver=driver,
                )
        except WebSocketDisconnect:
            pass

    return router


def create_neutral_app(
    driver: RuntimeDriver,
    *,
    request_timeout: float = 300,
    max_concurrency: int = 8,
) -> Any:
    """Convenience application for drivers that do not mount native routes."""

    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("The neutral runtime requires FastAPI") from exc
    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await driver.close()

    app = FastAPI(title=f"Harnest: {driver.info.name}", lifespan=lifespan)
    app.include_router(
        create_neutral_router(
            driver,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
        )
    )
    return app


__all__ = [
    "AgentInfo",
    "InvocationRequest",
    "InvocationResult",
    "MAX_LIVE_FRAMES",
    "MAX_REQUEST_BYTES",
    "NEUTRAL_USER_ID",
    "NoCustomerFacingOutputError",
    "RuntimeDriver",
    "RuntimeEvent",
    "SessionConflictError",
    "SessionRecord",
    "create_neutral_app",
    "create_neutral_router",
    "require_customer_facing_output",
]

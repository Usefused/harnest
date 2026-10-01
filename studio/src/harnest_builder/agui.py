"""Stream Studio authoring runs using AG-UI, with bounded private runtime events."""

import asyncio
from contextlib import suppress
from contextvars import ContextVar
import json
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from harnest.runtime_agui_input import AGUIInput
from pydantic import ValidationError

from . import mcp_routes
from .prompting import Prompt, propose

SINK = ContextVar("studio_progress", default=None)


async def progress(message: str) -> None:
    """Publish useful activity without forwarding source, tool payloads or reasoning."""
    sink = SINK.get()
    if sink:
        await sink({"type": "CUSTOM", "name": "studio.activity", "value": {"message": message}})


def install_routes(app, workspace, completion, mcp) -> None:
    """Accept AG-UI run envelopes behind the existing Studio authorization middleware."""
    @app.post("/api/agui")
    async def run(request: Request):
        """Validate the full authoring request before opening its event stream."""
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(422, "Expected an AG-UI run object.")
        envelope = AGUIInput.parse(payload)
        options = payload.get("forwardedProps", {})
        if not isinstance(options, dict):
            raise HTTPException(422, "forwardedProps must contain Studio authoring options.")
        try:
            body = Prompt.model_validate({**options, "prompt": envelope.text() or ""})
        except ValidationError:
            raise HTTPException(422, "Invalid Studio authoring options or empty user message.") from None
        workspace.project(body.project)
        browser = mcp_routes.session_id(request) or uuid.uuid4().hex
        response = StreamingResponse(events(workspace, body, completion, mcp, browser, envelope), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        # Set the review identity before SSE headers commit; later MCP plans use it.
        if not mcp_routes.session_id(request):
            response.set_cookie(mcp_routes.cookie_name(request), browser, httponly=True, samesite="lax", path="/", max_age=86400)
        return response


async def events(workspace, body, completion, mcp, browser, envelope):
    """Bound queued output and cancel owned work when the browser disconnects."""
    queue = asyncio.Queue(maxsize=32)
    identity = {"threadId": envelope.thread_id or uuid.uuid4().hex, "runId": envelope.run_id}
    task = asyncio.create_task(produce(queue, identity, workspace, body, completion, mcp, browser))
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), 10)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield "data: " + json.dumps(event) + "\n\n"
            if event["type"] in {"RUN_FINISHED", "RUN_ERROR"}:
                break
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def produce(queue, identity, workspace, body, completion, mcp, browser):
    """Expose only validated proposals and sanitized errors as terminal run events."""
    reset = SINK.set(queue.put)
    try:
        await queue.put({"type": "RUN_STARTED", **identity})
        await progress("Reading selected project source")
        with mcp_routes.translated():
            result = await propose(workspace, body, completion, mcp=mcp, session=browser)
        await emit_reply(queue, result)
        await queue.put({"type": "RUN_FINISHED", **identity})
    except HTTPException as error:
        await queue.put({"type": "RUN_ERROR", "message": str(error.detail)})
    except Exception:
        await queue.put({"type": "RUN_ERROR", "message": "The builder could not complete this request. Please retry."})
    finally:
        SINK.reset(reset)


async def emit_reply(queue, result):
    """Offer standard AG-UI text plus the validated Studio review extension."""
    identity = uuid.uuid4().hex
    await queue.put({"type": "TEXT_MESSAGE_START", "messageId": identity, "role": "assistant"})
    await queue.put({"type": "TEXT_MESSAGE_CONTENT", "messageId": identity, "delta": result["summary"]})
    await queue.put({"type": "TEXT_MESSAGE_END", "messageId": identity})
    await queue.put({"type": "CUSTOM", "name": "studio.proposal", "value": result})


class NativeEvents:
    """Accumulate final text and declared tool receipts, excluding earlier narration."""

    def __init__(self):
        """Keep per-invocation state isolated from other authoring rounds."""
        self.tools = {}
        self.output = []
        self.text = ""
        self.finished = False
        self.size = 0
        self.reported = False

    async def accept(self, event):
        """Reduce native AG-UI events while publishing payload-free tool progress."""
        kind = event["type"]
        if kind == "RUN_ERROR":
            raise ValueError("Native builder run failed")
        if kind == "RUN_FINISHED":
            self.finished = True
        elif kind == "TOOL_CALL_START":
            self.tools[event["toolCallId"]] = event["toolCallName"]
            await progress("Using " + event["toolCallName"])
        elif kind == "TOOL_CALL_RESULT":
            name = self.tools.pop(event["toolCallId"], "")
            output = json.loads(event["content"])
            self.output.append({"type": "tool_result", "name": name, "output": output})
            self.text = ""
            self.reported = False
            await progress(tool_status(name, output))
        elif kind == "TEXT_MESSAGE_CONTENT":
            self.text += event["delta"]
            if not self.reported:
                await progress("Writing the proposal")
                self.reported = True

    async def read(self, response):
        """Consume native single-line SSE frames with a total output budget."""
        async for line in response.aiter_lines():
            self.size += len(line.encode())
            if self.size > 2_000_000:
                raise ValueError("Builder output exceeded its budget")
            if line.startswith("data: "):
                await self.accept(json.loads(line[6:]))
        if not self.finished:
            raise ValueError("Builder stream ended before completion")
        return {"output": self.output, "outputText": self.text}


def tool_status(name: str, output) -> str:
    """Allow only the explicitly authored progress tool to expose a bounded message."""
    if name == "report_progress" and isinstance(output, dict) and isinstance(output.get("message"), str):
        return output["message"][:280]
    return "Completed " + name

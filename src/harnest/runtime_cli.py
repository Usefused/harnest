"""Terminal rendering for in-process Harnest agent invocation."""

from __future__ import annotations

import json
from typing import Any, TextIO

from .context_agent import AgentPendingResponse, AgentResponse, LocalAgentRuntime


async def run_local_cli(
    runtime: LocalAgentRuntime,
    message: str,
    *,
    session_id: str | None,
    output: str,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Invoke one local turn while keeping result and diagnostics separated."""

    if output not in {"text", "json", "ndjson"}:
        raise ValueError("local agent output must be text, json, or ndjson")
    session = (
        await runtime.create_session()
        if session_id is None
        else await runtime.open_session(session_id)
    )
    if output == "json":
        response = await session.invoke(message)
        _write_json(stdout, response.as_dict())
        return
    if output == "ndjson":
        async for item in session.stream(message):
            _write_json(stdout, item.as_dict())
        return
    await _stream_text(session, message, stdout=stdout, stderr=stderr)


async def _stream_text(
    session: Any,
    message: str,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Stream assistant text and reserve stderr for payload-free tool progress."""

    wrote_text = False
    ended_with_newline = False
    async for item in session.stream(message):
        if item.kind == "event":
            streamed = _write_event(item.event, stdout=stdout, stderr=stderr)
            if streamed is not None:
                wrote_text = wrote_text or bool(streamed)
                ended_with_newline = streamed.endswith("\n")
            continue
        if item.kind == "pending":
            _write_pending(
                item.pending,
                stdout=stdout,
                needs_newline=wrote_text and not ended_with_newline,
            )
            return
        wrote_text, ended_with_newline = _write_terminal_text(
            item.response,
            stdout=stdout,
            wrote_text=wrote_text,
            ended_with_newline=ended_with_newline,
        )
    if wrote_text and not ended_with_newline:
        stdout.write("\n")
        stdout.flush()


def _write_event(event: Any, *, stdout: TextIO, stderr: TextIO) -> str | None:
    """Render one safe event and return only text already written to stdout."""

    text = _event_text(event)
    if text is not None:
        stdout.write(text)
        stdout.flush()
    diagnostic = _tool_diagnostic(event)
    if diagnostic is not None:
        stderr.write(diagnostic + "\n")
        stderr.flush()
    return text


def _write_pending(
    value: AgentPendingResponse, *, stdout: TextIO, needs_newline: bool
) -> None:
    """Separate a durable handle from preceding streamed text."""

    if needs_newline:
        stdout.write("\n")
    _write_json(stdout, value.as_dict())


def _write_terminal_text(
    response: AgentResponse,
    *,
    stdout: TextIO,
    wrote_text: bool,
    ended_with_newline: bool,
) -> tuple[bool, bool]:
    """Render only output absent from streamed message events."""

    if wrote_text:
        return wrote_text, ended_with_newline
    if response.output_text:
        stdout.write(response.output_text)
        stdout.flush()
        return True, response.output_text.endswith("\n")
    _write_json(stdout, response.result)
    return True, True


def _event_text(event: Any) -> str | None:
    """Select only framework-neutral assistant message deltas for stdout."""

    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    value = event.get("text")
    return value if isinstance(value, str) else None


def _tool_diagnostic(event: Any) -> str | None:
    """Describe tool chronology without printing arguments, results, or call IDs."""

    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type not in {"tool_call", "tool_result"}:
        return None
    name = event.get("name")
    safe_name = name if isinstance(name, str) and name else "tool"
    state = "running" if event_type == "tool_call" else "completed"
    return f"[tool] {safe_name} {state}"


def _write_json(stream: TextIO, value: Any) -> None:
    """Write one machine-readable record without implicit object conversion."""

    stream.write(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n")
    stream.flush()


__all__ = ["run_local_cli"]

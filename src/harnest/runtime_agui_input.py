"""Validate AG-UI input without granting authority from client envelope fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import uuid

from fastapi import HTTPException

from .runtime_contract import SessionConflictError
from .runtime_invocation import InvocationCoordinator


@dataclass(frozen=True)
class AGUIInput:
    """Separate client presentation data from authenticated execution identity."""

    thread_id: str | None
    run_id: str
    messages: list[dict[str, Any]]
    state: dict[str, Any]
    metadata: dict[str, Any]
    resume: list[dict[str, Any]]

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> AGUIInput:
        """Validate all supported envelope fields before creating a session."""

        thread_id = _identity(payload, "threadId")
        run_id = _identity(payload, "runId") or uuid.uuid4().hex
        messages = _messages(payload)
        state = _object(payload, "state")
        if any(key.startswith(("_", "app:", "user:", "temp:")) for key in state):
            raise HTTPException(400, "state contains a reserved key")
        context = _objects(payload, "context")
        tools = _objects(payload, "tools")
        _validate_descriptors(context, tools)
        metadata = {"agui": {
            "context": context, "tools": tools,
            "forwardedProps": payload.get("forwardedProps"),
        }}
        return cls(thread_id, run_id, messages, state, metadata, _objects(payload, "resume"))

    def text(self) -> str | None:
        """Accept an empty initialization run or exactly the newest user turn."""

        if not self.messages:
            return None
        message = self.messages[-1]
        if message.get("role") != "user":
            raise HTTPException(400, "A new run must end with a user message")
        content = message.get("content")
        if isinstance(content, list):
            content = _text_parts(content)
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, "messages must include non-empty user content")
        return content


def _text_parts(parts: list[Any]) -> str:
    """Reject unsupported media rather than silently losing user input."""

    text = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            raise HTTPException(400, "AG-UI currently accepts text content only")
        if not isinstance(part.get("text"), str):
            raise HTTPException(400, "text content must be a string")
        text.append(part["text"])
    return "".join(text)


def _messages(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Give legacy text-only requests valid snapshot IDs without trusting history."""

    messages = []
    for message in _objects(payload, "messages"):
        identity = _identity(message, "id") or uuid.uuid4().hex
        if message.get("role") not in {"user", "assistant", "system", "developer", "tool", "activity", "reasoning"}:
            raise HTTPException(400, "messages contain an unsupported role")
        messages.append({**message, "id": identity})
    return messages


def _identity(payload: Mapping[str, Any], name: str) -> str | None:
    """Treat omitted identities differently from malformed explicit values."""

    value = payload.get(name)
    if name in payload and (not isinstance(value, str) or not value.strip()):
        raise HTTPException(400, f"{name} must be a non-empty string")
    return value


def _object(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Require an object even when the field is optional."""

    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise HTTPException(400, f"{name} must be an object")
    return value


def _objects(payload: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    """Validate envelope arrays before any entry is used for dispatch."""

    value = payload.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HTTPException(400, f"{name} must be an array of objects")
    return value


def _validate_descriptors(context: list[dict], tools: list[dict]) -> None:
    """Keep client context and tool advertisements structurally interoperable."""

    for item in context:
        if not all(isinstance(item.get(key), str) for key in ("description", "value")):
            raise HTTPException(400, "context entries require description and value strings")
    for item in tools:
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise HTTPException(400, "tools require a non-empty name")
        if not isinstance(item.get("parameters"), dict):
            raise HTTPException(400, "tools require a parameters object")


async def resolve_agui_session(
    coordinator: InvocationCoordinator, *, user_id: str, thread_id: str | None
) -> Any:
    """Create a client-selected thread inside the driver's principal namespace."""

    if thread_id is None:
        return await coordinator.resolve_session(user_id=user_id, session_id=None)
    session = await coordinator.driver.get_session(user_id=user_id, session_id=thread_id)
    if session is not None:
        return session
    try:
        return await coordinator.driver.create_session(
            user_id=user_id, session_id=thread_id, state={}
        )
    except SessionConflictError:
        # Another first request may have created the same owned thread. Never
        # recover a collision by looking outside this principal's namespace.
        return await coordinator.resolve_session(user_id=user_id, session_id=thread_id)

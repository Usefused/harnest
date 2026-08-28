"""Privacy-safe session payloads and opaque pagination cursors."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping, Sequence

from pydantic import BaseModel

from .runtime_contract import SessionMessage, SessionRecord


_BINARY_FIELD_NAMES = frozenset(
    {
        "blob",
        "bytes",
        "data",
        "data_url",
        "dataUrl",
        "file_data",
        "fileData",
        "inline_data",
        "inlineData",
        "provider_id",
        "providerId",
        "uri",
        "url",
    }
)
_NATIVE_CONTENT_FIELD_NAMES = _BINARY_FIELD_NAMES | {
    "asset_id",
    "assetId",
    "content",
    "messages",
    "parts",
}


def session_payload(session: SessionRecord) -> dict[str, Any]:
    """Render the complete neutral record without flattening native metadata."""

    return {
        "id": session.id,
        "userId": session.user_id,
        "state": dict(session.state),
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "metadata": dict(session.metadata),
    }


def session_message_payload(message: SessionMessage) -> dict[str, Any]:
    """Render portable content while excluding native binary-bearing fields."""

    return {
        "id": message.id,
        "role": message.role,
        "content": _public_message_value(message.content, metadata=False),
        "createdAt": message.created_at,
        "metadata": _public_message_value(dict(message.metadata), metadata=True),
    }


def _public_message_value(value: Any, *, metadata: bool) -> Any:
    """Recursively remove raw media representations as a transport backstop."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if isinstance(value, Mapping):
        blocked = _NATIVE_CONTENT_FIELD_NAMES if metadata else _BINARY_FIELD_NAMES
        return {
            key: _public_message_value(item, metadata=metadata)
            for key, item in value.items()
            if key not in blocked
        }
    if isinstance(value, (list, tuple)):
        return [_public_message_value(item, metadata=metadata) for item in value]
    return value


def _message_prefix_digest(
    messages: Sequence[SessionMessage], boundary: int
) -> str:
    """Fingerprint ordered message identities through one page boundary."""

    digest = hashlib.sha256()
    for message in messages[: boundary + 1]:
        identity = message.id.encode("utf-8")
        # Length-prefixing prevents distinct ID sequences from hashing as the
        # same concatenated bytes without constraining framework-owned IDs.
        digest.update(len(identity).to_bytes(8, "big"))
        digest.update(identity)
    return digest.hexdigest()


def _encode_message_cursor(
    *,
    messages: Sequence[SessionMessage],
    boundary: int,
    session_id: str,
    user_id: str,
) -> str:
    """Encode a resource-bound page boundary as an opaque API cursor."""

    payload = json.dumps(
        {
            "version": 1,
            "offset": boundary,
            "messageId": messages[boundary].id,
            "prefixDigest": _message_prefix_digest(messages, boundary),
            "sessionId": session_id,
            "userId": user_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor_payload(cursor: str) -> Mapping[str, Any]:
    """Decode one opaque URL-safe cursor into an untrusted mapping."""

    try:
        padding = "=" * (-len(cursor) % 4)
        payload = base64.b64decode(
            cursor + padding, altchars=b"-_", validate=True
        )
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid cursor") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid cursor")
    return value


def _valid_message_cursor_payload(value: Any) -> bool:
    """Validate cursor fields without trusting client-decoded JSON types."""

    return (
        isinstance(value, dict)
        and value.get("version") == 1
        and type(value.get("offset")) is int
        and value["offset"] >= 0
        and isinstance(value.get("messageId"), str)
        and bool(value["messageId"])
        and isinstance(value.get("prefixDigest"), str)
        and len(value["prefixDigest"]) == 64
        and isinstance(value.get("sessionId"), str)
        and isinstance(value.get("userId"), str)
    )


def _decode_message_cursor(cursor: str) -> tuple[int, str, str, str, str]:
    """Decode and structurally validate one opaque transcript cursor."""

    value = _decode_cursor_payload(cursor)
    if not _valid_message_cursor_payload(value):
        raise ValueError("invalid transcript cursor")
    return (
        value["offset"],
        value["messageId"],
        value["prefixDigest"],
        value["sessionId"],
        value["userId"],
    )


def paginate_session_messages(
    messages: Sequence[SessionMessage],
    *,
    limit: int,
    cursor: str | None,
    session_id: str,
    user_id: str,
) -> tuple[Sequence[SessionMessage], str | None]:
    """Return one ordered page and a cursor tied to its exact boundary."""

    start = 0
    if cursor is not None:
        offset, message_id, prefix_digest, cursor_session, cursor_user = (
            _decode_message_cursor(cursor)
        )
        valid_boundary = offset < len(messages) and messages[offset].id == message_id
        valid_resource = cursor_session == session_id and cursor_user == user_id
        valid_prefix = (
            valid_boundary
            and _message_prefix_digest(messages, offset) == prefix_digest
        )
        if not valid_resource or not valid_prefix:
            raise ValueError("invalid transcript cursor")
        start = offset + 1
    page = messages[start : start + limit]
    if not page or start + len(page) >= len(messages):
        return page, None
    boundary = start + len(page) - 1
    return page, _encode_message_cursor(
        messages=messages,
        boundary=boundary,
        session_id=session_id,
        user_id=user_id,
    )


def encode_session_cursor(*, after: str, user_id: str) -> str:
    """Encode one user-bound session-list keyset boundary."""

    payload = json.dumps(
        {
            "version": 1,
            "kind": "sessions",
            "afterSessionId": after,
            "userId": user_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_session_cursor(cursor: str, *, user_id: str) -> str:
    """Validate and return a session-list cursor in the caller's scope."""

    value = _decode_cursor_payload(cursor)
    valid = (
        value.get("version") == 1
        and value.get("kind") == "sessions"
        and isinstance(value.get("afterSessionId"), str)
        and bool(value["afterSessionId"])
        and value.get("userId") == user_id
    )
    if not valid:
        raise ValueError("invalid session cursor")
    return value["afterSessionId"]


__all__ = [
    "decode_session_cursor",
    "encode_session_cursor",
    "paginate_session_messages",
    "session_message_payload",
    "session_payload",
]

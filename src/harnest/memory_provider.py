"""Shared validation and privacy boundaries for bundled memory providers."""

from dataclasses import asdict, replace
from contextvars import ContextVar
import json
import math
import time
from typing import Any
import uuid

from .logging import get_logger
from ._json import json_value
from .memory import MemoryCleanupPage, MemoryConflictError, MemoryPage, MemoryRecord, MemoryScope, MemoryStorageError

_AUDIT = get_logger("memory.audit")
_CALL_DEPTH: ContextVar[int] = ContextVar("harnest_memory_call_depth", default=0)


def text_field(value: Any, name: str, maximum: int) -> str:
    """Bound UTF-8 fields without reflecting private values in failures."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text without NUL")
    if _utf8_size(value, name) > maximum:
        raise ValueError(f"{name} exceeds its byte limit")
    return value


def _utf8_size(value: str, name: str) -> int:
    """Reject malformed Unicode without embedding private text in the error."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{name} must contain valid UTF-8") from None


def validate_scope(scope: MemoryScope) -> None:
    """Reject incomplete identity before touching a database."""
    if not isinstance(scope, MemoryScope):
        raise TypeError("scope must be MemoryScope")
    for name in ("application_id", "user_id", "namespace"):
        text_field(getattr(scope, name), name, 512)


def revision_check(expected: str | None, current: MemoryRecord | None) -> None:
    """Fence conditional updates, including deletion/recreation races."""
    if expected is not None and (current is None or current.revision != expected):
        raise MemoryConflictError("memory revision does not match")


def encode_record(record: MemoryRecord) -> str:
    """Serialize detached records using one portable, strict JSON format."""
    return json.dumps(asdict(record), ensure_ascii=False, allow_nan=False)


def decode_record(raw: str) -> MemoryRecord:
    """Reconstruct records without sharing mutable provider-owned dictionaries."""
    value = json.loads(raw)
    value["scope"] = MemoryScope(**value["scope"])
    return MemoryRecord(**value)


def prepare_record(record: MemoryRecord) -> MemoryRecord:
    """Validate bounded payloads and assign provider-owned write metadata."""
    if not isinstance(record, MemoryRecord):
        raise TypeError("record must be MemoryRecord")
    validate_scope(record.scope)
    text_field(record.key, "key", 256)
    text_field(record.content, "content", 65536)
    record = _validate_metadata(record)
    now = time.time()
    expiry = record.expires_at
    if expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= now):
        raise ValueError("expires_at must be a finite future UTC timestamp")
    return decode_record(encode_record(replace(record, revision=uuid.uuid4().hex, created_at=now, updated_at=now)))


def _validate_metadata(record: MemoryRecord) -> MemoryRecord:
    """Keep metadata and provenance bounded and JSON-safe across providers."""
    from collections.abc import Mapping
    if not isinstance(record.metadata, Mapping) or not isinstance(record.source, Mapping):
        raise TypeError("metadata and source must be mappings")
    try:
        raw = json.dumps(json_value([record.metadata, record.source]), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("metadata and source must be JSON-safe") from None
    if len(raw.encode()) > 16384:
        raise ValueError("metadata and source exceed their byte limit")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in record.source.items()):
        raise ValueError("source must contain string keys and values")
    metadata, source = json.loads(raw)
    return replace(record, metadata=metadata, source=source)


def validate_page(scope: MemoryScope, limit: int, after: str | None) -> None:
    """Apply common scope, pagination and cursor bounds before provider queries."""
    validate_scope(scope)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if after is not None:
        text_field(after, "after", 256)


async def protected(operation: str, callback: Any, *args: Any) -> Any:
    """Sanitize provider errors and audit mutations without payloads or identities."""
    depth = _CALL_DEPTH.get()
    token = _CALL_DEPTH.set(depth + 1)
    try:
        result = await callback(*args)
    except MemoryConflictError:
        if not depth:
            _audit(operation, "conflict")
        raise MemoryConflictError("memory revision does not match") from None
    except Exception:
        if not depth:
            _audit(operation, "failed")
        raise MemoryStorageError(f"memory {operation} failed") from None
    finally:
        _CALL_DEPTH.reset(token)
    if not depth:
        _audit(operation, "committed")
    return result


def _audit(operation: str, outcome: str) -> None:
    """Audit durable writes, not memory contents, search queries or record keys."""
    if operation in {"put", "delete", "delete_all", "delete_user", "purge_expired"}:
        _AUDIT.info(f"memory.{operation}", operation=operation, trigger="user", outcome=outcome)


class MemoryProvider:
    """Implement validation and privacy around datastore-specific operations."""

    async def delete_all(self, scope: MemoryScope) -> int:
        """Erase one explicit namespace through trusted application code only."""
        validate_scope(scope)
        return await protected("delete_all", self._delete_all, scope)

    async def delete_user(self, application_id: str, user_id: str) -> int:
        """Erase an explicitly authorized owner without registering any endpoint."""
        scope = MemoryScope(application_id, user_id)
        validate_scope(scope)
        return await protected("delete_user", self._delete_user, scope)

    async def purge_expired(self, scope: MemoryScope, *, limit: int = 100, after: str | None = None) -> MemoryCleanupPage:
        """Sweep a bounded key page without exposing another user's collection."""
        validate_page(scope, limit, after)
        return await protected("purge_expired", self._purge_expired, scope, limit, after)

    async def put(self, record: MemoryRecord, *, expected_revision: str | None = None) -> MemoryRecord:
        """Validate before atomically persisting a deliberate memory write."""
        value = prepare_record(record)
        if expected_revision is not None:
            text_field(expected_revision, "expected_revision", 128)
        return await protected("put", self._put, value, expected_revision)

    async def get(self, scope: MemoryScope, key: str) -> MemoryRecord | None:
        """Read only an explicitly scoped live record."""
        validate_page(scope, 1, None)
        text_field(key, "key", 256)
        page = await protected("get", self._read, scope, key, None, 1, None)
        return page.items[0] if page.items else None

    async def list(self, scope: MemoryScope, *, limit: int = 20, after: str | None = None) -> MemoryPage:
        """Page records without loading an unbounded provider collection."""
        validate_page(scope, limit, after)
        return await protected("list", self._read, scope, None, None, limit, after)

    async def search(self, scope: MemoryScope, query: str, *, limit: int = 20, after: str | None = None) -> MemoryPage:
        """Retrieve explicit literal text matches without model or embedding calls."""
        validate_page(scope, limit, after)
        text_field(query, "query", 1024)
        return await protected("search", self._read, scope, None, query, limit, after)

    async def delete(self, scope: MemoryScope, key: str, *, expected_revision: str | None = None) -> bool:
        """Remove memory and its indexes with an optional atomic revision fence."""
        validate_scope(scope)
        text_field(key, "key", 256)
        if expected_revision is not None:
            text_field(expected_revision, "expected_revision", 128)
        return await protected("delete", self._delete, scope, key, expected_revision)

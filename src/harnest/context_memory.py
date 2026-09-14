"""Revocable memory handles bound to trusted invocation ownership."""

from typing import Any, Mapping
from functools import partial

from . import context
from .memory import MemoryPage, MemoryRecord, MemoryScope, MemoryStorageError
from .memory_provider import protected, text_field, validate_scope, validate_page, prepare_record


class MemoryContext:
    """Access explicit memories across sessions within one application/user scope."""

    def __init__(self, active: Any, namespace: str = "default") -> None:
        """Capture revocable authority, never caller-supplied tenant overrides."""
        self._active = active
        self._scope = MemoryScope(active._memory_application_id, active.user_id, namespace)
        validate_scope(self._scope)

    def namespace(self, name: str) -> "MemoryContext":
        """Select a logical collection without changing application/user ownership."""
        self._store()
        text_field(name, "namespace", 512)
        return MemoryContext(self._active, name)

    def _store(self) -> Any:
        """Reject retained or transferred handles outside their originating scope."""
        self._active._require_active()
        current = context.current()
        if current._lifetime is not self._active._lifetime:
            raise context.ContextUnavailableError("memory handle belongs to another invocation")
        store = self._active._memory_store
        if store is None:
            raise context.ContextResourceError("configure @lifecycle.storage.memory to use long-term memory")
        return store

    async def put(self, key: str, content: str, *, metadata: Mapping[str, Any] | None = None, expires_at: float | None = None, expected_revision: str | None = None) -> MemoryRecord:
        """Deliberately save or replace a memory, recording invocation provenance."""
        self._store()
        active = self._active
        record = MemoryRecord(self._scope, key, content, {} if metadata is None else metadata,
                              {"session_id": active.session_id, "invocation_id": active.invocation_id, "agent_name": active.agent_name},
                              expires_at=expires_at)
        return await self._invoke("put", prepare_record(record), expected_revision=expected_revision)

    async def get(self, key: str) -> MemoryRecord | None:
        """Read a scoped key without adding it to model context."""
        text_field(key, "key", 256)
        return await self._invoke("get", self._scope, key)

    async def list(self, *, limit: int = 20, after: str | None = None) -> MemoryPage:
        """List a bounded page of explicit memories across this user's sessions."""
        validate_page(self._scope, limit, after)
        return await self._invoke("list", self._scope, limit=limit, after=after)

    async def search(self, query: str, *, limit: int = 20, after: str | None = None) -> MemoryPage:
        """Search literal text on explicit request, without model calls."""
        validate_page(self._scope, limit, after)
        text_field(query, "query", 1024)
        return await self._invoke("search", self._scope, query, limit=limit, after=after)

    async def delete(self, key: str, *, expected_revision: str | None = None) -> bool:
        """Explicitly forget a memory and its provider index references."""
        text_field(key, "key", 256)
        return await self._invoke("delete", self._scope, key, expected_revision=expected_revision)

    async def _invoke(self, operation: str, *args: Any, **options: Any) -> Any:
        """Recheck lifetime after I/O and reject foreign custom-provider records."""
        expected = options.get("expected_revision")
        if expected is not None:
            text_field(expected, "expected_revision", 128)
        callback = partial(getattr(self._store(), operation), *args, **options)
        result = await protected(operation, callback)
        self._store()
        _validate_result(result, self._scope, operation, options.get("limit", 20))
        return result


def _validate_result(value: Any, scope: MemoryScope, operation: str, limit: int) -> None:
    """Do not publish data from a provider that returned the wrong ownership."""
    if operation == "delete" and isinstance(value, bool):
        return
    if operation == "get" and value is None:
        return
    records = _result_records(value, operation, limit)
    if any(not isinstance(record, MemoryRecord) or record.scope != scope for record in records):
        raise MemoryStorageError("memory provider returned an invalid scope")


def _result_records(value: Any, operation: str, limit: int) -> tuple[MemoryRecord, ...]:
    """Validate provider result shapes independently of ownership checks."""
    if operation in {"get", "put"} and isinstance(value, MemoryRecord):
        return (value,)
    if operation in {"search", "list"} and isinstance(value, MemoryPage) and len(value.items) <= limit:
        return value.items
    raise MemoryStorageError("memory provider returned an invalid result")

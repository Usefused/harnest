"""Bounded, owner-scoped response receipts shared by neutral transports."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
import time
from typing import Any, Callable, Mapping


_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "denied", "failed"})


class ResponseStatusCapacityError(RuntimeError):
    """Raised before execution when no response receipt can be retained."""


@dataclass(slots=True)
class _ResponseReceipt:
    """One transport-neutral response snapshot and its complete owner scope."""

    user_id: str
    session_id: str
    payload: dict[str, Any]
    updated_at: float

    @property
    def terminal(self) -> bool:
        return self.payload.get("status") in _TERMINAL_STATUSES


class InMemoryResponseStatusStore:
    """Retain bounded response receipts for retry and same-process recovery."""

    def __init__(
        self,
        *,
        max_responses: int = 1024,
        tombstone_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure bounded retention without storing request or credential data."""

        if max_responses < 1:
            raise ValueError("response max_responses must be at least one")
        if tombstone_seconds < 0:
            raise ValueError("response tombstone_seconds cannot be negative")
        self._max_responses = max_responses
        self._tombstone_seconds = tombstone_seconds
        self._clock = clock
        self._items: OrderedDict[str, _ResponseReceipt] = OrderedDict()
        self._lock = Lock()

    def begin(
        self,
        *,
        response_id: str,
        user_id: str,
        session_id: str,
        metadata: Mapping[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Reserve polling ownership before a response can expose its identifier."""

        payload: dict[str, Any] = {
            "id": response_id,
            "sessionId": session_id,
            "status": "in_progress",
            "outputText": "",
            "output": [],
            "metadata": dict(metadata),
        }
        if request_id is not None:
            payload["requestId"] = request_id
        return self.record(
            response_id=response_id,
            user_id=user_id,
            session_id=session_id,
            payload=payload,
        )

    def record(
        self,
        *,
        response_id: str,
        user_id: str,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Publish one detached snapshot without weakening its original ownership."""

        normalized = _status_payload(payload, response_id, session_id)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            current = self._items.get(response_id)
            if current is not None and (
                current.user_id != user_id or current.session_id != session_id
            ):
                raise KeyError("response not found")
            if current is None:
                self._make_room_locked()
                current = _ResponseReceipt(user_id, session_id, normalized, now)
                self._items[response_id] = current
            else:
                current.payload = normalized
                current.updated_at = now
            self._items.move_to_end(response_id)
        return deepcopy(normalized)

    def get(
        self, *, response_id: str, user_id: str, session_id: str
    ) -> dict[str, Any]:
        """Read one exact owner-scoped snapshot without revealing mismatches."""

        with self._lock:
            self._prune_locked(self._clock())
            current = self._items.get(response_id)
            if current is None or (
                current.user_id != user_id or current.session_id != session_id
            ):
                raise KeyError("response not found")
            self._items.move_to_end(response_id)
            return deepcopy(current.payload)

    def _make_room_locked(self) -> None:
        """Evict an oldest terminal receipt but never hide active work."""

        if len(self._items) < self._max_responses:
            return
        for response_id, receipt in self._items.items():
            if receipt.terminal:
                self._items.pop(response_id)
                return
        raise ResponseStatusCapacityError("response status capacity is exhausted")

    def _prune_locked(self, now: float) -> None:
        """Expire only terminal receipts so unresolved actions remain recoverable."""

        expired = tuple(
            response_id
            for response_id, receipt in self._items.items()
            if receipt.terminal
            and now - receipt.updated_at >= self._tombstone_seconds
        )
        for response_id in expired:
            self._items.pop(response_id, None)


def _status_payload(
    payload: Mapping[str, Any], response_id: str, session_id: str
) -> dict[str, Any]:
    """Normalize JSON, SSE, and WebSocket payloads onto the polling envelope."""

    normalized = deepcopy(dict(payload))
    normalized.pop("type", None)
    normalized.pop("sequence", None)
    normalized.pop("responseId", None)
    normalized["id"] = response_id
    normalized["sessionId"] = session_id
    if not isinstance(normalized.get("status"), str):
        raise ValueError("response status payload requires status")
    return normalized


__all__ = [
    "InMemoryResponseStatusStore",
    "ResponseStatusCapacityError",
]

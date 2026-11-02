"""Production eval capture contracts: what a capture rule sees and what a store keeps.

A capture rule registered with ``@lifecycle.evals.capture`` in
``lifecycle/evals_improvement.py`` receives a :class:`CaptureEvent` after each
finished invocation. Accepted invocations become :class:`CaptureRecord` values.
Without an authored redactor, only structure is retained. Harnest does not
detect personal data; teams own any policy that retains content.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable

CAPTURE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ToolActivity:
    """One tool call in a captured invocation, with its result when one arrived."""

    name: str
    arguments: Any = None
    result: Any = None
    error: bool = False

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-safe mapping for stores."""

        return {
            "name": self.name,
            "arguments": self.arguments,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "ToolActivity":
        """Rebuild one tool activity written by :meth:`to_json`."""

        return cls(
            name=str(value.get("name", "")),
            arguments=value.get("arguments"),
            result=value.get("result"),
            error=bool(value.get("error", False)),
        )


@dataclass(frozen=True, slots=True)
class CaptureEvent:
    """Facts about one finished invocation, offered to the capture rule.

    The event exists only in memory. ``user_id``, ``session_id`` and ``metadata``
    let a rule target specific traffic, but none is copied into the stored record.
    """

    agent_name: str
    invocation_id: str
    session_id: str
    user_id: str
    transport: str | None
    status: str
    input_text: str
    output_text: str
    tool_calls: tuple[ToolActivity, ...] = ()
    error_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """Return whether the invocation raised instead of completing."""

        return self.status == "failed"

    @property
    def tool_error(self) -> bool:
        """Return whether any tool result looked like an error.

        This is a heuristic over framework-neutral events (an ``error`` field or
        an error status); write an explicit check in the rule when it matters.
        """

        return any(item.error for item in self.tool_calls)


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    """One invocation with structure-only or team-redacted content.

    ``content="omitted"`` serializes empty input/output text and null tool
    arguments/results. ``content="redacted"`` records that the team's redactor
    ran, not a guarantee that all personal data was removed.
    """

    id: str
    captured_at: str
    agent_name: str
    invocation_id: str
    reasons: tuple[str, ...]
    status: str
    input_text: str
    output_text: str
    tool_calls: tuple[ToolActivity, ...] = ()
    error_type: str | None = None
    content: Literal["omitted", "redacted"] = "omitted"
    schema_version: int = CAPTURE_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        """Serialize schema version 1 without content unless explicitly redacted."""

        # A record passed directly to a store must honor the same omission
        # contract as runtime captures, even if its in-memory fields contain text.
        record = self if self.content == "redacted" else default_redactor(self)
        return {
            "schemaVersion": self.schema_version,
            "id": self.id,
            "capturedAt": self.captured_at,
            "agentName": self.agent_name,
            "invocationId": self.invocation_id,
            "reasons": list(self.reasons),
            "status": self.status,
            "errorType": self.error_type,
            "inputText": record.input_text,
            "outputText": record.output_text,
            "toolCalls": [item.to_json() for item in record.tool_calls],
            "content": record.content,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "CaptureRecord":
        """Read a record, omitting content without an explicit content policy."""

        content = value.get("content", "omitted")
        # Unknown markers must not silently authorize retention of payloads.
        if content not in {"omitted", "redacted"}:
            raise ValueError("capture content must be omitted or redacted")
        record = cls(
            id=str(value["id"]),
            captured_at=str(value["capturedAt"]),
            agent_name=str(value["agentName"]),
            invocation_id=str(value["invocationId"]),
            reasons=tuple(str(item) for item in value.get("reasons", ())),
            status=str(value.get("status", "completed")),
            input_text=str(value.get("inputText", "")),
            output_text=str(value.get("outputText", "")),
            tool_calls=tuple(
                ToolActivity.from_json(item) for item in value.get("toolCalls", ())
            ),
            error_type=value.get("errorType"),
            content=content,
            schema_version=int(value.get("schemaVersion", CAPTURE_SCHEMA_VERSION)),
        )
        # Older experimental records used heuristic redaction. Their boolean
        # marker is not evidence that an authored team policy ran.
        return record if content == "redacted" else default_redactor(record)


@runtime_checkable
class CaptureStore(Protocol):
    """Storage for capture records, registered with ``@lifecycle.storage.custom``."""

    async def start(self) -> None:
        """Open the underlying storage resource."""

    async def close(self) -> None:
        """Release the underlying storage resource."""

    async def append(self, record: CaptureRecord) -> None:
        """Persist one structure-only or team-redacted record."""

    async def list(
        self, *, since: datetime | None = None, limit: int = 100
    ) -> Sequence[CaptureRecord]:
        """Return records captured at or after ``since``, newest first."""


def default_redactor(record: CaptureRecord) -> CaptureRecord:
    """Omit all input, output and tool payloads, preserving only structure.

    This existing helper name remains importable, but no longer guesses which
    substrings contain personal data. Register ``@lifecycle.evals.redact`` to
    retain content under a team-owned policy instead.
    """

    return replace(
        record,
        input_text="",
        output_text="",
        tool_calls=tuple(
            replace(item, arguments=None, result=None)
            for item in record.tool_calls
        ),
        content="omitted",
    )


def redact_text(value: str) -> str:
    """Omit an entire string; retained for import compatibility, not PII detection."""

    del value
    return ""


class LocalCaptureStore:
    """Reference store writing one JSON Lines file per UTC day.

    Suitable for local development and single-instance deployments with a
    persistent volume. Files older than ``retention_days`` are deleted on start
    and whenever a new day's file is opened.
    """

    def __init__(self, directory: str | Path, *, retention_days: int = 30) -> None:
        """Record configuration only; no I/O happens until :meth:`start`."""

        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise TypeError("retention_days must be an integer")
        if retention_days < 1:
            raise ValueError("retention_days must be at least one")
        self._directory = Path(directory)
        self._retention = timedelta(days=retention_days)
        self._lock = asyncio.Lock()
        self._pruned_for: str | None = None

    async def start(self) -> None:
        """Create the directory and apply retention."""

        await asyncio.to_thread(self._directory.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self._prune, _today())

    async def close(self) -> None:
        """Nothing to release; every append closes its file."""

    async def append(self, record: CaptureRecord) -> None:
        """Append one record to today's file, serialized across tasks."""

        line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await asyncio.to_thread(self._write, line)

    async def list(
        self, *, since: datetime | None = None, limit: int = 100
    ) -> Sequence[CaptureRecord]:
        """Read records newest first, stopping at ``limit``."""

        if limit < 1:
            raise ValueError("limit must be at least one")
        return await asyncio.to_thread(self._read, since, limit)

    def _write(self, line: str) -> None:
        """Write under the async lock; prune once per new day."""

        day = _today()
        if self._pruned_for != day:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._prune(day)
        with (self._directory / f"{day}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def _prune(self, day: str) -> None:
        """Delete day files older than the retention window."""

        cutoff = (datetime.now(timezone.utc) - self._retention).strftime("%Y-%m-%d")
        for path in self._day_files():
            if path.stem < cutoff:
                path.unlink(missing_ok=True)
        self._pruned_for = day

    def _read(self, since: datetime | None, limit: int) -> list[CaptureRecord]:
        """Scan day files newest first so large histories stay bounded."""

        floor = _utc(since).strftime("%Y-%m-%d") if since else None
        found: list[CaptureRecord] = []
        for path in reversed(self._day_files()):
            if floor is not None and path.stem < floor:
                break
            found.extend(_records_in(path, since))
            if len(found) >= limit:
                break
        found.sort(key=lambda item: item.captured_at, reverse=True)
        return found[:limit]

    def _day_files(self) -> list[Path]:
        """Return day files in ascending date order."""

        if not self._directory.is_dir():
            return []
        return sorted(
            path
            for path in self._directory.glob("*.jsonl")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem)
        )


def _records_in(path: Path, since: datetime | None) -> list[CaptureRecord]:
    """Decode one day file, skipping a torn final line from an interrupted write."""

    records: list[CaptureRecord] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = CaptureRecord.from_json(json.loads(line))
            except (ValueError, KeyError, TypeError):
                continue
            if since is None or record.captured_at >= _utc(since).isoformat():
                records.append(record)
    return records


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC so comparisons never depend on host zone."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "CaptureEvent",
    "CaptureRecord",
    "CaptureStore",
    "LocalCaptureStore",
    "ToolActivity",
    "default_redactor",
    "redact_text",
]

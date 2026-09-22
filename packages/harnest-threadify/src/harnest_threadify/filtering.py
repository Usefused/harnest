"""Allowlist business telemetry before it reaches any Threadify transport."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
import math

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Status
from opentelemetry.sdk.util.instrumentation import InstrumentationScope


_SAFE_ATTRIBUTES = frozenset({
    "threadify.thread_id", "threadify.ref.sessionId", "harnest.session.key",
    "harnest.decision.name", "harnest.decision.version",
    "harnest.decision.outcome", "harnest.decision.action",
    "harnest.decision.duration_seconds", "business.outcome", "business.tool",
})
_PRIVATE_PREFIXES = ("gen_ai.", "llm.", "http.", "db.", "exception.", "rpc.")


@dataclass(frozen=True)
class BusinessFilter:
    """Select authored steps and scalar attributes; discard all events and links."""

    spans: tuple[str, ...] = ("harnest.request", "harnest.decision.evaluate", "business.*")
    attributes: tuple[str, ...] = ()
    max_string_length: int = 256

    def __post_init__(self) -> None:
        """Reject ambiguous patterns and invalid truncation limits up front."""
        # Attribute wildcards would silently admit newly introduced payloads.
        if any(not key or any(c in key for c in "*?[") for key in self.attributes):
            raise ValueError("business attributes must be exact non-empty names")
        if self.max_string_length < 1:
            raise ValueError("max_string_length must be positive")

    def sanitize(self, span: ReadableSpan, *, service_name: str) -> ReadableSpan | None:
        """Build a separate span so filtering cannot affect another destination."""
        # Unlinked infrastructure spans cannot create incidental business threads.
        if not (span.attributes or {}).get("threadify.thread_id"):
            return None
        if not any(fnmatchcase(span.name, pattern) for pattern in self.spans):
            return None
        attributes = self._attributes(span.attributes or {})
        return ReadableSpan(
            name=span.name, context=span.context, parent=None,
            start_time=span.start_time, end_time=span.end_time,
            status=Status(span.status.status_code), attributes=attributes,
            events=(), links=(), resource=Resource({"service.name": service_name}),
            instrumentation_scope=InstrumentationScope("harnest.threadify"),
        )

    def _attributes(self, values):
        """Drop unknown/nested values and cap explicitly allowed business strings."""
        allowed = _SAFE_ATTRIBUTES.union(self.attributes)
        result = {}
        for key, value in values.items():
            # Framework payload namespaces stay private even in expanded profiles.
            if key not in allowed or key.startswith(_PRIVATE_PREFIXES):
                continue
            normalized = self._scalar(value)
            if normalized is not None:
                result[key] = normalized
        return result

    def _scalar(self, value):
        """Accept bounded primitives without serializing arbitrary objects."""
        if isinstance(value, str):
            return value[:self.max_string_length]
        if isinstance(value, (bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return None

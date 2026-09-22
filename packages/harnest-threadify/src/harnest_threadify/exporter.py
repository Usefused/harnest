"""Bridge synchronous OpenTelemetry batches to a lifecycle-owned async SDK."""

import asyncio
import logging

from harnest.context import ContextUnavailableError, optional_active_context
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_LOG = logging.getLogger("harnest.threadify")


class SessionProcessor(SpanProcessor):
    """Attach verified invocation correlation before work leaves its task."""

    def __init__(self, owner):
        """Borrow the optional integration without owning its connection."""
        self.owner = owner

    def on_start(self, span, parent_context=None):
        """Capture only the active session identity, never invocation payloads."""
        try:
            active = optional_active_context()
        except ContextUnavailableError:
            return
        # Spans outside an invocation remain unlinked and are filtered at export.
        if active is not None:
            self.owner.annotate(span, active.user_id, active.session_id)

    def on_end(self, span):
        """Leave batching and filtering to the destination exporter."""

    def shutdown(self):
        """The lifecycle resource owns shutdown, not this borrowed processor."""

    def force_flush(self, timeout_millis=30000):
        """No spans are queued by the correlation processor."""
        return True


class BusinessExporter(SpanExporter):
    """Apply one filtering policy to both OTLP/Protobuf and native SDK export."""

    def __init__(self, owner, delegate=None):
        """Keep transport construction free of network traffic."""
        self.owner = owner
        self.delegate = delegate

    def export(self, spans):
        """Acknowledge native batches only after the async SDK has accepted them."""
        clean = tuple(value for span in spans if (
            value := self.owner.filter.sanitize(span, service_name=self.owner.service_name)
        ) is not None)
        # An entirely filtered batch is a successful intentional no-op.
        if not clean:
            return SpanExportResult.SUCCESS
        if self.delegate is not None:
            return self.delegate.export(clean)
        loop = self.owner.loop
        if loop is None or loop.is_closed():
            return SpanExportResult.FAILURE
        # Synchronous flush on the SDK's own loop cannot wait for that loop.
        if _on_loop(loop):
            return SpanExportResult.FAILURE
        pending = asyncio.run_coroutine_threadsafe(self.owner.export_spans(clean), loop)
        try:
            pending.result(timeout=self.owner.timeout)
            return SpanExportResult.SUCCESS
        except Exception:
            pending.cancel()
            _LOG.warning("Threadify batch export failed")
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis=30000):
        """Forward HTTP flushing; native export already awaits each batch."""
        if self.delegate is not None:
            return self.delegate.force_flush(timeout_millis)
        return True

    def shutdown(self):
        """Release only the owned synchronous transport; SDK closure is async."""
        if self.delegate is not None:
            self.delegate.shutdown()


def _on_loop(loop):
    """Detect a same-loop synchronous flush without creating an event loop."""
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False

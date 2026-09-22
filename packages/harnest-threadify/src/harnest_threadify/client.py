"""Lifecycle-owned Threadify connections and session-oriented business telemetry."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator, Mapping, Sequence

if TYPE_CHECKING:
    from threadify import Connection, ThreadInstance
    from threadify import Threadify as NativeThreadify
    from harnest.lifecycle import LifecycleContext
    from harnest.http import HTTPCallRequest, HTTPLifecycleContext, HTTPResponseHead
    from harnest.runtime_contract import AgentInfo, InvocationRequest, SessionRecord
    from harnest.lifecycle_transition import Next
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Span

from harnest import context
from harnest.telemetry import TelemetryExporter, get_tracer
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .exporter import BusinessExporter, SessionProcessor
from .filtering import BusinessFilter

_LOG = logging.getLogger("harnest.threadify")
_REQUEST = ContextVar("harnest_threadify_request", default=None)


class Threadify:
    """Opt-in SDK/OTLP integration, configured without import-time credentials or I/O."""

    def __init__(
        self, *, service_name: str, api_key_env: str = "THREADIFY_API_KEY",
        filter: BusinessFilter | None = None, business_tools: tuple[str, ...] = (),
        otlp_endpoint: str | None = None, otlp_headers: Mapping[str, str] | None = None,
        connection_options: Mapping[str, Any] | None = None,
        timeout: float = 10.0, max_sessions: int = 10000,
    ) -> None:
        """Keep connection secrets deferred until the authored resource starts."""
        if not service_name or timeout <= 0 or max_sessions < 1:
            raise ValueError("service_name, timeout and max_sessions must be valid")
        self.service_name = service_name
        self.api_key_env = api_key_env
        self.filter = filter or BusinessFilter()
        self.business_tools = frozenset(business_tools)
        self.timeout = timeout
        self.max_sessions = max_sessions
        self._options = dict(connection_options or {})
        self._otlp_endpoint = otlp_endpoint
        self._otlp_headers = dict(otlp_headers or {})
        self.loop = None
        self._native = None
        self._bindings = {}
        self._uncertain = set()
        self._threads = {}
        self._lock = None
        self._provider = None
        self._exporter = None
        self._closed = False

    @property
    def native_class(self) -> type[NativeThreadify]:
        """Expose the actual SDK factory only when explicitly requested."""
        from threadify import Threadify as NativeThreadify
        return NativeThreadify

    @property
    def native(self) -> Connection:
        """Return the native connection on the application's async event loop."""
        if self._native is None:
            raise RuntimeError("Threadify is not connected")
        return self._native

    def telemetry_exporter(self) -> TelemetryExporter:
        """Construct the filtered destination once; Harnest owns batch processing."""
        if self._exporter is None:
            delegate = None
            # OTLP is optional; the SDK remains responsible for session threads.
            if self._otlp_endpoint is not None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                delegate = OTLPSpanExporter(
                    endpoint=self._otlp_endpoint, headers=self._otlp_headers,
                    timeout=self.timeout,
                )
            self._exporter = BusinessExporter(self, delegate)
        return TelemetryExporter(name="threadify", traces=self._exporter)

    async def __aenter__(self) -> Threadify:
        """Connect only during runtime startup and install session correlation."""
        if self.loop is not None or self._closed:
            raise RuntimeError("Threadify resources cannot be entered twice")
        self.loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        # A missing secret is a configuration error, not a remote outage.
        key = os.environ[self.api_key_env]
        self._provider = trace.get_tracer_provider()
        if not callable(getattr(self._provider, "add_span_processor", None)):
            raise RuntimeError("Register Threadify's telemetry_exporter before its resource")
        try:
            self._native = await asyncio.wait_for(
                self.native_class.connect(key, service_name=self.service_name, **self._options),
                timeout=self.timeout,
            )
        except Exception:
            # Optional telemetry must not prevent the agent from serving requests.
            _LOG.warning("Threadify connection unavailable; business export disabled")
        self._provider.add_span_processor(SessionProcessor(self))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Flush while the SDK loop is alive, then release the connection."""
        try:
            # BatchSpanProcessor waits synchronously; keep the SDK event loop free.
            await asyncio.to_thread(self._provider.force_flush, int(self.timeout * 1000))
        except Exception:
            _LOG.warning("Threadify flush failed")
        finally:
            try:
                if self._native is not None:
                    await asyncio.wait_for(self._native.close(), timeout=self.timeout)
            except Exception:
                _LOG.warning("Threadify connection close failed")
            finally:
                self._native = None
                self._bindings.clear()
                self._threads.clear()
                self._uncertain.clear()
                self._closed = True

    def _key(self, user_id, session_id):
        """Scope lookup references without exporting raw authenticated user IDs."""
        identity = json.dumps([self.service_name, user_id, session_id], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()

    async def session(self, *, user_id: str, session_id: str) -> ThreadInstance | None:
        """Reuse a session thread and recover its durable reference after restart."""
        if self._native is None:
            return None
        key = self._key(user_id, session_id)
        try:
            # SDK start/join replies are action-correlated, so serialize requests.
            async with self._lock:
                return await asyncio.wait_for(
                    self._session(key, session_id), timeout=self.timeout,
                )
        except Exception:
            # Never create a replacement after an ambiguous lookup/start failure.
            _LOG.warning("Threadify session link unavailable")
            return None

    async def _session(self, key, session_id):
        """Resolve once per owner-scoped session without closing threads per turn."""
        if key in self._bindings:
            return self._threads[self._bindings[key]]
        # Bound both SDK and integration ownership instead of silently evicting it.
        if key not in self._uncertain and len(self._bindings) + len(self._uncertain) >= self.max_sessions:
            raise RuntimeError("Threadify session capacity reached")
        existing = await self.native.get_thread_by_ref("harnestSession", key)
        if existing is not None:
            thread = await self.native.join(thread_id=existing.id, role="participant")
        else:
            # A timed-out start may have committed remotely. Require recovery
            # by reference rather than issuing a second creation for this key.
            if key in self._uncertain:
                return None
            self._uncertain.add(key)
            thread = await self.native.start(
                label=self.service_name,
                refs={"harnestSession": key, "sessionId": session_id},
            )
        self._uncertain.discard(key)
        self._bindings[key] = thread.thread_id
        self._threads[thread.thread_id] = thread
        return thread

    def annotate(self, span: Span, user_id: str, session_id: str) -> None:
        """Attach only an already verified session binding to a recording span."""
        key = self._key(user_id, session_id)
        thread_id = self._bindings.get(key)
        if thread_id is not None:
            span.set_attributes({
                "threadify.thread_id": thread_id,
                "threadify.ref.sessionId": session_id,
                "harnest.session.key": key,
            })

    async def session_created(self, info: AgentInfo, session: SessionRecord) -> None:
        """Link a committed new session without forwarding state or metadata."""
        await self.session(user_id=session.user_id, session_id=session.id)
        self._annotate_request(session.user_id, session.id)

    async def before_invocation(self, lifecycle_context: LifecycleContext, request: InvocationRequest) -> Next[Any]:
        """Recover sessions opened before integration and link every agent turn."""
        await self.session(user_id=request.user_id, session_id=request.session_id)
        self._annotate_request(request.user_id, request.session_id)
        return lifecycle_context.next()

    def _annotate_request(self, user_id, session_id):
        """Join server and current spans even when framework spans sit between them."""
        self.annotate(trace.get_current_span(), user_id, session_id)
        request = _REQUEST.get()
        if request is not None:
            self.annotate(request, user_id, session_id)

    @asynccontextmanager
    async def request_scope(self, http_context: HTTPLifecycleContext, request: HTTPCallRequest) -> AsyncIterator[None]:
        """Keep one payload-free business request span alive through streaming."""
        with get_tracer("harnest.threadify").start_as_current_span(
            "harnest.request", record_exception=False, set_status_on_exception=False,
        ) as span:
            token = _REQUEST.set(span)
            span.set_attribute("business.outcome", "completed")
            try:
                yield
            except BaseException:
                span.set_attribute("business.outcome", "failed")
                span.set_status(StatusCode.ERROR)
                raise
            finally:
                _REQUEST.reset(token)

    def after_http(self, http_context: HTTPLifecycleContext, response: HTTPResponseHead) -> Next[Any]:
        """Capture handled HTTP failures without exporting URL paths or headers."""
        span = _REQUEST.get()
        if span is not None and response.status_code >= 400:
            span.set_attribute("business.outcome", "failed")
            span.set_status(StatusCode.ERROR)
        return http_context.next()

    async def current_thread(self) -> ThreadInstance | None:
        """Expose the native thread for the active, owner-checked invocation."""
        active = context.current()
        return await self.session(user_id=active.user_id, session_id=active.session_id)

    @contextmanager
    def step(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
        """Record an explicit business step with the destination's attribute policy."""
        with get_tracer("harnest.threadify").start_as_current_span(
            "business." + name, attributes=attributes or {},
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException:
                span.set_status(StatusCode.ERROR)
                raise

    def tool_completed(self, active: Any, result: Any) -> Next[Any]:
        """Record only named business tools, without arguments or return values."""
        self._tool_outcome(active.tool_name, "completed")
        return active.next()

    def tool_failed(self, active: Any, error: BaseException) -> None:
        """Record failure categories without exception text or framework internals."""
        self._tool_outcome(active.tool_name, "failed")

    def _tool_outcome(self, name: str, outcome: str) -> None:
        """Exclude utility/skill tools unless the application deliberately selects them."""
        if name not in self.business_tools:
            return
        with self.step("tool", attributes={"business.tool": name, "business.outcome": outcome}) as span:
            if outcome == "failed":
                span.set_status(StatusCode.ERROR)

    async def export_spans(self, spans: Sequence[ReadableSpan]) -> None:
        """Translate sanitized spans using public SDK APIs and stable event keys."""
        async with self._lock:
            for span in spans:
                thread = self._threads.get(span.attributes["threadify.thread_id"])
                # Only threads resolved through our owner-scoped lookup can receive data.
                if thread is None:
                    continue
                step = thread.step(span.name).idempotency_key(
                    f"{span.context.trace_id:032x}:{span.context.span_id:016x}"
                )
                step.add_context(dict(span.attributes))
                if span.status.status_code is StatusCode.ERROR:
                    await step.failed()
                else:
                    await step.success()

"""In-process root-agent invocation for tasks and local adapters."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import math
from types import MappingProxyType
from typing import Any, AsyncIterator, Iterator, Literal, Mapping
import uuid

from ._json import json_value
from .approval import InMemoryApprovalStore
from .client_tool import InMemoryClientToolStore
from .logging import get_logger
from .runtime_continuation import next_run_boundary, start_approval_run
from .runtime_contract import (
    InvocationRequest,
    InvocationResult,
    RuntimeDriver,
    RuntimeEvent,
    SessionConflictError,
    require_customer_facing_output,
)
from .tracing import get_tracer


_AUDIT = get_logger("context_agent.audit")
_TRIGGERS = frozenset({"agent", "cron", "user"})


class AgentInvocationUnavailableError(RuntimeError):
    """No compiled runtime is bound to the current authored task."""


class AgentSessionNotFoundError(LookupError):
    """An identity-scoped local session does not exist."""


class AgentInvocationTimeout(RuntimeError):
    """An in-process agent invocation exceeded its configured deadline."""


class AgentContinuationUnsupportedError(RuntimeError):
    """A local invocation reached a continuation its caller cannot service."""

    def __init__(self, kind: str) -> None:
        """Identify the unsupported boundary without retaining run identity."""

        super().__init__(
            f"local agent invocation cannot return {kind}: its continuation "
            "is process-local and would become invalid when the task returns"
        )
        self.kind = kind


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """Framework-neutral result returned by an in-process invocation."""

    output_text: str
    result: Any
    events: tuple[RuntimeEvent, ...]
    session_id: str
    invocation_id: str
    metadata: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return strict JSON without stringifying unsupported output objects."""

        return json_value(
            {
                "outputText": self.output_text,
                "result": self.result,
                "events": self.events,
                "sessionId": self.session_id,
                "invocationId": self.invocation_id,
                "metadata": self.metadata,
            }
        )


@dataclass(frozen=True, slots=True)
class AgentPendingResponse:
    """A persisted external wait that can be resumed by the owning runtime."""

    status: Literal["in_progress"]
    session_id: str
    invocation_id: str
    pending_action: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        """Expose only provider-neutral durable continuation metadata."""

        return json_value(
            {
                "status": self.status,
                "sessionId": self.session_id,
                "invocationId": self.invocation_id,
                "pendingAction": self.pending_action,
            }
        )


@dataclass(frozen=True, slots=True)
class AgentStreamItem:
    """One portable runtime event or the validated terminal response."""

    kind: Literal["event", "completed", "pending"]
    sequence: int
    event: RuntimeEvent | None = field(default=None, repr=False)
    response: AgentResponse | None = field(default=None, repr=False)
    pending: AgentPendingResponse | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Keep consumers from mistaking an event for terminal completion."""

        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise TypeError("agent stream sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("agent stream sequence cannot be negative")
        _validate_stream_payload(self)

    def as_dict(self) -> dict[str, Any]:
        """Serialize a stable local envelope without importing HTTP contracts."""

        payload: dict[str, Any] = {
            "type": f"agent.{self.kind}",
            "sequence": self.sequence,
        }
        if self.kind == "event":
            payload["event"] = self.event
        elif self.kind == "completed":
            payload["response"] = self.response.as_dict()  # type: ignore[union-attr]
        else:
            payload["response"] = self.pending.as_dict()  # type: ignore[union-attr]
        return json_value(payload)


@dataclass(slots=True)
class _BindingLifetime:
    """Revoke runtime authority copied into child asyncio contexts."""

    active: bool = True


@dataclass(slots=True)
class _ContextAgentBinding:
    """Derive replay-stable child identities inside one task attempt."""

    runtime: LocalAgentRuntime
    namespace: str
    lifetime: _BindingLifetime
    next_session: int = 0

    def session_identity(self, key: str | None) -> str:
        """Use call order only when authored code did not provide a stable key."""

        if key is not None:
            return key
        current = self.next_session
        self.next_session += 1
        return str(current)


_ACTIVE_AGENT_RUNTIME: ContextVar[_ContextAgentBinding | None] = ContextVar(
    "harnest_context_agent_runtime", default=None
)


class LocalAgentRuntime:
    """Invoke a final compiled runtime driver without an HTTP round trip."""

    def __init__(
        self,
        driver: RuntimeDriver,
        *,
        user_id: str,
        transport: str = "local",
        trigger: Literal["agent", "cron", "user"] = "user",
        request_timeout: float = 300,
        identity_namespace: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Retain caller identity while leaving driver lifecycle with its owner."""

        if not isinstance(driver, RuntimeDriver):
            raise TypeError("local agent runtime requires a RuntimeDriver")
        _validate_text(user_id, "local agent user_id")
        _validate_text(transport, "local agent transport")
        if trigger not in _TRIGGERS:
            raise ValueError("local agent trigger must be agent, cron, or user")
        _validate_timeout(request_timeout)
        if identity_namespace is not None:
            _validate_text(identity_namespace, "local agent identity_namespace")
        self._driver = driver
        self._user_id = user_id
        self._transport = transport
        self._trigger = trigger
        self._request_timeout = float(request_timeout)
        self._identity_namespace = identity_namespace
        self._metadata = _json_mapping(
            metadata or {}, "local agent inherited metadata"
        )
        self._approvals = InMemoryApprovalStore()
        self._client_tools = InMemoryClientToolStore()

    @property
    def user_id(self) -> str:
        """Return the session owner selected by the local caller."""

        return self._user_id

    async def create_session(
        self,
        *,
        state: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        reuse_existing: bool = False,
    ) -> AgentSession:
        """Create one persisted session under the configured local identity."""

        normalized_state = _json_mapping(state or {}, "agent session state")
        selected_id = session_id or self._random_session_id()
        _validate_text(selected_id, "agent session_id")
        try:
            await self._driver.create_session(
                session_id=selected_id,
                user_id=self._user_id,
                state=normalized_state,
            )
        except SessionConflictError:
            if not reuse_existing:
                _audit("session.create", self._trigger, "failed", self._driver)
                raise
            try:
                existing = await self._driver.get_session(
                    session_id=selected_id, user_id=self._user_id
                )
            except Exception:
                _audit("session.create", self._trigger, "failed", self._driver)
                raise
            if existing is None:
                _audit("session.create", self._trigger, "failed", self._driver)
                raise RuntimeError("agent session creation conflict was not readable")
            _audit("session.create", self._trigger, "unchanged", self._driver)
        except asyncio.CancelledError:
            _audit("session.create", self._trigger, "cancelled", self._driver)
            raise
        except Exception:
            _audit("session.create", self._trigger, "failed", self._driver)
            raise
        else:
            _audit("session.create", self._trigger, "committed", self._driver)
        return self._session(selected_id)

    async def open_session(self, session_id: str) -> AgentSession:
        """Open an existing session without revealing another owner's records."""

        _validate_text(session_id, "agent session_id")
        existing = await self._driver.get_session(
            session_id=session_id, user_id=self._user_id
        )
        if existing is None:
            raise AgentSessionNotFoundError("agent session was not found")
        return self._session(session_id)

    def _session(self, session_id: str) -> AgentSession:
        """Bind stable invocation identity only when a task supplied a namespace."""

        namespace = self._identity_namespace
        return AgentSession(
            id=session_id,
            user_id=self._user_id,
            _runtime=self,
            _identity_namespace=namespace,
        )

    def _random_session_id(self) -> str:
        """Use an opaque local prefix so identities remain transport-independent."""

        return f"local_{uuid.uuid4().hex}"

    def _stable_session_id(self, identity: str) -> str:
        """Derive a retry-stable ID without placing authored keys in persistence."""

        namespace = self._identity_namespace
        if namespace is None:  # pragma: no cover - guarded by context facade
            return self._random_session_id()
        digest = _opaque_identity(
            self._driver.info.id,
            self._user_id,
            namespace,
            identity,
        )
        return f"child_{digest}"

    def _request(
        self,
        session: AgentSession,
        value: Any,
        metadata: Mapping[str, Any] | None,
    ) -> InvocationRequest:
        """Build a portable request without bypassing the final driver pipeline."""

        _validate_input(value, self._driver)
        invocation_id = session._next_invocation_id()
        supplied_metadata = _json_mapping(
            metadata or {}, "agent invocation metadata"
        )
        return InvocationRequest(
            input=value,
            user_id=self._user_id,
            session_id=session.id,
            invocation_id=invocation_id,
            metadata={**self._metadata, **supplied_metadata},
            state_delta={},
            transport=self._transport,
        )

    async def _invoke(
        self,
        session: AgentSession,
        value: Any,
        *,
        metadata: Mapping[str, Any] | None,
        request_timeout: float | None,
    ) -> AgentResponse | AgentPendingResponse:
        """Run locally while surfacing only replica-safe durable continuations."""

        timeout = self._timeout(request_timeout)
        request = self._request(session, value, metadata)
        run = self._start_run(request, stream=False)
        join_terminal = False
        try:
            kind, boundary = await next_run_boundary(run, timeout=timeout)
            join_terminal = kind in {"result", "error", "external_continuation"}
            result = _invocation_boundary(kind, boundary, request=request)
            if isinstance(result, AgentPendingResponse):
                response: AgentResponse | AgentPendingResponse = result
            else:
                response = _agent_response(result, request.invocation_id)
        except asyncio.TimeoutError as error:
            _audit("invoke", self._trigger, "failed", self._driver)
            raise AgentInvocationTimeout("local agent invocation timed out") from error
        except asyncio.CancelledError:
            _audit("invoke", self._trigger, "cancelled", self._driver)
            raise
        except AgentContinuationUnsupportedError:
            _audit("invoke", self._trigger, "failed", self._driver)
            raise
        except Exception:
            _audit("invoke", self._trigger, "failed", self._driver)
            raise
        finally:
            await self._stop_run(run, join_terminal=join_terminal)
        outcome = (
            "suspended" if isinstance(response, AgentPendingResponse) else "committed"
        )
        _audit("invoke", self._trigger, outcome, self._driver)
        return response

    async def _stream(
        self,
        session: AgentSession,
        value: Any,
        *,
        metadata: Mapping[str, Any] | None,
        request_timeout: float | None,
    ) -> AsyncIterator[AgentStreamItem]:
        """Yield portable events and one validated terminal local envelope."""

        timeout = self._timeout(request_timeout)
        request = self._request(session, value, metadata)
        run = self._start_run(request, stream=True)
        sequence = 0
        deadline = asyncio.get_running_loop().time() + timeout
        outcome = "cancelled"
        join_terminal = False
        try:
            while True:
                kind, boundary = await _next_before_deadline(run, deadline)
                if kind == "event":
                    yield AgentStreamItem("event", sequence, event=dict(boundary))
                    sequence += 1
                    continue
                join_terminal = kind in {"result", "error", "external_continuation"}
                result = _invocation_boundary(kind, boundary, request=request)
                if isinstance(result, AgentPendingResponse):
                    outcome = "suspended"
                    yield AgentStreamItem("pending", sequence, pending=result)
                else:
                    response = _agent_response(result, request.invocation_id)
                    outcome = "committed"
                    yield AgentStreamItem("completed", sequence, response=response)
                return
        except asyncio.TimeoutError as error:
            outcome = "failed"
            raise AgentInvocationTimeout("local agent invocation timed out") from error
        except AgentContinuationUnsupportedError:
            outcome = "failed"
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            await self._stop_run(run, join_terminal=join_terminal)
            _audit("invoke", self._trigger, outcome, self._driver)

    def _start_run(self, request: InvocationRequest, *, stream: bool) -> Any:
        """Install the same wait scopes as neutral HTTP without its wire layer."""

        run = start_approval_run(
            self._approvals,
            self._client_tools,
            self._driver,
            request,
            stream=stream,
            external_continuations=getattr(
                self._driver, "external_continuations", None
            ),
        )
        run.activation.set()
        return run

    async def _stop_run(self, run: Any, *, join_terminal: bool = False) -> None:
        """Cancel and join incomplete work so local cancellation cannot leak tasks."""

        task = run.task
        if task is None or task.done():
            return
        if join_terminal:
            # A terminal notification precedes the run's final cleanup. Durable
            # waits also arm their framework suspension here; this never waits
            # for the external provider or retains the authored Python stack.
            await task
            return
        self._approvals.cancel_run(run)
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            # The caller already received the sanitized boundary or failure;
            # cleanup errors must not replace that result with provider details.
            return

    def _timeout(self, value: float | None) -> float:
        """Use one validated timeout for the entire local invocation."""

        if value is None:
            return self._request_timeout
        _validate_timeout(value)
        return float(value)


@dataclass(slots=True)
class AgentSession:
    """One persisted session owned by a local in-process runtime identity."""

    id: str
    user_id: str
    _runtime: LocalAgentRuntime = field(repr=False)
    _identity_namespace: str | None = field(default=None, repr=False)
    _lifetime: _BindingLifetime | None = field(default=None, repr=False)
    _next_invocation: int = field(default=0, init=False, repr=False)

    async def invoke(
        self,
        value: Any,
        *,
        metadata: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> AgentResponse | AgentPendingResponse:
        """Run one non-streaming turn through the complete runtime pipeline."""

        self._require_active()
        with _invocation_span(self._runtime):
            return await self._runtime._invoke(
                self,
                value,
                metadata=metadata,
                request_timeout=request_timeout,
            )

    async def stream(
        self,
        value: Any,
        *,
        metadata: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> AsyncIterator[AgentStreamItem]:
        """Stream portable events and a final response with cancellation cleanup."""

        self._require_active()
        with _invocation_span(self._runtime):
            # Explicitly closing the inner generator propagates CLI disconnects
            # and Ctrl-C before Python's eventual async-generator finalization.
            async with aclosing(
                self._runtime._stream(
                    self,
                    value,
                    metadata=metadata,
                    request_timeout=request_timeout,
                )
            ) as source:
                async for item in source:
                    yield item

    def _next_invocation_id(self) -> str:
        """Preserve task-retry identity while local callers receive fresh runs."""

        current = self._next_invocation
        self._next_invocation += 1
        if self._identity_namespace is None:
            return f"local_{uuid.uuid4().hex}"
        digest = _opaque_identity(
            self._runtime._driver.info.id,
            self.user_id,
            self._identity_namespace,
            self.id,
            str(current),
        )
        return f"local_{digest}"

    def _require_active(self) -> None:
        """Reject a task-owned runtime capability after the task returns."""

        if self._lifetime is not None and not self._lifetime.active:
            raise AgentInvocationUnavailableError(
                "context.agent sessions are available only during their task"
            )


class _ContextAgentAccess:
    """Expose only the local root-agent operations safe inside a task."""

    async def create_session(
        self,
        *,
        state: Mapping[str, Any] | None = None,
        key: str | None = None,
    ) -> AgentSession:
        """Create or recover a replay-stable child session for this task."""

        binding = _active_binding()
        if key is not None:
            _validate_text(key, "context.agent session key")
        identity = binding.session_identity(key)
        session_id = binding.runtime._stable_session_id(identity)
        session = await binding.runtime.create_session(
            state=state,
            session_id=session_id,
            reuse_existing=True,
        )
        session._lifetime = binding.lifetime
        session._identity_namespace = f"{binding.namespace}:session:{identity}"
        return session


@contextmanager
def activate_context_agent(runtime: LocalAgentRuntime) -> Iterator[None]:
    """Bind task-scoped child invocation authority and revoke copied contexts."""

    if not isinstance(runtime, LocalAgentRuntime):
        raise TypeError("context agent binding requires LocalAgentRuntime")
    namespace = runtime._identity_namespace
    if namespace is None:
        raise ValueError("context agent binding requires an identity namespace")
    lifetime = _BindingLifetime()
    binding = _ContextAgentBinding(runtime, namespace, lifetime)
    token = _ACTIVE_AGENT_RUNTIME.set(binding)
    try:
        yield
    finally:
        lifetime.active = False
        _ACTIVE_AGENT_RUNTIME.reset(token)


def _active_binding() -> _ContextAgentBinding:
    """Resolve one task binding without falling back to direct target imports."""

    binding = _ACTIVE_AGENT_RUNTIME.get()
    if binding is None or not binding.lifetime.active:
        raise AgentInvocationUnavailableError(
            "context.agent requires an active compiled Harnest task runtime"
        )
    return binding


def _invocation_boundary(
    kind: str, value: Any, *, request: InvocationRequest
) -> InvocationResult | AgentPendingResponse:
    """Return durable waits while rejecting process-local continuation IDs."""

    if kind == "result":
        if not isinstance(value, InvocationResult):
            raise TypeError("local runtime returned an invalid invocation result")
        require_customer_facing_output(value.text, value.result)
        return value
    if kind == "error":
        raise value
    if kind == "external_continuation":
        public = value.public()
        return AgentPendingResponse(
            status="in_progress",
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            pending_action=MappingProxyType(dict(public)),
        )
    if kind in {"approval", "client_tool"}:
        raise AgentContinuationUnsupportedError(kind)
    raise RuntimeError("local runtime returned an unexpected invocation boundary")


def _agent_response(result: InvocationResult, invocation_id: str) -> AgentResponse:
    """Detach mutable backend containers before returning authored output."""

    return AgentResponse(
        output_text=result.text,
        result=result.result,
        events=tuple(dict(event) for event in result.events),
        session_id=result.session_id,
        invocation_id=invocation_id,
        metadata=MappingProxyType(dict(result.metadata)),
    )


async def _next_before_deadline(run: Any, deadline: float) -> tuple[str, Any]:
    """Apply one absolute timeout across all streamed events."""

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await next_run_boundary(run, timeout=remaining)


def _json_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Normalize public containers through Harnest's strict JSON boundary."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized = json_value(value)
    if not isinstance(normalized, dict):  # pragma: no cover - mapping owns shape
        raise TypeError(f"{name} must normalize to an object")
    return normalized


def _validate_stream_payload(item: AgentStreamItem) -> None:
    """Require exactly one payload whose type matches the declared item kind."""

    expected = {
        "event": (item.event, dict),
        "completed": (item.response, AgentResponse),
        "pending": (item.pending, AgentPendingResponse),
    }
    if item.kind not in expected:
        raise TypeError("agent stream item kind is invalid")
    payload, payload_type = expected[item.kind]
    if not isinstance(payload, payload_type):
        raise TypeError("agent stream item payload does not match its kind")
    populated = sum(
        value is not None for value in (item.event, item.response, item.pending)
    )
    if populated != 1:
        raise TypeError("agent stream item must contain exactly one payload")


def _validate_input(value: Any, driver: RuntimeDriver) -> None:
    """Reject empty default text while leaving authored schemas to the backend."""

    if driver.info.input_schema is None and (
        not isinstance(value, str) or not value.strip()
    ):
        raise ValueError("agent input must be non-empty text")


def _validate_text(value: Any, name: str) -> None:
    """Reject ambiguous identities before they reach framework persistence."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value) > 512:
        raise ValueError(f"{name} cannot exceed 512 characters")


def _validate_timeout(value: Any) -> None:
    """Keep local execution deadlines finite and cancellation-friendly."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("local agent request_timeout must be positive and finite")


def _opaque_identity(*values: str) -> str:
    """Hash durable identity inputs so authored keys never enter logs or stores."""

    encoded = "\0".join(values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _invocation_span(runtime: LocalAgentRuntime) -> Iterator[None]:
    """Trace local execution without prompts, results, user, or session IDs."""

    # Automatic exception recording can capture provider messages. The
    # payload-free audit signal below owns failure reporting for this boundary.
    with get_tracer("harnest.agent").start_as_current_span(
        "harnest.agent.local.invoke",
        attributes={
            "harnest.agent.name": runtime._driver.info.id,
            "harnest.framework": runtime._driver.info.framework or "unknown",
            "harnest.transport": runtime._transport,
            "harnest.trigger": runtime._trigger,
        },
        record_exception=False,
        set_status_on_exception=False,
    ):
        yield


def _audit(
    operation: str,
    trigger: str,
    outcome: str,
    driver: RuntimeDriver,
) -> None:
    """Record only low-cardinality execution facts after durable boundaries."""

    _AUDIT.info(
        f"context_agent.{operation}",
        operation=f"context_agent.{operation}",
        trigger=trigger,
        outcome=outcome,
        action="root_agent",
        framework=driver.info.framework or "unknown",
    )


agent = _ContextAgentAccess()


__all__ = [
    "AgentContinuationUnsupportedError",
    "AgentInvocationTimeout",
    "AgentInvocationUnavailableError",
    "AgentPendingResponse",
    "AgentResponse",
    "AgentSession",
    "AgentSessionNotFoundError",
    "AgentStreamItem",
    "LocalAgentRuntime",
]

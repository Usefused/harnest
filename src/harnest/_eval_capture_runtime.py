"""Run the authored eval capture rule after invocations, off the response path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import inspect
import json
import os
import random
from typing import Any, Callable
import uuid

from .eval_capture import (
    CaptureEvent,
    CaptureRecord,
    CaptureStore,
    ToolActivity,
    default_redactor,
)
from .lifecycle import eval_capture_options
from .logging import get_logger
from .runtime_contract import InvocationRequest, InvocationResult, RuntimeEvent

_AUDIT = get_logger("eval.capture.audit")
_ENABLED_ENV = "HARNEST_EVAL_CAPTURE"
_SAMPLE_RATE_ENV = "HARNEST_EVAL_CAPTURE_SAMPLE_RATE"
_OFF_VALUES = frozenset({"0", "false", "off", "no", "disabled"})
# Pending captures get a bounded grace period at shutdown so a slow store can
# never hold a deployment's termination hostage.
_CLOSE_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class EvalCapturePolicy:
    """Resolved capture behaviour: authored code first, then environment, then defaults."""

    rule: Callable[[CaptureEvent], Any]
    redactor: Callable[[CaptureRecord], Any] | None
    store: CaptureStore
    sample_rate: float


def eval_capture_policy(
    listeners: Sequence[Any],
    custom_stores: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> EvalCapturePolicy | None:
    """Resolve capture, preserving absence of a redactor as structure-only policy."""

    environ = os.environ if environ is None else environ
    capture = _single(listeners, "eval_capture")
    if capture is None or _switched_off(environ):
        return None
    options = eval_capture_options(capture.callback)
    if options is None:
        return None
    store = custom_stores.get(options.store)
    if not isinstance(store, CaptureStore):
        # Discovery already refuses this; a hand-built application falls back
        # to no capture rather than failing requests.
        return None
    redact = _single(listeners, "eval_redact")
    return EvalCapturePolicy(
        rule=capture.callback,
        redactor=redact.callback if redact is not None else None,
        store=store,
        sample_rate=_sample_rate(options.sample_rate, environ),
    )


def with_eval_capture(driver: Any, application: Any) -> Any:
    """Wrap a runtime driver when ``lifecycle/evals_improvement.py`` enables capture."""

    policy = eval_capture_policy(
        getattr(application, "lifecycle_extensions", ()),
        getattr(application, "custom_stores", {}),
    )
    return driver if policy is None else EvalCaptureRuntimeDriver(driver, policy)


class EvalCaptureRuntimeDriver:
    """Observe invocations and capture them in the background without changing results."""

    def __init__(self, driver: Any, policy: EvalCapturePolicy) -> None:
        """Keep the wrapped driver and the resolved policy."""

        self._driver = driver
        self._policy = policy
        self._pending: set[asyncio.Task[None]] = set()

    def __getattr__(self, name: str) -> Any:
        """Expose every other driver capability unchanged (sessions, continuations, ...)."""

        return getattr(self._driver, name)

    @property
    def info(self) -> Any:
        """Return the wrapped driver's metadata."""

        return self._driver.info

    async def start(self) -> None:
        """Start the wrapped pipeline when it supports eager start."""

        starter = getattr(self._driver, "start", None)
        if callable(starter):
            await starter()

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        """Return the wrapped result unchanged, then capture it in the background."""

        try:
            result = await self._driver.invoke(request)
        except Exception as error:
            self._schedule(request, (), "", "failed", type(error).__name__)
            raise
        self._schedule(request, tuple(result.events), result.text, "completed", None)
        return result

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        """Yield every event unchanged and capture the finished stream."""

        events: list[RuntimeEvent] = []
        status, error_type = "cancelled", None
        try:
            async for event in self._driver.stream(request):
                events.append(event)
                yield event
            status = "completed"
        except Exception as error:
            status, error_type = "failed", type(error).__name__
            raise
        finally:
            self._schedule(request, tuple(events), None, status, error_type)

    async def close(self) -> None:
        """Give pending captures a bounded grace period, then close the driver."""

        if self._pending:
            await asyncio.wait(set(self._pending), timeout=_CLOSE_GRACE_SECONDS)
        for task in tuple(self._pending):
            task.cancel()
        await self._driver.close()

    def _schedule(
        self,
        request: InvocationRequest,
        events: tuple[RuntimeEvent, ...],
        text: str | None,
        status: str,
        error_type: str | None,
    ) -> None:
        """Hand the finished invocation to a background task the response never awaits."""

        if status == "cancelled":
            return
        task = asyncio.create_task(self._capture(request, events, text, status, error_type))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _capture(
        self, request: InvocationRequest, events: tuple[RuntimeEvent, ...], text: str | None,
        status: str, error_type: str | None,
    ) -> None:
        """Keep projection failures as well as policy and store failures off the response path."""

        agent_name = "agent"
        try:
            agent_name = str(getattr(self._driver.info, "name", "agent"))
            event = _capture_event(agent_name, request, events, text, status, error_type)
            reasons = await self._reasons(event)
            if not reasons:
                return
            record = await _redacted(self._policy.redactor, _record(event, reasons))
            await self._policy.store.append(record)
        except Exception as error:  # noqa: BLE001 - capture must never affect users
            _AUDIT.warning(
                "eval.capture.failed",
                trigger="runtime",
                outcome="failed",
                agent=agent_name,
                error=type(error).__name__,
            )
            return
        _AUDIT.info(
            "eval.capture.stored",
            trigger="runtime",
            outcome="stored",
            agent=event.agent_name,
            reason=reasons[0],
        )

    async def _reasons(self, event: CaptureEvent) -> tuple[str, ...]:
        """Return why this invocation is captured: the rule first, then sampling."""

        decision = self._policy.rule(event)
        if inspect.isawaitable(decision):
            decision = await decision
        if decision is True:
            return ("rule",)
        if self._policy.sample_rate and random.random() < self._policy.sample_rate:
            return ("sample",)
        return ()


async def _redacted(
    redactor: Callable[[CaptureRecord], Any] | None, record: CaptureRecord
) -> CaptureRecord:
    """Omit content by default; only an authored redactor can authorize retention."""

    # Keeping this distinction avoids labeling omission or heuristic guesses as
    # successful execution of a team-owned redaction policy.
    if redactor is None:
        return default_redactor(record)
    value = redactor(record)
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, CaptureRecord):
        raise TypeError("eval redactor must return a CaptureRecord")
    # The runtime owns this marker; redactors need only return their edited record.
    return replace(value, content="redacted")


def _capture_event(
    agent_name: str,
    request: InvocationRequest,
    events: tuple[RuntimeEvent, ...],
    text: str | None,
    status: str,
    error_type: str | None,
) -> CaptureEvent:
    """Project framework-neutral events into the facts a rule can judge."""

    return CaptureEvent(
        agent_name=agent_name,
        invocation_id=request.invocation_id,
        session_id=request.session_id,
        user_id=request.user_id,
        transport=request.transport,
        status=status,
        input_text=_input_text(request.input),
        output_text=text if text is not None else _streamed_text(events),
        tool_calls=_tool_activity(events),
        error_type=error_type,
        metadata=dict(request.metadata or {}),
    )


def _record(event: CaptureEvent, reasons: tuple[str, ...]) -> CaptureRecord:
    """Copy only what a draft eval case needs; user and session ids stay behind."""

    return CaptureRecord(
        id=uuid.uuid4().hex,
        captured_at=datetime.now(timezone.utc).isoformat(),
        agent_name=event.agent_name,
        invocation_id=event.invocation_id,
        reasons=reasons,
        status=event.status,
        input_text=event.input_text,
        output_text=event.output_text,
        tool_calls=event.tool_calls,
        error_type=event.error_type,
    )


def _input_text(value: Any) -> str:
    """Reduce text, mappings and content-part lists to the user's words."""

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        return value["text"]
    if isinstance(value, (list, tuple)):
        return "".join(_input_text(item) for item in value)
    return "" if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _streamed_text(events: Sequence[RuntimeEvent]) -> str:
    """Join streamed message deltas into the reply the user saw."""

    return "".join(
        str(event.get("text", "")) for event in events if event.get("type") == "message"
    )


def _tool_activity(events: Sequence[RuntimeEvent]) -> tuple[ToolActivity, ...]:
    """Pair tool calls with results by id, falling back to call order."""

    calls: list[dict[str, Any]] = []
    by_id: dict[Any, dict[str, Any]] = {}
    for event in events:
        kind = event.get("type")
        if kind == "tool_call":
            item = {"name": str(event.get("name") or ""), "arguments": event.get("arguments")}
            calls.append(item)
            if event.get("id") is not None:
                by_id[event.get("id")] = item
        elif kind == "tool_result":
            _attach_result(calls, by_id, event)
    return tuple(
        ToolActivity(
            name=item["name"],
            arguments=item.get("arguments"),
            result=item.get("result"),
            error=bool(item.get("error", False)),
        )
        for item in calls
    )


def _attach_result(
    calls: list[dict[str, Any]], by_id: Mapping[Any, dict[str, Any]], event: RuntimeEvent
) -> None:
    """Attach one tool result to its call, or record an orphan result."""

    call_id = event.get("id")
    target = by_id.get(call_id)
    if call_id is None:
        # Only id-less results can use call order. An unknown explicit ID is an orphan.
        name = event.get("name")
        target = next((item for item in calls if "result" not in item and (not name or item["name"] == name)), None)
    if target is None:
        target = {"name": str(event.get("name") or "")}
        calls.append(target)
    result = event.get("result", event.get("output"))
    target["result"] = result
    target["error"] = _looks_like_error(event, result)


def _looks_like_error(event: Mapping[str, Any], result: Any) -> bool:
    """Recognise the error shapes ADK and LangGraph tools commonly return."""

    if event.get("status") == "error" or event.get("isError") is True:
        return True
    return isinstance(result, Mapping) and bool(result.get("error"))


def _single(listeners: Sequence[Any], phase: str) -> Any | None:
    """Return the one listener for a phase; discovery guarantees at most one."""

    return next((item for item in listeners if getattr(item, "phase", None) == phase), None)


def _switched_off(environ: Mapping[str, str]) -> bool:
    """Let one environment switch disable capture without editing code."""

    return environ.get(_ENABLED_ENV, "").strip().lower() in _OFF_VALUES


def _sample_rate(authored: float | None, environ: Mapping[str, str]) -> float:
    """Resolve the rate: code argument, then environment, then zero."""

    if authored is not None:
        return authored
    raw = environ.get(_SAMPLE_RATE_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if 0 <= value <= 1 else 0.0


__all__ = [
    "EvalCapturePolicy",
    "EvalCaptureRuntimeDriver",
    "eval_capture_policy",
    "with_eval_capture",
]

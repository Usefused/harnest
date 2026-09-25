"""Apply Jev's browser-use judgment before the agent and every desktop tool."""

from contextvars import ContextVar
from dataclasses import replace

from harnest import context, lifecycle
from harnest.decisions import DecisionAction


DESKTOP_TOOLS = frozenset({
    "browser_navigate", "desktop_screenshot", "desktop_click", "desktop_type", "desktop_key",
})
RECENT_REQUEST_LIMIT = 2
RECENT_REQUEST_LENGTH = 1500
LAST_REPLY_LENGTH = 2000
_decision: ContextVar[tuple[str, bool] | None] = ContextVar("browser_use_decision", default=None)


@lifecycle.agent.before
async def decide_browser_use(lifecycle_context, request):
    """Ask Jev whether this turn needs the desktop, including recent user context."""
    if not isinstance(request.input, str) or not request.input.strip():
        raise ValueError("Jev browser routing requires a non-empty text request")
    session = context.session.namespace("browser_routing")
    stored = await session.get("recent_user_requests", [])
    last_reply = await session.get("last_agent_reply", "")
    prior = (
        [item for item in stored[-RECENT_REQUEST_LIMIT:] if isinstance(item, str)]
        if isinstance(stored, list) else []
    )
    evaluation = await context.decisions.evaluate(
        "browser_use", {
            "request": request.input[:8000],
            "prior_user_requests": prior,
            "last_agent_reply": last_reply if isinstance(last_reply, str) else "",
        }
    )
    if evaluation.error is not None:
        raise RuntimeError("Jev browser decision failed")
    # Keep only recent user turns in this session; the next brief follow-up can
    # refer to them without exposing another session's history to Jev.
    recent = [*prior, request.input[:RECENT_REQUEST_LENGTH]][-RECENT_REQUEST_LIMIT:]
    await session.set("recent_user_requests", recent)
    allowed = evaluation.outcome.action is DecisionAction.PROCEED
    _decision.set((lifecycle_context.invocation_id, allowed))
    # The model sees Jev's decision, while the tool hook below enforces it even
    # if model output or untrusted page content suggests a different action.
    guidance = (
        "Jev browser-use decision: browser and desktop tools are allowed for this request."
        if allowed else
        "Jev browser-use decision: answer directly without browser or desktop actions "
        "for this request. The desktop remains running; do not describe its tools as "
        "expired, disconnected, or waiting to switch back on."
    )
    return lifecycle_context.next(replace(request, input=f"{request.input}\n\n{guidance}"))


@lifecycle.tool.before
async def enforce_browser_use(tool_context, call):
    """Prevent desktop actions unless Jev allowed them for this invocation."""
    if call.name not in DESKTOP_TOOLS:
        return tool_context.next()
    decision = _decision.get()
    if decision is None or decision[0] != tool_context.invocation_id:
        raise RuntimeError("desktop tool has no Jev decision for this invocation")
    if not decision[1]:
        return tool_context.finish(
            "Jev selected a direct response for this request. Do not use browser or desktop tools."
        )
    return tool_context.next()


@lifecycle.agent.after
async def clear_browser_use(lifecycle_context, result):
    """Keep the reply for short follow-ups and discard this turn's tool decision."""
    if result.text.strip():
        # The user's next confirmation refers to this reply, even after an idle
        # period. Retain its ending where the agent usually asks the question.
        session = context.session.namespace("browser_routing")
        await session.set("last_agent_reply", result.text[-LAST_REPLY_LENGTH:])
    _decision.set(None)
    return lifecycle_context.next()

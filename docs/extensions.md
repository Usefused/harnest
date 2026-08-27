# Runtime extensions

`extensions/` contains runtime behavior that extends an agent without adding
capability resources. Typical uses include storing conversations in BigQuery,
running input or output guardrails, recording audits, or adding
framework-specific model and tool controls.

Each extension is a directory. Its portable lifecycle lives in
`lifecycle.py` and exports an `Extension` named `extension`:

```text
extensions/
└── bigquery/
    ├── lifecycle.py
    ├── adk.py          # optional
    └── langgraph.py    # optional
```

```python
from dataclasses import replace

from harnest.extension import DROP_EVENT, Extension


def before_invoke(context, request):
    # Input guardrail: return a replacement request, or None to keep it.
    context.attributes["input"] = request.input
    return replace(request, input=check_input(request.input))


def on_event(context, event):
    # Output guardrail: this runs before JSON/SSE/WebSocket output is exposed.
    if should_block(event):
        return DROP_EVENT
    return sanitize(event)  # Return None to keep the event unchanged.


async def after_invoke(context, result):
    # Persistence/audit: result has already passed through on_event hooks.
    await conversation_store.write(
        invocation_id=context.invocation_id,
        user_id=context.user_id,
        session_id=context.session_id,
        input=context.attributes.get("input"),
        output=result.text,
    )


extension = Extension(
    name="bigquery",
    before_invoke=before_invoke,
    on_event=on_event,
    after_invoke=after_invoke,
)
```

Hooks may be synchronous or asynchronous. `before_invoke` receives an
`InvocationRequest`; `on_event` receives one normalized runtime event;
`after_invoke` receives an `InvocationResult`; and `on_error` receives the
original exception as a notification. Transforming hooks return a replacement
of the same type or `None` to keep the current value. `on_event` may return
`DROP_EVENT` to suppress an event. `context.attributes` is an invocation-local
scratchpad shared by all four hooks.

A real BigQuery extension can create a `google.cloud.bigquery.Client` in this
module and insert from `after_invoke`; that dependency belongs in the agent's
`requirements.txt`. Use `context.invocation_id` as the insert ID so retries are
idempotent. The lifecycle contract is storage-neutral—the same extension shape
works for BigQuery, Postgres, object storage, or an audit queue.

The `Extension.name` must match the containing directory. Portable lifecycle
behavior is the stable Harnest layer and applies to direct execution and the
neutral JSON, SSE, and WebSocket surfaces.

An extension may optionally add one native implementation for tighter control:

- `adk.py` integrates with ADK's native plugin lifecycle.
- `langgraph.py` integrates with LangGraph's native agent middleware.

Only the file for the selected framework is loaded. This lets an extension use
native model, tool, agent, and error hooks without forcing the other framework's
dependencies to be installed. Portable and native pieces are additive: the
portable lifecycle surrounds the request and normalized output, while the
native implementation runs inside the selected framework.

Each native file also exports exactly `extension`. For ADK it is a
`google.adk.plugins.BasePlugin` instance whose name matches the directory. For
LangGraph it is a `langchain.agents.middleware.AgentMiddleware` instance. The
compiler passes ADK plugins to `App(..., plugins=[...])` and LangGraph
middleware to every managed `Agent` node before `create_agent` is called.

Extension directories are discovered in sorted name order. Missing, empty, and
ignored-only directories are skipped. Public files other than `lifecycle.py`,
`adk.py`, and `langgraph.py` are rejected instead of being silently ignored.
An extension does not contribute tools, agents, MCP clients, skills, sandbox
policy, evals, tests, configuration, or Agent Card fields.

Streaming guardrails must inspect or transform output before each event is
emitted. A hook that runs after a response can persist or audit the completed
conversation, but it cannot retract bytes already delivered over SSE or a
WebSocket. Extension failures follow the runtime's normal request or terminal
stream error behavior; persistence integrations should use the invocation ID
as an idempotency key when retries are possible.

ADK's official native endpoints bypass the portable Harnest transport layer.
An extension that must also cover those routes should provide `adk.py`.
Advanced `Agent.advanced(...)` applications own their framework wiring, so they
add lifecycle integration or middleware directly in `agent.py`.

Use [plugins](plugins.md) when the goal is to package MCP client connections
with skills that teach the host agent how to use their tools.

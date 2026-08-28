# Runtime extensions

Root `extensions/**/*.py` files add application lifecycle behavior without
adding tools, agents, MCP clients, or skills. Files and nested directories may
have any valid public Python name. Harnest ignores ordinary helper functions and
discovers only functions decorated through `harnest.lifecycle`.

An agent may declare one root session-store factory. `harnest init --example`
creates `extensions/sessions.py` with the development-only in-memory store:

```python
from harnest.lifecycle import lifecycle
from harnest.session import InMemorySessionStore


@lifecycle.session_store
def session_store():
    return InMemorySessionStore()
```

The optional synchronous zero-argument factory runs once when the compiled
application is created. Harnest starts its returned `SessionStore` before first use, shares
it across JSON, SSE, WebSocket, and framework adapters, and closes it with the
application. Replace the return value with a durable Harnest store or a custom
`SessionStore` implementation for production. Duplicate, asynchronous, or
incorrectly typed factories fail compilation. A missing factory keeps the
development fallback. Session storage cannot also be
injected by the host, because that would create competing authorities.

```python
# extensions/history.py
from harnest.lifecycle import lifecycle


@lifecycle.before_invoke(order=10)
async def apply_policy(context, request):
    return request


@lifecycle.after_invoke(order=20)
async def persist_result(context, result):
    await save(result)
    # None observes without replacing the result.
```

A file may define multiple listeners, and multiple files may listen to the same
phase. Execution order is explicit `order`, then relative file path, source
line, and function name. Put shared implementation in root `lib/` and import it
through `harnest.lib.*`. Nested agents cannot own extensions.

## Portable phases

- `authenticate(connection, principal)` runs once on each protected HTTP/SSE
  request or WebSocket handshake. It returns `AuthPrincipal` or `None`. The
  read-only `ConnectionContext` contains transport, method, path, headers,
  cookies, and query values without exposing FastAPI. The pipeline starts at
  `None`, fails closed if no listener resolves a principal, and never permits an
  established `user_id` to change. With no auth listeners, local anonymous mode
  remains enabled.
- `before_invoke(context, request)` pipelines `InvocationRequest` values.
- `on_event(context, event)` pipelines runtime events; return `DROP_EVENT` to
  suppress one.
- `after_invoke(context, result)` pipelines `InvocationResult`. A streamed
  result is already public, so its after hook is observational.
- `on_error(context, error)` observes failures. Observer failures never replace
  the primary error.
- `before_model(context, request)` receives neutral `ModelCallRequest`; return a
  replacement, `ModelCallResponse` to short-circuit, or `None`.
- `after_model(context, response)` pipelines neutral `ModelCallResponse`.
- `on_model_error(context, error)` observes model failures without masking them.

Managed ADK and LangGraph agents lower portable model listeners through native
model callbacks/middleware while keeping provider request types private. The v1
portable contract transforms visible text or short-circuits a call; it cannot
add/remove messages, change roles, or switch a LangGraph model. Native opaque
fields, tool calls, metadata, usage, IDs, and structured output are preserved.
Use a native decorator for structural control. Arbitrary advanced native model
calls do not receive portable hooks automatically; configure Harnest's LiteLLM
lifecycle or a native plugin/middleware explicitly.

## Native integrations

Use an explicit synchronous zero-argument factory when portable hooks cannot
express a framework feature:

```python
@lifecycle.adk_plugin(order=30)
def audit_plugin():
    from google.adk.plugins.base_plugin import BasePlugin
    return BasePlugin(name="audit")


@lifecycle.langgraph_middleware(order=30)
def guardrail_middleware():
    from langchain.agents.middleware import AgentMiddleware
    return AgentMiddleware()
```

ADK plugins retain all native `BasePlugin` callbacks and are attached in order
to `App.plugins`. LangGraph middleware is passed to managed agent construction.
Advanced LangGraph graphs are already compiled, so middleware must instead be
wired before `Agent.advanced(...)`; Harnest rejects a discovered late factory.
Wrong native return types, duplicate ADK plugin names, symlinks, and public
non-Python extension resources fail compilation. A native factory targeting the
agent's other framework also fails instead of being silently ignored.

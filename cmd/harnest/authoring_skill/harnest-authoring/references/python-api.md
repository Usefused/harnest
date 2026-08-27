# Compiler-provided Python API

Read this reference when editing authored Python or selecting a resource type.
Every symbol must be imported explicitly.

## Root agents and portable graphs

```python
from harnest.agent import Agent
from harnest.graph import START, Edge, Graph
from harnest.model import LiteLLMModel


root_agent = Graph(
    name="support",
    nodes={
        "respond": Agent(
            name="responder",
            model=LiteLLMModel("ollama_chat/qwen3.5:cloud"),
        ),
    },
    edges=(Edge(START, "respond"),),
)
```

`Agent` is an alias of `AgentDefinition`. Common fields are `name`, `model`,
`instruction`, `description`, `tools`, `subagents`, `mcp`, `sandbox`,
`output_key`, and `generate_content_config`. Prefer filesystem composition over
manually populating discovered resources.

`Graph` nodes may be `Agent` definitions, typed callables, nested `Graph`
objects, `Join()` nodes, accepted backend-native nodes, or strings naming
filesystem-discovered tools and subagents. String references are how a graph
uses a sibling resource without importing it. `Edge` sources use `START` for
entry. A callable can return a plain value or `Event(output=..., route=...,
message=...)`; routed edges set `route=`. Every node must be reachable.

## Tools

```python
from harnest.tool import tool


@tool
def lookup_ticket(ticket_id: str) -> str:
    """Return the current ticket summary."""
    return repository.lookup(ticket_id)
```

Place this in `tools/lookup_ticket.py`; the callable and file stem must match.
A tool needs a docstring or `@tool(description="...")`. The decorator keeps the
function directly callable for unit tests.

## Models

`harnest.model.LiteLLMModel(provider/model, **completion_args)` is the default
provider-neutral connector. `OllamaModel` is an optional convenience that still
routes through LiteLLM. Pass provider options such as `api_base` and `api_key`
through the connector or runtime environment. Managed ADK and LangGraph both
resolve a `ModelConnector` lazily without contacting the model during compile.

## MCP clients

```python
import os

from harnest.mcp import MCPClient


knowledge = (
    MCPClient.streamable_http(
        os.environ["KNOWLEDGE_MCP_URL"],
        headers={"Authorization": "Bearer ${KNOWLEDGE_MCP_TOKEN}"},
        tools=("search",),
        prefix="knowledge",
    )
    if os.getenv("KNOWLEDGE_MCP_URL")
    else None
)
```

Available constructors are `stdio`, `streamable_http`, and legacy `sse`.
`${ENV_VAR}` placeholders are resolved when connecting, not during discovery.
Use a prefix to prevent tool-name collisions.

## Plugins versus extensions

A plugin is a filesystem bundle of MCP client modules and progressive skills.
There is no Python plugin object and no plugin agent.

An extension changes invocation behavior. Portable lifecycle hooks are
`before_invoke`, `after_invoke`, `on_event`, and `on_error`:

```python
from harnest.extension import Extension


async def after_invoke(context, result):
    await store.write(context.invocation_id, result.text)


extension = Extension(name="conversation_store", after_invoke=after_invoke)
```

The extension name must match its directory. Return `None` from transforming
hooks to keep the value, return a replacement to transform it, and return
`DROP_EVENT` from `on_event` to suppress an output event. Native `adk.py` or
`langgraph.py` integrations may accompany the portable lifecycle.

## Logging, tracing, and sandboxing

```python
from harnest.logging import get_logger

logger = get_logger("tickets", component="support")
logger.info("ticket.loaded", ticket_id=ticket_id)
```

Structured logs correlate with active OpenTelemetry trace/span identifiers.
Never log credentials or raw sensitive conversation content.

Use `harnest.tracing` for authored spans:

```python
from harnest.tracing import get_tracer, span, traced


with span("catalog.lookup", product_id=product_id):
    result = lookup(product_id)


@traced("catalog.refresh")
async def refresh_catalog() -> None:
    ...
```

`span` accepts `attributes=`, optional `kind=`, and structured keyword
attributes. `traced` supports synchronous, asynchronous, generator, and async
generator functions. `get_tracer` exposes a dynamic tracer and
`current_trace_ids()` returns the active W3C identifiers when recording.

`harnest.sandbox.Sandbox.container(...)` and `Sandbox.provider(...)` define lazy
ADK code executors. A sandbox is an execution boundary, not merely a policy
flag; provider packages and Docker requirements belong in `requirements.txt`.

## Advanced applications

Use the same public `Agent` type in advanced mode:

```python
from harnest.agent import Agent


root_agent = Agent.advanced(
    name="support",
    target=compiled_graph,
    input_adapter=to_graph_input,
    output_adapter=from_graph_output,
)
```

Import ADK or LangGraph directly to construct `target`; Harnest does not wrap or
re-export their APIs. ADK targets are validated ADK applications, agents, or
nodes. LangGraph targets are compiled `Pregel` applications; conventional
`messages` state needs no adapter, while custom state should define neutral
input/output adapters. Portable `harnest.graph.Graph` construction remains a
managed-mode feature.

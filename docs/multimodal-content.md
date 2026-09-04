# Multimodal Pydantic content

Harnest uses portable Pydantic parts for multimodal agent input, output, and
tool results. Keep the application contracts in the root `models/` folder so
agents, tools, client tools, extensions, and subagents import one definition
through `harnest.models.*`.

```python
# models/vision.py
from typing import Annotated

from pydantic import BaseModel

from harnest.content import Image, ImageConstraints


Screenshot = Annotated[
    Image,
    ImageConstraints(
        media_types=frozenset({"image/jpeg", "image/png"}),
        max_bytes=500 * 1024,
        max_width=1920,
        max_height=1080,
        max_pixels=2_073_600,
        animated=False,
    ),
]


class VisionRequest(BaseModel):
    question: str
    screenshot: Screenshot


class VisionResult(BaseModel):
    answer: str
```

Use the model on the agent boundary:

```python
# agent.py
from harnest.agent import Agent
from harnest.models.vision import VisionRequest, VisionResult
from harnest.model import LiteLLMModel


root_agent = Agent(
    name="vision",
    model=LiteLLMModel("provider/vision-model"),
    input_schema=VisionRequest,
    output_schema=VisionResult,
)
```

The same contracts apply to JSON and SSE requests, `/live` WebSocket frames,
structured responses, and `GET /sessions/{sessionId}/messages` projections.
`@tool(output_schema=...)` and `@client_tool(output_schema=...)` use the same
Pydantic models, including when a client-tool result resumes a subagent in the
middle of a turn.

`Text`, `Image`, `Audio`, `Video`, `File`, `AssetRef`, and typed `Data[T]` are
available from `harnest.content`. Their corresponding `Annotated` constraints
configure accepted MIME types, decoded byte size, dimensions, pixels,
animation, duration, sample rate, channels, pages, and other applicable limits.
These are field contracts, not `server.yaml` settings. Harnest validates
decoded bytes and trusted inspected metadata rather than trusting values
claimed by a client.

## Transient media is the default

With no storage annotation, media is inline base64 for the current turn:

```json
{
  "sessionId": "demo",
  "input": {
    "question": "What is visible?",
    "screenshot": {
      "type": "image",
      "mediaType": "image/jpeg",
      "data": "<base64>"
    }
  }
}
```

Harnest validates and leases the decoded bytes before a framework can persist
the value. The ADK or LangGraph model adapter injects those bytes only into the
immediate model call. This applies to top-level input, ordinary typed tool
output, and mid-turn typed client-tool output, including client tools called by
subagents. A provider retry may reuse the private lease; a successful model
call consumes it, and terminal failure or cancellation clears it.

Transient input and tool-result base64 stays in a private invocation lease.
Neither those bytes nor the lease identifier enters native ADK/LangGraph
checkpoints, session history, logs, traces, audit records, or intermediate
public events. Framework-visible history contains only a non-secret attachment
placeholder. Public streams and `/messages` retain ordering without retaining
the content, for example:

```json
{
  "type": "image",
  "mediaType": "image/jpeg",
  "content": "attached"
}
```

Because transient media is deliberately not durable, it has no later asset to
fetch through `context.assets`.

When inline media is deliberately part of the agent's final `output_schema`,
the authenticated JSON, SSE, or WebSocket response returns that authored data
once. It is not durable or replayable through session messages. Use
`Stored(...)` when output must remain retrievable by reference after the
response.

## Durable media is explicit

Add `Stored(...)` to a media field only when the application needs durable
storage and a later reference:

```python
# models/capture.py
from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel

from harnest import Stored
from harnest.content import Image, ImageConstraints


class CaptureResult(BaseModel):
    screenshot: Annotated[
        Image,
        ImageConstraints(max_bytes=5 * 1024 * 1024),
        Stored(
            store="media",
            path="screenshots",
            expires_in=60,
            retention=timedelta(days=7),
        ),
    ]
```

`store` selects a named root `AssetStorage`. `path` is a storage hint,
`retention` controls the stored bytes, and `expires_in` controls the lifetime
of the model-facing URL; URL lifetime and storage retention are independent.
The durable public value is an opaque, user-and-session-scoped reference with
`assetId` and `store`, never a storage credential or permanent URL.

Configure each storage implementation through the existing extension
lifecycle:

```python
# lifecycle/assets.py
from harnest import AssetStorage, lifecycle
from harnest.lib.assets import S3AssetStorage


@lifecycle.asset_store(name="media")
def media_assets() -> AssetStorage:
    return S3AssetStorage(bucket="agent-media")
```

An inline value accepted by a `Stored(...)` field is inspected and saved to
that named store before it becomes framework-visible. At the immediate model
boundary, a URL-capable storage receives the scoped reference and creates a
fresh signed URL using `expires_in`; signed URLs are not created when the media
is captured and are not persisted in history. The framework adapter then
lowers the URL to the provider's media shape.

An ordinary `@tool` whose output model contains `Stored(...)` must be declared
with `async def`, because Harnest awaits the selected storage before returning
the tool result. Harnest rejects a synchronous declaration instead of silently
changing its call semantics. A `@client_tool` remains a client-executed stub;
Harnest performs its awaited storage pass when the client submits the result.

Managed agent code accesses a stored reference through the invocation-scoped
capability rather than the backend object:

```python
from harnest.context import context


record = await context.assets.stat(result.screenshot)
payload = await context.assets.get(result.screenshot, max_bytes=5 * 1024 * 1024)
async for chunk in context.assets.open(result.screenshot):
    ...
temporary_url = await context.assets.url(result.screenshot, expires_in=60)
```

`context.assets` supplies the current authenticated user and session. It also
offers scoped `delete(...)`. Calls outside an active Harnest invocation fail.
Application code outside that boundary must apply equivalent user-and-session
ownership checks through its own storage API.

Existing clients may also upload directly to
`POST /sessions/{sessionId}/assets` and pass a matching `assetId`/`store`
reference. `GET`, `HEAD`, and `DELETE
/sessions/{sessionId}/assets/{assetId}` remain the neutral asset endpoints for
the default store, including bounded range downloads.

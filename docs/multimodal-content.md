# Multimodal Pydantic content

Harnest represents multimodal input, output, tool results, streams, WebSocket
frames, and session messages with portable Pydantic content parts. Authored
models belong in the root `models/` folder and import as `harnest.models.*`.

```python
# models/conversation.py
from typing import Annotated

from pydantic import BaseModel

from harnest.content import (
    Data,
    DataConstraints,
    Image,
    ImageConstraints,
    Text,
)


SupportImage = Annotated[
    Image,
    ImageConstraints(
        media_types=frozenset({"image/png", "image/jpeg"}),
        max_bytes=5 * 1024 * 1024,
        max_width=4096,
        max_height=4096,
        max_pixels=12_000_000,
        animated=False,
    ),
]
TicketData = Annotated[Data[dict[str, str]], DataConstraints(max_bytes=8192)]


class Request(BaseModel):
    content: list[Text | SupportImage | TicketData]


class Result(BaseModel):
    answer: str
    image: SupportImage | None = None
```

Configure the agent with `input_schema=Request` and `output_schema=Result`.
The same contracts apply to JSON responses, SSE, `/live`, ADK, LangGraph, and
`GET /sessions/{sessionId}/messages`. `@tool(output_schema=Result)` and
`@client_tool(output_schema=Result)` use the same structural Pydantic types.

## Asset references

Media parts contain an opaque `assetId`; they never contain a URL, base64,
provider file ID, or bytes. Upload bytes into an existing session first:

```bash
curl -X POST http://127.0.0.1:8080/sessions/demo/assets \
  -H 'Content-Type: image/png' \
  --data-binary @diagram.png
```

Then send the returned reference:

```json
{
  "sessionId": "demo",
  "input": {
    "content": [
      {"type": "text", "text": "Explain this diagram"},
      {"type": "image", "assetId": "asset_..."}
    ]
  }
}
```

Use `GET`, `HEAD`, or `DELETE
/sessions/{sessionId}/assets/{assetId}` to stream, inspect, or delete an owned
asset. Downloads support a single HTTP byte range. Asset lookup is bound to the
authenticated user and session, and deleting a session removes its assets.

Harnest validates in two stages: Pydantic checks the authored reference shape,
then Harnest reads store-owned MIME, byte size, dimensions, duration, and count
metadata and enforces the field's `Annotated` constraints. Client-claimed
metadata is discarded. Fixed internal abuse ceilings remain above authored
constraints and are not configured in `server.yaml`.

ADK and LangGraph sessions/checkpoints keep only references. Harnest loads bytes
inside the framework's model-call boundary, and stages inspected inline model
output into the asset store before persistence. Session messages expose ordered
portable parts. Harnest traces and audit logs record only counts, kinds, and
outcomes—not bytes, URLs, filenames, captions, custom data, or asset IDs.

For a custom durable store, return an `AssetStore` from one root
`@lifecycle.asset_store` factory. The default `MemoryAssetStore` is bounded and
development-only.

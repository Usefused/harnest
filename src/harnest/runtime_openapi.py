"""Discoverable request examples without changing runtime validation or limits."""

from typing import Any

from pydantic import BaseModel


def openapi_resources() -> list[dict[str, str]]:
    """Describe HTTP spec resources that calling agents can fetch without UI parsing."""

    return [
        {"name": "openapi.json", "uri": "/openapi.json", "mimeType": "application/json"},
        {"name": "openapi.yaml", "uri": "/openapi.yaml", "mimeType": "application/yaml"},
    ]


def configure_openapi(app: Any, *, enabled: bool) -> None:
    """Apply the same exposure policy to neutral and native-framework applications."""

    if not isinstance(enabled, bool):
        raise TypeError("openapi_enabled must be boolean")
    app.state.openapi_enabled = enabled
    app.openapi_schema = None
    if not enabled:
        # Remove the framework-created routes, not just their navigation links.
        paths = {app.openapi_url, app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url, "/openapi.yaml"} - {None}
        app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) not in paths]
        app.openapi_url = app.docs_url = app.redoc_url = None
        return

    from starlette.responses import Response
    import yaml

    @app.get("/openapi.yaml", include_in_schema=False)
    async def openapi_yaml() -> Response:
        """Serialize the same cached document as JSON, including custom routes."""

        return Response(
            yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True),
            media_type="application/yaml",
            headers={"Content-Disposition": 'inline; filename="openapi.yaml"'},
        )


def announce_openapi(app: Any, *, host: str, port: int) -> None:
    """Print usable spec URLs using the actual listener configuration, never agent-card guesses."""

    if not getattr(getattr(app, "state", None), "openapi_enabled", False):
        return
    # Wildcard binds are listener addresses, not useful client destinations.
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    authority = f"[{host}]" if ":" in host else host
    origin = f"http://{authority}:{port}"
    print(f"Swagger UI: {origin}/docs", flush=True)
    for resource in openapi_resources():
        print(f"OpenAPI resource ({resource['mimeType']}): {origin}{resource['uri']}", flush=True)


API_DESCRIPTION = """Invoke this agent with `POST /responses` and a JSON body.
For text agents, start with `{"input":"Hello!"}`. Typed agents require the
object described by the operation's `input` schema. Omit `sessionId` for a new
conversation; reuse the returned `sessionId` for follow-up turns. Set `stream`
to `true` for SSE. Read `outputText` for the answer and `status` for completion
or pending work. Authentication is deployment-specific; use the credentials
provided by the agent owner.

Discover routes at `/agent` and download this schema at `/openapi.json`.
[Curl examples and API guide](https://docs.usefused.com/harnest/runtime/serving/neutral-api).
"""


def response_operation(model: type[BaseModel], *, text_input: bool) -> dict[str, Any]:
    """Describe the manually validated body, preserving authored input schemas."""

    # These definitions are nested in the operation, not at the document root.
    # Absolute JSON pointers let Swagger and other OpenAPI clients resolve them.
    schema = model.model_json_schema(
        by_alias=True,
        ref_template="#/paths/~1responses/post/requestBody/content/application~1json/schema/$defs/{model}",
    )
    body: dict[str, Any] = {"schema": schema}
    if text_input:
        body["examples"] = {
            "newConversation": {"summary": "Start a conversation", "value": {"input": "Hello! What can you help me with?"}},
            "stream": {"summary": "Stream a new conversation", "value": {"input": "Hello!", "stream": True}},
            "followUp": {"summary": "Reuse a returned session ID", "value": {"input": "Tell me more.", "sessionId": "REPLACE_WITH_RETURNED_SESSION_ID"}},
        }
    description = API_DESCRIPTION
    if text_input:
        description += """
Copyable local request (replace the origin for a different host or port):
```bash
curl --fail-with-body -sS http://127.0.0.1:1907/responses \\
  -H 'Content-Type: application/json' \\
  -d '{"input":"Hello!"}'
```
For streaming, add `-N` and send `{"input":"Hello!","stream":true}`.
"""
    return {
        "summary": "Send a message to the agent",
        "operation_id": "createAgentResponse",
        "tags": ["Responses"],
        "description": description,
        "openapi_extra": {"requestBody": {"required": True, "content": {"application/json": body}}},
        "responses": {
            200: {
                "description": "Agent response, or SSE frames when stream is true. Inspect status: a successful HTTP request can still require approval, client-tool output, or polling.",
                "content": {
                    "application/json": {"schema": _response_schema()},
                    "text/event-stream": {"schema": {"type": "string"}, "example": "event: response.text.delta\ndata: {\"delta\":\"Hello!\"}\n\n"},
                },
            },
            400: {"description": "Invalid request body or input."},
            404: {"description": "Session does not exist or is not accessible to the caller."},
            413: {"description": "Request exceeds the configured size limit."},
        },
    }


def _response_schema() -> dict[str, Any]:
    """Document the shared envelope without constraining authored output values."""

    return {
        "type": "object",
        "required": ["id", "sessionId", "status", "outputText", "output", "metadata"],
        "properties": {
            "id": {"type": "string", "description": "Response ID for GET /responses/{id}?sessionId=..."},
            "sessionId": {"type": "string", "description": "Reuse in subsequent POST /responses requests."},
            "status": {"type": "string", "description": "Execution state, such as completed, in_progress, requires_action, failed, or cancelled."},
            "outputText": {"type": "string", "description": "Assistant answer text; excludes reasoning."},
            "output": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "metadata": {"type": "object", "additionalProperties": True},
            "result": {"description": "Optional authored structured output."},
            "requiredAction": {"type": "object", "additionalProperties": True},
            "usage": {"type": "object", "additionalProperties": True},
        },
    }

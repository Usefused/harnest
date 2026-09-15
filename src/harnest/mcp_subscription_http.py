"""Resource invalidations over the shared stateless MCP HTTP transport."""

from typing import Any
from uuid import uuid4

from .mcp_http_transport import _call, _client, _messages, _request, request_headers
from .mcp_resources import MCPResourceError

_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"


async def listen(listener: Any) -> None:
    """Acknowledge the selected URI, recover its retained value, then follow invalidations."""

    configured, subscription = listener.configured, listener.subscription
    async with _client(configured, listener.framework) as (client, url):
        discovered = await _call(client, url, "server/discover", {}, configured.max_content_bytes)
        if not discovered.get("capabilities", {}).get("resources", {}).get("subscribe"):
            raise MCPResourceError("MCP server does not advertise resource subscriptions")
        request_id = uuid4().hex
        request = _request("subscriptions/listen", {"notifications": {"resourceSubscriptions": [subscription.uri]}}, request_id)
        async with client.stream("POST", url, json=request, headers=request_headers("subscriptions/listen", {}), follow_redirects=False) as response:
            messages = _messages(response, configured.max_content_bytes)
            acknowledgment = await anext(messages)
            _acknowledged(acknowledgment, request_id, subscription.uri)

            async def read() -> dict[str, Any]:
                """Fetch through MCP; never follow a resource URI as a network address."""

                return await _call(client, url, "resources/read", {"uri": subscription.uri}, configured.max_content_bytes)

            await listener.connected(read)
            async for message in messages:
                if _updated(message, request_id, subscription.uri):
                    await listener.deliver(read, "update")


def _acknowledged(message: dict[str, Any], request_id: str, uri: str) -> None:
    """Reject partial subscription acceptance instead of silently losing notifications."""

    params = message.get("params", {})
    valid = (
        message.get("method") == "notifications/subscriptions/acknowledged"
        and params.get("_meta", {}).get(_SUBSCRIPTION_ID) == request_id
        and uri in params.get("notifications", {}).get("resourceSubscriptions", [])
    )
    if not valid:
        raise MCPResourceError("MCP server did not acknowledge the requested resource")


def _updated(message: dict[str, Any], request_id: str, uri: str) -> bool:
    """Ignore unrelated or incorrectly correlated notifications."""

    params = message.get("params", {})
    return (
        message.get("method") == "notifications/resources/updated"
        and params.get("_meta", {}).get(_SUBSCRIPTION_ID) == request_id
        and params.get("uri") == uri
    )

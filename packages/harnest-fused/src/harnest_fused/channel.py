"""Generic Fused-backed channel adapter: any Fused-connected service, one class.

Registers under the extension name ``fused``. A binding supplies only data (a
field-path mapping and a reply operation); this module never grows a new class
per platform. Inbound normalization is offline; outbound sends go through the
same governed MCP transport as any other Fused connection, but as one narrowly
selected, non-model-selected call, never through model tool-use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from harnest.channels import ChannelAdapter, ChannelError, register_channel_adapter
from harnest.channel_storage import ChannelEvent, ChannelReply

from .client import FusedMCPClient


def _lookup(payload: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted field path through nested mappings without raising on absence."""

    value: Any = payload
    for key in path.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


@dataclass(frozen=True, slots=True)
class FusedEventMapping:
    """Declarative field paths translating one service's raw payload into a ChannelEvent.

    Every path is dotted and resolved against the raw provider payload. Paths,
    not code, are what let one adapter work for any Fused-connected service.
    """

    provider_event_id: str
    kind: str
    sender_id: str
    conversation_id: str
    message_id: str
    occurred_at: str
    content: str | None = None
    thread_id: str | None = None
    delivery_id: str | None = None

    def apply(self, *, platform: str, installation_id: str, raw: Mapping[str, Any]) -> ChannelEvent:
        """Build one ChannelEvent by resolving every declared path against raw."""

        def require(path: str, name: str) -> Any:
            value = _lookup(raw, path)
            if value is None:
                raise ChannelError(f"fused channel payload is missing {name!r} at path {path!r}")
            return value

        thread = _lookup(raw, self.thread_id) if self.thread_id else None
        delivery = _lookup(raw, self.delivery_id) if self.delivery_id else None
        return ChannelEvent(
            platform=platform, installation_id=installation_id,
            provider_event_id=str(require(self.provider_event_id, "provider_event_id")),
            kind=str(require(self.kind, "kind")),
            sender_id=str(require(self.sender_id, "sender_id")),
            conversation_id=str(require(self.conversation_id, "conversation_id")),
            message_id=str(require(self.message_id, "message_id")),
            occurred_at=float(require(self.occurred_at, "occurred_at")),
            content=str(_lookup(raw, self.content) or "") if self.content else "",
            thread_id=str(thread) if thread is not None else None,
            delivery_id=str(delivery) if delivery is not None else "",
        )


@dataclass(frozen=True, slots=True)
class FusedChannelConfig:
    """Everything one Fused-backed channel binding needs, expressed only as data.

    ``client`` is the same generic FusedMCPClient used for outbound tool calls;
    ``reply_operation`` is the MCP tool this adapter calls to deliver a reply, and
    ``reply_arguments`` maps its argument names to either a literal string or a
    ``ChannelReply`` field reference spelled ``$field_name``.
    """

    platform: str
    installation_id: str
    mapping: FusedEventMapping
    client: FusedMCPClient
    reply_operation: str
    reply_arguments: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.platform or not self.installation_id or not self.reply_operation:
            raise ValueError("FusedChannelConfig requires platform, installation_id, and reply_operation")


class FusedChannelAdapter(ChannelAdapter):
    """Normalize and reply through any Fused-connected service via one MCP client.

    Instantiated per binding from a FusedChannelConfig, so the platform, event
    shape, and reply operation are all data: this class never changes.
    """

    def __init__(self, config: FusedChannelConfig) -> None:
        self.platform = config.platform
        self._config = config

    def normalize_event(self, raw: Mapping[str, Any]) -> ChannelEvent:
        """Translate one raw provider payload using this binding's field mapping."""

        return self._config.mapping.apply(
            platform=self.platform, installation_id=self._config.installation_id, raw=raw,
        )

    async def send_reply(self, reply: ChannelReply) -> Mapping[str, Any]:
        """Call the configured Fused operation directly; never through model tool-use."""

        from harnest.mcp_resources import resource_session

        arguments = {key: _reply_argument(reply, template)
                     for key, template in self._config.reply_arguments.items()}
        async with resource_session(self._config.client, framework="langgraph") as (session, _initialized):
            result = await session.call_tool(self._config.reply_operation, arguments)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _reply_argument(reply: ChannelReply, template: str) -> Any:
    """Resolve one reply-argument template: a `$field` reference or a literal."""

    if template.startswith("$"):
        return getattr(reply, template[1:])
    return template


def _adapter_from_config(raw_config: Mapping[str, Any]) -> FusedChannelAdapter:
    """Build one adapter instance from a ChannelBinding's plain-data config.

    Every field is data (URL/token env references, dotted mapping paths, a
    tool name, and argument templates): connecting a new service to Fused
    never requires writing a new Python class.
    """

    mapping = FusedEventMapping(**raw_config["mapping"])
    client = FusedMCPClient.streamable_http(
        raw_config["url"], headers=dict(raw_config.get("headers", {})),
    )
    config = FusedChannelConfig(
        platform=raw_config["platform"], installation_id=raw_config["installation_id"],
        mapping=mapping, client=client, reply_operation=raw_config["reply_operation"],
        reply_arguments=dict(raw_config.get("reply_arguments", {})),
    )
    return FusedChannelAdapter(config)


register_channel_adapter("fused", _adapter_from_config)


__all__ = ["FusedChannelAdapter", "FusedChannelConfig", "FusedEventMapping"]

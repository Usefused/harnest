"""Declarative multi-service Fused MCP clients and explicit provisioning."""

from .client import FusedMCPClient, OpenAPISpec
from .provision import FusedSetupResult
from ._cli import FusedCLIError
from .channel_reply import FusedSlackReplySender
from .channel_sdk import FusedChannelReceiver, slack_mention
from . import channel as _channel  # registers the "fused" channel adapter extension

FusedChannelAdapter = _channel.FusedChannelAdapter
FusedChannelConfig = _channel.FusedChannelConfig
FusedEventMapping = _channel.FusedEventMapping

__all__ = [
    "FusedCLIError",
    "FusedChannelReceiver",
    "FusedSlackReplySender",
    "slack_mention",
    "FusedChannelAdapter",
    "FusedChannelConfig",
    "FusedEventMapping",
    "FusedMCPClient",
    "FusedSetupResult",
    "OpenAPISpec",
]

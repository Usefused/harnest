"""Declarative multi-service Fused MCP clients and explicit provisioning."""

from .client import FusedMCPClient, OpenAPISpec
from .provision import FusedSetupResult
from ._cli import FusedCLIError

__all__ = ["FusedCLIError", "FusedMCPClient", "FusedSetupResult", "OpenAPISpec"]

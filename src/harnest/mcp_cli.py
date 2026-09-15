"""Developer inspection of authored MCP connections, without starting an agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


def add_mcp_parser(subparsers: Any) -> None:
    """Share the connection selection and output contract across CLI operations."""

    parser = subparsers.add_parser("mcp", help="inspect configured MCP resources and prompts")
    parser.add_argument("operation", choices=("inspect", "read", "prompt"))
    parser.add_argument("client")
    parser.add_argument("identifier", nargs="?")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--framework", choices=("adk", "langgraph"), default="langgraph")
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--catalog", choices=("tools", "resources", "resource-templates", "prompts"))
    parser.add_argument("--cursor")


def run_mcp_command(args: Any) -> int:
    """Bind authored helpers for the entire connection lifetime, including cleanup."""

    from ._library import authored_library
    from .bundle import _discover_mcp

    _validate_request(args)
    with authored_library(args.project.resolve()):
        clients = _discover_mcp(args.project.resolve() / "mcp")
        configured = next((item for item in clients if item.identity == args.client), None)
        if configured is None:
            raise ValueError("configured MCP client not found in the agent's mcp/ folder")
        result = asyncio.run(_execute(configured, args))
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0


def _validate_request(args: Any) -> None:
    """Reject ambiguous invocations before importing any authored Python."""

    if (args.operation == "inspect") != (args.identifier is None):
        raise ValueError("inspect accepts only CLIENT; read and prompt require an identifier")
    if args.arg and args.operation != "prompt":
        raise ValueError("--arg is available only for MCP prompts")
    if args.catalog and args.operation != "inspect":
        raise ValueError("--catalog is available only for MCP inspection")
    if args.cursor and not args.catalog:
        raise ValueError("--cursor requires --catalog")
    _prompt_arguments(args.arg)


def _prompt_arguments(values: list[str]) -> dict[str, str]:
    """Preserve equals signs in values while rejecting duplicate or malformed keys."""

    arguments: dict[str, str] = {}
    for value in values:
        key, separator, content = value.partition("=")
        if not separator or not key or key in arguments:
            raise ValueError("each --arg must be a unique non-empty key=value")
        arguments[key] = content
    return arguments


async def _execute(configured: Any, args: Any) -> dict[str, Any]:
    """Use the same transport, credentials, bounds, and allowlists as agent context."""

    async with configured.connect(framework=args.framework) as client:
        if args.operation == "inspect":
            if args.catalog:
                operation = "list_" + args.catalog.replace("-", "_")
                return await getattr(client, operation)(cursor=args.cursor)
            return await client.inspect()
        if args.operation == "read":
            return await client.read_resource(args.identifier)
        return await client.get_prompt(args.identifier, _prompt_arguments(args.arg))

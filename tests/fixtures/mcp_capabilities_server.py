"""Disposable stdio MCP server for protocol and framework integration tests."""

import asyncio
import sys

from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

server = Server("knowledge-fixture")
version = 0
first_read = asyncio.Event()


@server.list_tools()
async def tools():
    return [types.Tool(name="echo", description="Echo text", inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})]


@server.call_tool()
async def call(name, arguments):
    return [types.TextContent(type="text", text=arguments["text"])]


@server.list_resources()
async def resources():
    return [types.Resource(name="handbook", uri="knowledge://handbook", description="Team handbook", mimeType="text/markdown")]


@server.list_resource_templates()
async def templates():
    return [types.ResourceTemplate(name="document", uriTemplate="knowledge://documents/{id}")]


@server.read_resource()
async def read(uri):
    text = "x" * 4096 if str(uri) == "knowledge://large" else f"# Handbook v{version}"
    first_read.set()
    return [ReadResourceContents(text, mime_type="text/markdown")]


@server.list_prompts()
async def prompts():
    return [types.Prompt(name="summarize", description="Summarize a topic", arguments=[types.PromptArgument(name="topic", required=True)])]


@server.get_prompt()
async def prompt(name, arguments):
    return types.GetPromptResult(messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"Summarize {arguments['topic']}"))])


@server.subscribe_resource()
async def subscribe(uri):
    session = server.request_context.session

    async def update():
        global version
        await first_read.wait()
        version += 1
        await session.send_resource_updated(uri)

    asyncio.create_task(update())


async def main():
    if "--resources-only" in sys.argv or "--prompts-only" in sys.argv:
        server.request_handlers.pop(types.ListToolsRequest)
        server.request_handlers.pop(types.CallToolRequest)
    if "--tools-only" in sys.argv or "--prompts-only" in sys.argv:
        for request in (types.ListResourcesRequest, types.ListResourceTemplatesRequest, types.ReadResourceRequest, types.SubscribeRequest):
            server.request_handlers.pop(request, None)
    if "--tools-only" in sys.argv or "--resources-only" in sys.argv:
        server.request_handlers.pop(types.ListPromptsRequest)
        server.request_handlers.pop(types.GetPromptRequest)
    async with stdio_server() as (read, write):
        options = server.create_initialization_options()
        if options.capabilities.resources is not None:
            options.capabilities.resources.subscribe = True
        await server.run(read, write, options)


if __name__ == "__main__":
    asyncio.run(main())

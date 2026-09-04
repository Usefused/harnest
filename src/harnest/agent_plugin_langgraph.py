"""Own native stdio sessions without LangChain's extra environment expansion."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any


class PortableStdioOwner:
    """Keep native MCP task groups inside one task until driver shutdown."""

    def __init__(self, client: Any, name: str, configured: Any) -> None:
        """Retain one adapter's policies and one immutable portable configuration."""
        self._client = client
        self._name = name
        self._configured = configured
        self._ready = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> list[Any]:
        """Discover tools while unwinding partial startup on failure or cancellation."""
        self._task = asyncio.create_task(self._run())
        try:
            return await asyncio.shield(self._ready)
        except BaseException:
            await self.aclose()
            if self._ready.done() and not self._ready.cancelled():
                self._ready.exception()
            raise

    async def _run(self) -> None:
        """Enter and exit AnyIO scopes in the same task, across HTTP requests."""
        try:
            await self._serve()
        except BaseException as error:
            if self._ready.done():
                raise
            self._ready.set_exception(error)

    async def _serve(self) -> None:
        """Pass already-expanded portable values directly to the native MCP SDK."""
        from langchain_mcp_adapters.tools import load_mcp_tools
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        configured = self._configured
        configured.portable.prepare()
        parameters = StdioServerParameters(**configured.portable.stdio(configured))
        # Passing an existing session avoids the adapter's second ${VAR} pass.
        # The adapter still owns tool conversion, error results and interceptors.
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=configured.timeout_seconds),
            ) as session:
                await session.initialize()
                tools = await load_mcp_tools(
                    session, server_name=self._name, tool_name_prefix=True,
                    tool_interceptors=self._client.tool_interceptors,
                    callbacks=self._client.callbacks,
                    handle_tool_errors=self._client.handle_tool_errors,
                )
                self._ready.set_result(tools)
                await self._stop.wait()

    async def aclose(self) -> None:
        """Close the process and protocol session through their owning task."""
        task = self._task
        if task is None:
            return
        self._stop.set()
        if not self._ready.done():
            task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        error = results[0]
        if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError):
            raise error

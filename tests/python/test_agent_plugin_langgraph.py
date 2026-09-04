"""Portable stdio remains literal and closes across runtime task boundaries."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from langchain_mcp_adapters.client import MultiServerMCPClient

from harnest.agent_plugin_langgraph import PortableStdioOwner
from harnest.agent_plugin_runtime import PortableMCP
from harnest.mcp import MCPClient


_SERVER = '''
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

Path(os.environ["PLUGIN_ROOT"], "pid").write_text(str(os.getpid()))
server = FastMCP("literal-proof")

@server.tool()
def proof() -> str:
    """Return exactly the configured environment value."""
    return os.environ["LITERAL"]

server.run(transport="stdio")
'''


class AgentPluginLangGraphTests(unittest.IsolatedAsyncioTestCase):
    """Exercise native session ownership without monkeypatching adapter globals."""

    async def asyncSetUp(self):
        """Give each test an independent package and client-controlled data path."""
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        environment = patch.dict(os.environ, {
            "HARNEST_PLUGIN_DATA_DIR": str(self.root / "data"),
            "HARNEST_PORTABLE_LITERAL_PROOF": "host-value-must-not-expand",
        })
        environment.start()
        self.addCleanup(environment.stop)
        script = self.root / "server.py"
        script.write_text(_SERVER, encoding="utf-8")
        configured = MCPClient(
            transport="stdio", command=sys.executable, args=(str(script),),
            env={"LITERAL": "${HARNEST_PORTABLE_LITERAL_PROOF}"},
            portable=PortableMCP.create(self.root, "starter", "proof"),
        )
        self.owner = PortableStdioOwner(MultiServerMCPClient(), "starter", configured)
        self.addAsyncCleanup(self.owner.aclose)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process liveness check")
    async def test_cross_task_shutdown_reaps_process_and_preserves_literal_env(self):
        """Start, invoke, and close in separate tasks without leaked MCP processes."""
        tools = await asyncio.create_task(self.owner.start())
        self.assertEqual([tool.name for tool in tools], ["starter_proof"])
        result = await asyncio.create_task(tools[0].ainvoke({}))
        self.assertIn("${HARNEST_PORTABLE_LITERAL_PROOF}", str(result))
        self.assertEqual(os.environ["HARNEST_PORTABLE_LITERAL_PROOF"], "host-value-must-not-expand")
        pid = int((self.root / "pid").read_text())
        await asyncio.create_task(self.owner.aclose())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_cancelled_start_unwinds_owning_task(self):
        """A cancelled caller cannot strand a session that is still initializing."""
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def initializing():
            """Model a native session whose initialization never returns."""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        with patch.object(self.owner, "_serve", side_effect=initializing):
            starting = asyncio.create_task(self.owner.start())
            await entered.wait()
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await starting
        self.assertTrue(closed.is_set())
        self.assertTrue(self.owner._task.done())

    async def test_failed_start_is_closed_and_can_be_cleaned_again(self):
        """Repeated driver cleanup must not rethrow an already-contained failure."""
        with patch.object(self.owner, "_serve", side_effect=ValueError("configuration failure")):
            with self.assertRaisesRegex(ValueError, "configuration failure"):
                await self.owner.start()
        await asyncio.create_task(self.owner.aclose())
        self.assertTrue(self.owner._task.done())

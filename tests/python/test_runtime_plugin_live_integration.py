import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import harnest.plugins as plugin_namespace
from harnest.bundle import compile_artifact
from harnest.plugins import PluginContextUnavailableError
from harnest.runtime import create_fastapi_app

from _session_store_fixture import write_session_store


LANGGRAPH_AVAILABLE = importlib.util.find_spec("langgraph") is not None
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
MCP_ADAPTER_AVAILABLE = (
    importlib.util.find_spec("langchain_mcp_adapters") is not None
)


@unittest.skipUnless(
    LANGGRAPH_AVAILABLE and MCP_AVAILABLE and MCP_ADAPTER_AVAILABLE,
    "LangGraph and its MCP runtime dependencies are required",
)
class RuntimePluginLiveIntegrationTests(unittest.TestCase):
    def _write(self, path: Path, value: str) -> None:
        """Write one dedented authored resource into the temporary application."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")

    def _write_root(self, root: Path) -> None:
        """Create a deterministic model that invokes the plugin-provided tool."""

        self._write(
            root / "agent.py",
            """
            from langchain_core.language_models.chat_models import BaseChatModel
            from langchain_core.messages import AIMessage
            from langchain_core.outputs import ChatGeneration, ChatResult

            from harnest.agent import Agent


            class ProbeModel(BaseChatModel):
                @property
                def _llm_type(self):
                    return "runtime-plugin-probe"

                def bind_tools(self, tools, **kwargs):
                    del kwargs
                    names = {tool.name for tool in tools}
                    if "plugin_probe" not in names:
                        raise RuntimeError("plugin tool was not materialized")
                    return self

                def _generate(
                    self, messages, stop=None, run_manager=None, **kwargs
                ):
                    del stop, run_manager, kwargs
                    if getattr(messages[-1], "type", None) == "tool":
                        response = AIMessage(
                            content=f"completed:{messages[-1].content}"
                        )
                    else:
                        response = AIMessage(
                            content="",
                            tool_calls=[{
                                "name": "plugin_probe",
                                "args": {"value": "hello"},
                                "id": "plugin-probe-call",
                                "type": "tool_call",
                            }],
                        )
                    return ChatResult(
                        generations=[ChatGeneration(message=response)]
                    )


            root_agent = Agent(name="plugin_root", model=ProbeModel())
            """,
        )
        self._write(
            root / "instructions.md",
            "Invoke plugin_probe and return its result.\n",
        )
        self._write(
            root / "agent-card.yaml",
            """
            name: Runtime plugin probe
            description: Executes a local plugin and MCP transport.
            version: 0.1.0
            """,
        )
        write_session_store(root)

    def _write_plugin(self, root: Path, journal: Path) -> None:
        """Author a runtime plugin spanning startup, context, tool, and MCP hooks."""

        plugin = root / "plugins" / "temporal"
        self._write(
            plugin / "plugin.yaml",
            """
            apiVersion: harnest.dev/v1alpha1
            kind: RuntimePlugin
            metadata:
              name: temporal
              version: 1.0.0
            runtime:
              entrypoint: plugin:plugin
            capabilities:
              - content.tools
              - content.mcp
              - context.mcp
              - lifecycle.agent
              - lifecycle.mcp
            """,
        )
        self._write_plugin_singleton(plugin, journal)
        self._write_plugin_tool(plugin, journal)
        self._write_plugin_mcp(plugin)
        self._write_plugin_hooks(plugin, journal)
        self._write_mcp_server(plugin, journal)

    def _write_plugin_singleton(self, plugin: Path, journal: Path) -> None:
        """Create the plugin singleton and a typed per-invocation context."""

        self._write(
            plugin / "plugin.py",
            f"""
            from pathlib import Path

            from harnest.plugins import Plugin, PluginContext


            _JOURNAL = Path({str(journal)!r})


            def _record(value):
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(value + "\\n")


            class TemporalContext(PluginContext):
                __slots__ = ("serial",)

                def __init__(self, plugin_name, serial):
                    super().__init__(plugin_name)
                    self.serial = serial


            class TemporalPlugin(Plugin[TemporalContext]):
                def __init__(self):
                    self.serial = 0
                    self.started = False

                async def start(self, context):
                    self.started = True
                    _record(f"plugin:start:{{context.framework}}")

                async def stop(self):
                    _record("plugin:stop")
                    self.started = False

                def create_context(self, base):
                    if not self.started:
                        raise RuntimeError("plugin is not started")
                    self.serial += 1
                    return TemporalContext(base.plugin_name, self.serial)


            plugin = TemporalPlugin()
            """,
        )

    def _write_plugin_tool(self, plugin: Path, journal: Path) -> None:
        """Route a plugin tool through both typed plugin and governed MCP contexts."""

        self._write(
            plugin / "tools" / "plugin_probe.py",
            f"""
            from pathlib import Path

            from harnest.context import context
            from harnest.plugins.temporal import TemporalContext, plugin
            from harnest.tool import tool


            _JOURNAL = Path({str(journal)!r})


            @tool
            async def plugin_probe(value: str) -> str:
                \"\"\"Call the plugin-owned local catalog through managed MCP.\"\"\"

                view = context.plugins("temporal", TemporalContext)
                if plugin.context is not view:
                    raise RuntimeError("plugin context binding diverged")
                remote = await context.mcp("catalog").call_tool(
                    "echo", {{"value": value}}
                )
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(f"tool:{{view.serial}}\\n")
                return f"plugin:{{view.serial}}:{{remote}}"
            """,
        )

    def _write_plugin_mcp(self, plugin: Path) -> None:
        """Configure an artifact-relative stdio server without external networking."""

        self._write(
            plugin / "mcp" / "catalog.py",
            """
            from pathlib import Path
            import sys

            from harnest.mcp import MCPClient


            def client():
                server = Path(__file__).resolve().parents[1] / "lib" / "server.py"
                return MCPClient.stdio(
                    sys.executable,
                    str(server),
                    tools=("echo",),
                    prefix="catalog",
                    timeout_seconds=10,
                )
            """,
        )

    def _write_plugin_hooks(self, plugin: Path, journal: Path) -> None:
        """Record plugin-origin lifecycle execution around the governed MCP call."""

        self._write(
            plugin / "extensions" / "hooks.py",
            f"""
            from pathlib import Path

            from harnest.context import context
            from harnest.lifecycle import lifecycle
            from harnest.plugins.temporal import TemporalContext


            _JOURNAL = Path({str(journal)!r})


            def _record(value):
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(value + "\\n")


            @lifecycle.agent.before
            def before_agent(_scope, _request):
                view = context.plugins("temporal", TemporalContext)
                _record(f"agent:before:{{view.serial}}")


            @lifecycle.mcp.before
            def before_mcp(scope, _request):
                _record(f"mcp:before:{{scope.client_name}}:{{scope.tool_name}}")
                return scope.next()


            @lifecycle.mcp.after
            def after_mcp(scope, result):
                _record(f"mcp:after:{{scope.client_name}}:{{scope.tool_name}}")
                return scope.next(result)
            """,
        )

    def _write_mcp_server(self, plugin: Path, journal: Path) -> None:
        """Create the real local MCP process used by the compiled artifact."""

        self._write(
            plugin / "lib" / "server.py",
            f"""
            from pathlib import Path

            from mcp.server.fastmcp import FastMCP


            _JOURNAL = Path({str(journal)!r})
            server = FastMCP("catalog")


            @server.tool()
            def echo(value: str) -> str:
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(f"server:echo:{{value}}\\n")
                return f"remote:{{value}}"


            if __name__ == "__main__":
                server.run(transport="stdio")
            """,
        )

    def test_compiled_langgraph_runtime_plugin_executes_over_http_and_stdio(self):
        """Execute the full compiled runtime and prove every live ownership stage."""

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "source"
            artifact = workspace / "artifact"
            journal = workspace / "runtime-journal.txt"
            self._write_root(root)
            self._write_plugin(root, journal)

            compile_artifact(root, artifact, framework="langgraph")
            self.assertFalse(journal.exists())
            self.assertNotIn("harnest.plugins.temporal", sys.modules)

            app = create_fastapi_app(artifact, playground_enabled=False)
            self.assertTrue(hasattr(plugin_namespace, "temporal"))
            plugin = plugin_namespace.temporal.plugin
            self.assertFalse(plugin.started)

            with TestClient(app) as client:
                self.assertTrue(plugin.started)
                self.assertEqual(
                    journal.read_text(encoding="utf-8").splitlines(),
                    ["plugin:start:langgraph"],
                )
                with self.assertRaises(PluginContextUnavailableError):
                    _ = plugin.context

                session = client.post("/sessions", json={})
                self.assertEqual(session.status_code, 201, session.text)
                with self.assertLogs(
                    "harnest.agent.mcp.audit", level="INFO"
                ) as captured:
                    response = client.post(
                        "/responses",
                        json={
                            "input": "probe",
                            "sessionId": session.json()["id"],
                        },
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("completed:plugin:1:", response.json()["outputText"])
                self.assertEqual(captured.records[0].outcome, "committed")
                self.assertEqual(
                    captured.records[0].client,
                    "plugin__temporal__mcp__catalog",
                )
                self.assertNotIn(
                    "hello", repr(captured.records[0].__dict__)
                )

                live_lines = journal.read_text(encoding="utf-8").splitlines()
                self.assertIn("agent:before:1", live_lines)
                self.assertIn(
                    "mcp:before:catalog:echo",
                    live_lines,
                )
                self.assertIn("server:echo:hello", live_lines)
                self.assertIn(
                    "mcp:after:catalog:echo",
                    live_lines,
                )
                self.assertIn("tool:1", live_lines)

            self.assertFalse(plugin.started)
            self.assertEqual(
                journal.read_text(encoding="utf-8").splitlines()[-1],
                "plugin:stop",
            )
            self.assertNotIn("harnest.plugins.temporal", sys.modules)
            self.assertFalse(hasattr(plugin_namespace, "temporal"))


if __name__ == "__main__":
    unittest.main()

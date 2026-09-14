import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import harnest.extensions as extension_namespace
from harnest.bundle import compile_artifact
from harnest.extensions import ExtensionContextUnavailableError
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
class HarnestExtensionLiveIntegrationTests(unittest.TestCase):
    def _write(self, path: Path, value: str) -> None:
        """Write one dedented authored resource into the temporary application."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(value).lstrip(), encoding="utf-8")

    def _write_root(self, root: Path) -> None:
        """Create a deterministic model that invokes the extension-provided tool."""

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
                    return "runtime-extension-probe"

                def bind_tools(self, tools, **kwargs):
                    del kwargs
                    names = {tool.name for tool in tools}
                    if "extension_probe" not in names:
                        raise RuntimeError("extension tool was not materialized")
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
                                "name": "extension_probe",
                                "args": {"value": "hello"},
                                "id": "extension-probe-call",
                                "type": "tool_call",
                            }],
                        )
                    return ChatResult(
                        generations=[ChatGeneration(message=response)]
                    )


            root_agent = Agent(name="extension_root", model=ProbeModel())
            """,
        )
        self._write(
            root / "instructions.md",
            "Invoke extension_probe and return its result.\n",
        )
        self._write(
            root / "agent-card.yaml",
            """
            name: Runtime extension probe
            description: Executes a local extension and MCP transport.
            version: 0.1.0
            """,
        )
        write_session_store(root)

    def _write_extension(self, root: Path, journal: Path) -> None:
        """Author a runtime extension spanning startup, context, tool, and MCP hooks."""

        extension = root / "extensions" / "temporal"
        self._write(
            extension / "extension.yaml",
            """
            apiVersion: harnest.dev/v1alpha1
            kind: Extension
            metadata:
              name: temporal
              version: 1.0.0
            runtime:
              entrypoint: extension:extension
            contributes:
              lifecycle: [lifecycle/]
              mcp: [mcp/]
              tools: [tools/]
            capabilities:
              - content.tools
              - content.mcp
              - context.mcp
              - lifecycle.agent
              - lifecycle.mcp
            """,
        )
        self._write_extension_singleton(extension, journal)
        self._write_extension_tool(extension, journal)
        self._write_extension_mcp(extension)
        self._write_extension_hooks(extension, journal)
        self._write_mcp_server(extension, journal)

    def _write_extension_singleton(self, extension: Path, journal: Path) -> None:
        """Create the extension singleton and a typed per-invocation context."""

        self._write(
            extension / "extension.py",
            f"""
            from pathlib import Path

            from harnest.extensions import Extension, ExtensionContext


            _JOURNAL = Path({str(journal)!r})


            def _record(value):
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(value + "\\n")


            class TemporalContext(ExtensionContext):
                __slots__ = ("serial",)

                def __init__(self, extension_name, serial):
                    super().__init__(extension_name)
                    self.serial = serial


            class TemporalExtension(Extension[TemporalContext]):
                def __init__(self):
                    self.serial = 0
                    self.started = False

                async def start(self, context):
                    self.started = True
                    _record(f"extension:start:{{context.framework}}")

                async def stop(self):
                    _record("extension:stop")
                    self.started = False

                def create_context(self, base):
                    if not self.started:
                        raise RuntimeError("extension is not started")
                    self.serial += 1
                    return TemporalContext(base.extension_name, self.serial)


            extension = TemporalExtension()
            """,
        )

    def _write_extension_tool(self, extension: Path, journal: Path) -> None:
        """Route a extension tool through both typed extension and governed MCP contexts."""

        self._write(
            extension / "tools" / "extension_probe.py",
            f"""
            from pathlib import Path

            from harnest import context
            from harnest.extensions.temporal import TemporalContext, extension
            from harnest.agent import tool


            _JOURNAL = Path({str(journal)!r})


            @tool
            async def extension_probe(value: str) -> str:
                \"\"\"Call the extension-owned local catalog through managed MCP.\"\"\"

                view = context.extensions("temporal", TemporalContext)
                if extension.context is not view:
                    raise RuntimeError("extension context binding diverged")
                remote = await context.mcp("catalog").call_tool(
                    "echo", {{"value": value}}
                )
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(f"tool:{{view.serial}}\\n")
                return f"extension:{{view.serial}}:{{remote}}"
            """,
        )

    def _write_extension_mcp(self, extension: Path) -> None:
        """Configure an artifact-relative stdio server without external networking."""

        self._write(
            extension / "mcp" / "catalog.py",
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

    def _write_extension_hooks(self, extension: Path, journal: Path) -> None:
        """Record extension-origin lifecycle execution around the governed MCP call."""

        self._write(
            extension / "lifecycle" / "hooks.py",
            f"""
            from pathlib import Path

            from harnest import context
            from harnest import lifecycle
            from harnest.extensions.temporal import TemporalContext


            _JOURNAL = Path({str(journal)!r})


            def _record(value):
                with _JOURNAL.open("a", encoding="utf-8") as stream:
                    stream.write(value + "\\n")


            @lifecycle.agent.before
            def before_agent(_scope, _request):
                view = context.extensions("temporal", TemporalContext)
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

    def _write_mcp_server(self, extension: Path, journal: Path) -> None:
        """Create the real local MCP process used by the compiled artifact."""

        self._write(
            extension / "lib" / "server.py",
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

    def test_compiled_langgraph_runtime_extension_executes_over_http_and_stdio(self):
        """Execute the full compiled runtime and prove every live ownership stage."""

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "source"
            artifact = workspace / "artifact"
            journal = workspace / "runtime-journal.txt"
            self._write_root(root)
            self._write_extension(root, journal)

            compile_artifact(root, artifact, framework="langgraph")
            self.assertFalse(journal.exists())
            self.assertNotIn("harnest.extensions.temporal", sys.modules)

            app = create_fastapi_app(artifact, playground_enabled=False)
            self.assertTrue(hasattr(extension_namespace, "temporal"))
            extension = extension_namespace.temporal.extension
            self.assertFalse(extension.started)

            with TestClient(app) as client:
                self.assertTrue(extension.started)
                self.assertEqual(
                    journal.read_text(encoding="utf-8").splitlines(),
                    ["extension:start:langgraph"],
                )
                with self.assertRaises(ExtensionContextUnavailableError):
                    _ = extension.context

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
                self.assertIn("completed:extension:1:", response.json()["outputText"])
                self.assertEqual(captured.records[0].outcome, "committed")
                self.assertEqual(
                    captured.records[0].client,
                    "extension__temporal__mcp__catalog",
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

            self.assertFalse(extension.started)
            self.assertEqual(
                journal.read_text(encoding="utf-8").splitlines()[-1],
                "extension:stop",
            )
            self.assertNotIn("harnest.extensions.temporal", sys.modules)
            self.assertFalse(hasattr(extension_namespace, "temporal"))


if __name__ == "__main__":
    unittest.main()

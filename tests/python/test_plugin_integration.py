import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harnest.agent import AgentDefinition
from harnest.bundle import (
    BundleConventionError,
    BundleDuplicateError,
    BundleExportError,
    compile_application,
)
from harnest.graph import Graph
from _session_store_fixture import write_session_store


class PluginIntegrationTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    @staticmethod
    def _backend() -> SimpleNamespace:
        return SimpleNamespace(
            lower_managed=lambda value, **_kwargs: value,
            wrap_managed=lambda target, native_extensions=(): None,
        )

    def _root_agent(self, root: Path) -> None:
        write_session_store(root)
        self._write(
            root / "agent.py",
            "from harnest.agent import Agent\n"
            "root_agent = Agent(name='root', model='test/model')\n",
        )
        self._write(root / "instructions.md", "Use available capabilities.\n")

    def _plugin_mcp(self, root: Path, *, url: str = "https://mcp.example/mcp") -> None:
        self._write(
            root / "plugins" / "support" / "mcp" / "support.py",
            "from harnest.mcp import MCPClient\n"
            "def client():\n"
            f"    return MCPClient.streamable_http({url!r}, prefix='support')\n",
        )

    def _plugin_skill(self, root: Path, name: str = "support-guide") -> None:
        self._write(
            root / "plugins" / "support" / "skills" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: Explain how to use support MCP tools.\n"
            "---\n\n# Support MCP\nUse the support tools only when needed.\n",
        )

    def test_agent_receives_plugin_mcp_and_skill_for_both_frameworks(self):
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._root_agent(root)
                self._plugin_mcp(root)
                self._plugin_skill(root)

                with patch("harnest.bundle.get_backend", return_value=self._backend()):
                    compiled = compile_application(
                        root, entrypoint="agent:root_agent", framework=framework
                    )

                self.assertEqual(len(compiled.target.mcp), 1)
                self.assertEqual(compiled.target.mcp[0].tool_name_prefix, "support")
                self.assertEqual(
                    compiled.target.mcp[0].capability_id,
                    "plugin__support__mcp__support",
                )
                if framework == "langgraph":
                    tools = {tool.__name__: tool for tool in compiled.target.tools}
                    self.assertEqual(
                        json.loads(tools["list_skills"]())["skills"],
                        ["support-guide"],
                    )

    def test_graph_agent_node_receives_plugin_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from harnest.graph import START, Edge, Graph\n"
                "root_agent = Graph(name='root', nodes={"
                "'worker': Agent(name='worker', model='test/model')}, "
                "edges=(Edge(START, 'worker'),))\n",
            )
            self._write(root / "instructions.md", "Use the plugin carefully.\n")
            write_session_store(root)
            self._plugin_mcp(root)
            self._plugin_skill(root)
            write_session_store(root)

            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                compiled = compile_application(
                    root, entrypoint="agent:root_agent", framework="langgraph"
                )

            self.assertIsInstance(compiled.target, Graph)
            worker = compiled.target.nodes["worker"]
            self.assertIsInstance(worker, AgentDefinition)
            self.assertEqual([client.tool_name_prefix for client in worker.mcp], ["support"])
            tools = {tool.__name__: tool for tool in worker.tools}
            self.assertEqual(
                json.loads(tools["list_skills"]())["skills"], ["support-guide"]
            )

    def test_callable_graph_rejects_plugin_without_agent_consumer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(
                root / "agent.py",
                "from harnest.graph import START, Edge, Graph\n"
                "def respond(value): return value\n"
                "root_agent = Graph(name='root', nodes={'respond': respond}, "
                "edges=(Edge(START, 'respond'),))\n",
            )
            self._plugin_mcp(root)
            self._plugin_skill(root)
            write_session_store(root)

            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(
                    BundleConventionError, "MCP clients but no Agent node"
                ):
                    compile_application(
                        root, entrypoint="agent:root_agent", framework="adk"
                    )

    def test_plugin_mcp_export_contract_is_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            self._write(
                root / "plugins" / "support" / "mcp" / "support.py",
                "def client():\n    return object()\n",
            )
            self._plugin_skill(root)
            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(
                    BundleExportError, "must return MCPClient"
                ):
                    compile_application(
                        root, entrypoint="agent:root_agent", framework="langgraph"
                    )

    def test_duplicate_direct_and_plugin_mcp_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            source = (
                "from harnest.mcp import MCPClient\n"
                "def client():\n"
                "    return MCPClient.streamable_http("
                "'https://mcp.example/mcp', prefix='support')\n"
            )
            self._write(root / "mcp" / "support.py", source)
            self._write(root / "plugins" / "support" / "mcp" / "support.py", source)
            self._plugin_skill(root)

            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(
                    BundleDuplicateError, "duplicate MCP client configuration"
                ):
                    compile_application(
                        root, entrypoint="agent:root_agent", framework="langgraph"
                    )

    def test_duplicate_direct_and_plugin_skill_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._root_agent(root)
            self._write(root / "skills" / "triage" / "SKILL.md", "# Direct\n")
            self._write(
                root / "plugins" / "support" / "skills" / "triage" / "SKILL.md",
                "# Plugin\n",
            )
            self._plugin_mcp(root)

            with patch("harnest.bundle.get_backend", return_value=self._backend()):
                with self.assertRaisesRegex(BundleDuplicateError, "duplicate skill name"):
                    compile_application(
                        root, entrypoint="agent:root_agent", framework="langgraph"
                    )


if __name__ == "__main__":
    unittest.main()

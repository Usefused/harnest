import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.bundle import BundleConventionError, compile_application


class BundleResourceConsumptionTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def test_advanced_mode_rejects_every_populated_discovery_folder(self):
        resources = {
            "tools": "lookup.py",
            "subagents": "helper.py",
            "mcp": "server.py",
            "extensions": "support.py",
            "plugins": "audit.py",
            "sandbox": "sandbox.py",
            "skills": "research/SKILL.md",
            "evals": "smoke.evalset.json",
        }
        for directory, relative in resources.items():
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write(
                    root / "agent.py",
                    "from harnest.agent import Agent\n"
                    "root_agent = Agent.advanced(object())\n",
                )
                self._write(root / directory / relative, "resource\n")
                with self.assertRaisesRegex(
                    BundleConventionError,
                    rf"advanced adk mode.*{directory}/.*wire them into Agent.advanced",
                ):
                    compile_application(
                        root, entrypoint="agent:root_agent", framework="adk", mode="advanced"
                    )

    def test_advanced_mode_allows_empty_and_placeholder_only_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "root_agent = Agent.advanced(object())\n",
            )
            for name in (
                "tools",
                "subagents",
                "mcp",
                "extensions",
                "plugins",
                "sandbox",
                "skills",
                "evals",
            ):
                self._write(root / name / "_README.md", "Optional.\n")
                (root / name / "empty").mkdir()
            # Reaching advanced lowering proves placeholders were skipped.
            target = object()
            backend = SimpleNamespace(
                validate_advanced=lambda value, fallback_name: SimpleNamespace(
                    name="advanced", target=target, native_app=None
                )
            )
            with patch("harnest.bundle.get_backend", return_value=backend):
                result = compile_application(
                    root, entrypoint="agent:root_agent", framework="adk", mode="advanced"
                )
            self.assertIs(result.target, target)

    def test_langgraph_agent_rejects_discovered_subagents(self):
        cases = {
            "subagents/helper.py": (
                "from harnest.agent import Agent\n"
                "helper = Agent(name='helper', model='test/model', instruction='Help.')\n"
            ),
        }
        for relative, source in cases.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write(
                    root / "agent.py",
                    "from harnest.agent import Agent\n"
                    "root_agent = Agent(name='root', model='test/model')\n",
                )
                self._write(root / "instructions.md", "Help clearly.\n")
                self._write(root / relative, source)
                with self.assertRaisesRegex(
                    BundleConventionError,
                    "LangGraph Agent definitions cannot consume discovered subagents",
                ):
                    compile_application(
                        root,
                        entrypoint="agent:root_agent",
                        framework="langgraph",
                    )

    def test_callable_graph_rejects_unconsumed_tools_mcp_skills_and_sandbox(self):
        cases = {
            "tools/lookup.py": (
                "from harnest.tool import tool\n"
                "@tool\n"
                "def lookup(value):\n"
                "    \"\"\"Return the input.\"\"\"\n"
                "    return value\n",
                "does not consume discovered resources.*tool 'lookup'",
            ),
            "mcp/server.py": (
                "from harnest.mcp import MCPClient\n"
                "server = MCPClient.streamable_http('https://mcp.example/mcp')\n",
                "has MCP clients but no Agent node",
            ),
            "skills/research/SKILL.md": (
                "---\nname: research\ndescription: Research carefully.\n---\n\n# Research\n",
                "has skills but no Agent node",
            ),
            "sandbox/sandbox.py": (
                "from harnest.sandbox import Sandbox\n"
                "sandbox = Sandbox.container(image='python:3.12')\n",
                "has sandbox configuration but no Agent node",
            ),
        }
        for relative, (source, expected) in cases.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write(
                    root / "agent.py",
                    "from harnest.graph import START, Edge, Event, Graph\n"
                    "def respond(value):\n"
                    "    return Event(message='ok')\n"
                    "root_agent = Graph(\n"
                    "    name='root', nodes={'respond': respond},\n"
                    "    edges=(Edge(START, 'respond'),),\n"
                    ")\n",
                )
                self._write(root / "instructions.md", "Unused by callable graph.\n")
                self._write(root / relative, source)
                with self.assertRaisesRegex(BundleConventionError, expected):
                    compile_application(
                        root, entrypoint="agent:root_agent", framework="adk"
                    )

    def test_langgraph_rejects_adk_eval_files_before_lowering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='test/model')\n",
            )
            self._write(root / "instructions.md", "Help clearly.\n")
            self._write(root / "evals" / "smoke.evalset.json", "{}\n")
            with self.assertRaisesRegex(
                BundleConventionError, "evals/.*cannot be compiled.*LangGraph"
            ):
                compile_application(
                    root,
                    entrypoint="agent:root_agent",
                    framework="langgraph",
                )

if __name__ == "__main__":
    unittest.main()

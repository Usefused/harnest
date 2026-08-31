import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from harnest.agent import AgentDefinition
from harnest.backends import get_backend
from harnest.bundle import (
    BundleConventionError,
    BundleExportError,
    BundleSkillError,
    _discover_subagents,
    compile_application,
)
from harnest.graph import Graph
from _session_store_fixture import write_session_store


class BundleResourceConsumptionTests(unittest.TestCase):
    def _write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    @staticmethod
    def _backend() -> SimpleNamespace:
        return SimpleNamespace(
            lower_managed=lambda value, **_kwargs: value,
            wrap_managed=lambda target, native_extensions=(): None,
        )

    def _write_scoped_graph(self, root: Path, *, sandbox: bool) -> None:
        write_session_store(root)
        self._write(
            root / "agent.py",
            "from harnest.agent import Agent\n"
            "from harnest.graph import START, Edge, Graph\n"
            "root_agent = Graph(\n"
            "    name='root',\n"
            "    nodes={\n"
            "        'inline': Agent(name='inline', model='test/model'),\n"
            "        'researcher': 'researcher',\n"
            "    },\n"
            "    edges=(Edge(START, 'inline'), Edge(START, 'researcher')),\n"
            ")\n",
        )
        self._write(root / "instructions.md", "Root instructions.\n")
        self._write(
            root / "tools" / "root_tool.py",
            "from harnest.tool import tool\n"
            "@tool\n"
            "def root_tool(value):\n"
            "    \"\"\"Use the root tool.\"\"\"\n"
            "    return value\n",
        )
        self._write(
            root / "mcp" / "root_mcp.py",
            "from harnest.mcp import MCPClient\n"
            "def client():\n"
            "    return MCPClient.streamable_http(\n"
            "        'https://root.example/mcp', prefix='root')\n",
        )
        self._write(
            root / "skills" / "root_skill" / "SKILL.md",
            "---\nname: root_skill\ndescription: Root guidance.\n---\n\n# Root\n",
        )

        nested = root / "subagents" / "researcher"
        self._write(
            nested / "agent.py",
            "from harnest.agent import Agent\n"
            "researcher = Agent(name='researcher', model='test/model')\n",
        )
        self._write(nested / "instructions.md", "Research privately.\n")
        self._write(
            nested / "tools" / "local_tool.py",
            "from harnest.tool import tool\n"
            "@tool\n"
            "def local_tool(value):\n"
            "    \"\"\"Use the local tool.\"\"\"\n"
            "    return value\n",
        )
        self._write(
            nested / "mcp" / "local_mcp.py",
            "from harnest.mcp import MCPClient\n"
            "def client():\n"
            "    return MCPClient.streamable_http(\n"
            "        'https://local.example/mcp', prefix='local')\n",
        )
        self._write(
            nested / "skills" / "local_skill" / "SKILL.md",
            "---\nname: local_skill\ndescription: Local guidance.\n---\n\n# Local\n",
        )
        if sandbox:
            self._write(
                root / "sandbox" / "sandbox.py",
                "from harnest.sandbox import Sandbox\n"
                "sandbox = Sandbox.provider(lambda: None, name='root-sandbox')\n",
            )
            self._write(
                nested / "sandbox" / "sandbox.py",
                "from harnest.sandbox import Sandbox\n"
                "sandbox = Sandbox.provider(lambda: None, name='local-sandbox')\n",
            )

    def _compile_with_real_backend(self, root: Path, framework: str):
        self._write_scoped_graph(root, sandbox=False)
        for relative in (
            "mcp",
            "skills",
            "subagents/researcher/mcp",
            "subagents/researcher/skills",
        ):
            shutil.rmtree(root / relative)
        if framework == "langgraph":
            for path in (root / "agent.py", root / "subagents/researcher/agent.py"):
                source = path.read_text(encoding="utf-8")
                source = source.replace(
                    "from harnest.agent import Agent\n",
                    "from harnest.agent import Agent\n"
                    "from langchain_core.language_models.fake_chat_models "
                    "import FakeListChatModel\n"
                    "model = FakeListChatModel(responses=['ok'])\n",
                ).replace("model='test/model'", "model=model")
                path.write_text(source, encoding="utf-8")

        actual = get_backend(framework)
        lowered_graphs = []

        def lower_graph(graph, native_extensions, checkpointer):
            lowered_graphs.append(graph)
            return actual._lower_graph(graph, native_extensions, checkpointer)

        observed = replace(actual, _lower_graph=lower_graph)
        with patch("harnest.bundle.get_backend", return_value=observed):
            compiled = compile_application(
                root, entrypoint="agent:root_agent", framework=framework
            )
        return compiled, lowered_graphs[0]

    def _assert_real_backend_scope(self, framework: str) -> None:
        with tempfile.TemporaryDirectory() as temp:
            compiled, graph = self._compile_with_real_backend(Path(temp), framework)
        inline = graph.nodes["inline"]
        researcher = graph.nodes["researcher"]
        self.assertEqual([tool.__name__ for tool in inline.tools], ["root_tool"])
        self.assertEqual([tool.__name__ for tool in researcher.tools], ["local_tool"])
        self.assertIsNotNone(compiled.target)

    def test_adk_real_backend_lowers_folder_scoped_tools(self):
        self._assert_real_backend_scope("adk")

    def test_langgraph_real_backend_lowers_folder_scoped_tools(self):
        self._assert_real_backend_scope("langgraph")

    def test_adk_graph_preserves_nested_folder_resource_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_scoped_graph(root, sandbox=True)
            self._write(
                root / "subagents" / "assistant.py",
                "from harnest.agent import Agent\n"
                "assistant = Agent(name='assistant', model='test/model', "
                "instruction='Assist the root.')\n",
            )
            self._write(
                root / "subagents" / "researcher" / "subagents" / "critic.py",
                "from harnest.agent import Agent\n"
                "critic = Agent(name='critic', model='test/model', "
                "instruction='Critique the research.')\n",
            )

            def skill_toolset(directories, _tools):
                if not directories:
                    return None
                return SimpleNamespace(
                    skill_names=tuple(path.name for path in directories)
                )

            with (
                patch("harnest.bundle.get_backend", return_value=self._backend()),
                patch(
                    "harnest.bundle._discover_skill_toolset",
                    side_effect=skill_toolset,
                ),
            ):
                compiled = compile_application(
                    root, entrypoint="agent:root_agent", framework="adk"
                )

        self.assertIsInstance(compiled.target, Graph)
        inline = compiled.target.nodes["inline"]
        researcher = compiled.target.nodes["researcher"]
        self.assertIsInstance(inline, AgentDefinition)
        self.assertIsInstance(researcher, AgentDefinition)
        self.assertEqual(inline.tools[0].__name__, "root_tool")
        self.assertEqual(inline.tools[1].skill_names, ("root_skill",))
        self.assertEqual(researcher.tools[0].__name__, "local_tool")
        self.assertEqual(researcher.tools[1].skill_names, ("local_skill",))
        self.assertEqual([client.tool_name_prefix for client in inline.mcp], ["root"])
        self.assertEqual(
            [client.capability_id for client in inline.mcp], ["mcp__root_mcp"]
        )
        self.assertEqual(
            [client.tool_name_prefix for client in researcher.mcp], ["local"]
        )
        self.assertEqual(
            [client.capability_id for client in researcher.mcp],
            ["agent__researcher__mcp__local_mcp"],
        )
        self.assertEqual(inline.sandbox.backend, "root-sandbox")
        self.assertEqual(researcher.sandbox.backend, "local-sandbox")
        self.assertEqual([child.name for child in researcher.subagents], ["critic"])

    def test_langgraph_graph_preserves_nested_folder_resource_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_scoped_graph(root, sandbox=False)
            with patch(
                "harnest.bundle.get_backend", return_value=self._backend()
            ):
                compiled = compile_application(
                    root, entrypoint="agent:root_agent", framework="langgraph"
                )
                root_tools = {
                    tool.__name__: tool
                    for tool in compiled.target.nodes["inline"].tools
                }
                loaded_root_skill = root_tools["load_skill"]("root_skill")

        inline = compiled.target.nodes["inline"]
        researcher = compiled.target.nodes["researcher"]
        inline_tools = {tool.__name__: tool for tool in inline.tools}
        researcher_tools = {tool.__name__: tool for tool in researcher.tools}
        self.assertIn("root_tool", inline_tools)
        self.assertNotIn("local_tool", inline_tools)
        self.assertIn("local_tool", researcher_tools)
        self.assertNotIn("root_tool", researcher_tools)
        self.assertEqual(
            json.loads(inline_tools["list_skills"]())["skills"],
            [{"name": "root_skill", "description": "Root guidance."}],
        )
        self.assertEqual(
            json.loads(researcher_tools["list_skills"]())["skills"],
            [{"name": "local_skill", "description": "Local guidance."}],
        )
        self.assertIn(
            "description: Root guidance.",
            loaded_root_skill,
        )
        self.assertEqual([client.tool_name_prefix for client in inline.mcp], ["root"])
        self.assertEqual(
            [client.capability_id for client in inline.mcp], ["mcp__root_mcp"]
        )
        self.assertEqual(
            [client.tool_name_prefix for client in researcher.mcp], ["local"]
        )
        self.assertEqual(
            [client.capability_id for client in researcher.mcp],
            ["agent__researcher__mcp__local_mcp"],
        )

    def test_langgraph_skill_catalog_requires_portable_routing_metadata(self):
        """Fail compilation when LangGraph cannot describe a skill before loading it."""

        cases = (
            ("# Missing frontmatter\n", "start with YAML frontmatter"),
            (
                "---\nname: another\ndescription: Route requests.\n---\n",
                "name must match directory",
            ),
            (
                "---\nname: routing\ndescription: ''\n---\n",
                "description must be a non-empty string",
            ),
            (
                "---\nname: routing\ndescription: " + ("x" * 1025) + "\n---\n",
                "description must be at most 1024 characters",
            ),
        )
        for contents, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write(
                    root / "agent.py",
                    "from harnest.agent import Agent\n"
                    "root_agent = Agent(name='root', model='test/model')\n",
                )
                self._write(root / "instructions.md", "Use relevant skills.\n")
                self._write(root / "skills" / "routing" / "SKILL.md", contents)
                write_session_store(root)

                with patch("harnest.bundle.get_backend", return_value=self._backend()):
                    with self.assertRaisesRegex(BundleSkillError, expected):
                        compile_application(
                            root,
                            entrypoint="agent:root_agent",
                            framework="langgraph",
                        )

    def test_nested_reference_does_not_consume_any_root_resource(self):
        cases = {
            "tools/root_tool.py": (
                "from harnest.tool import tool\n"
                "@tool\n"
                "def root_tool(value):\n"
                "    \"\"\"Root tool.\"\"\"\n"
                "    return value\n",
                "does not consume discovered resources: tool 'root_tool'",
            ),
            "mcp/root_mcp.py": (
                "from harnest.mcp import MCPClient\n"
                "def client():\n"
                "    return MCPClient.streamable_http('https://root.example/mcp')\n",
                "MCP clients but no Agent node in the root graph",
            ),
            "skills/root_skill/SKILL.md": (
                "# Root skill\n",
                "skills but no Agent node in the root graph",
            ),
            "sandbox/sandbox.py": (
                "from harnest.sandbox import Sandbox\n"
                "sandbox = Sandbox.provider(lambda: None, name='root')\n",
                "sandbox configuration but no Agent node in the root graph",
            ),
        }
        for relative, (source, expected) in cases.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._write(
                    root / "agent.py",
                    "from harnest.graph import START, Edge, Graph\n"
                    "root_agent = Graph(name='root', "
                    "nodes={'researcher': 'researcher'}, "
                    "edges=(Edge(START, 'researcher'),))\n",
                )
                self._write(
                    root / "subagents" / "researcher" / "agent.py",
                    "from harnest.agent import Agent\n"
                    "researcher = Agent(name='researcher', model='test/model')\n",
                )
                self._write(
                    root / "subagents" / "researcher" / "instructions.md",
                    "Research.\n",
                )
                self._write(root / relative, source)
                write_session_store(root)
                with patch(
                    "harnest.bundle.get_backend", return_value=self._backend()
                ):
                    with self.assertRaisesRegex(BundleConventionError, expected):
                        compile_application(
                            root, entrypoint="agent:root_agent", framework="adk"
                        )

    def test_advanced_mode_rejects_managed_capability_folders(self):
        resources = {
            "tools": "lookup.py",
            "subagents": "helper.py",
            "mcp": "server.py",
            "plugins": "audit.py",
            "sandbox": "sandbox.py",
            "skills": "research/SKILL.md",
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
            write_session_store(root)
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

    def test_nested_advanced_subagent_is_rejected_instead_of_ignoring_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(
                root / "native_child" / "agent.py",
                "from harnest.agent import Agent\n"
                "native_child = Agent.advanced(object(), name='native_child')\n",
            )
            self._write(
                root / "native_child" / "tools" / "ignored.py",
                "from harnest.tool import tool\n"
                "@tool\n"
                "def ignored():\n"
                "    return 'ignored'\n",
            )

            with self.assertRaisesRegex(
                BundleExportError,
                r"nested advanced subagent.*subagents/native_child\.py",
            ):
                _discover_subagents(root)

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
                write_session_store(root)
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
                "def client():\n"
                "    return MCPClient.streamable_http('https://mcp.example/mcp')\n",
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
                write_session_store(root)
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
            write_session_store(root)
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

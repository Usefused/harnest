"""Verify filesystem-first, non-executing Studio workspace inspection."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from harnest.playground_inspector import StudioInspectionError, inspect_workspace


_CONFIG = """apiVersion: harnest.dev/v1alpha1
kind: Agent
metadata:
  name: support-agent
  displayName: Support Agent
spec:
  entrypoint: agent:root_agent
  framework:
    name: adk
    mode: managed
  runtime:
    version: "3.12"
  environment:
    API_TOKEN: studio-inspector-secret
  secrets:
    - environmentVariable: PRIVATE_KEY
      secretRef: production-key
"""


class StudioInspectorTests(unittest.TestCase):
    """Exercise static declaration, graph, source identity, and safety behavior."""

    def test_inactive_resources_and_ambiguous_models_have_no_ownership_edges(self):
        """A source declaration is not proof that the compiler wires it to an agent."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='root', model=shared)\n")
            _write(root / "tools/_example.py", "@tool\ndef example(): pass\n")
            _write(root / "models/one.py", "shared = LiteLLMModel('one')\n")
            _write(root / "models/two.py", "shared = LiteLLMModel('two')\n")
            _write(root / "subagents/_example.py", "example = Agent(name='example')\n")
            projection = inspect_workspace(root)
            self.assertEqual(projection.connections, ())
            self.assertIn("tools/_example.py", [item.path for item in projection.files])

    def test_client_tools_and_environment_models_are_components(self):
        """Canonical browser tools and credential-free model factories stay visible."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='root', model=primary)\n")
            _write(root / "tools/browser.py", "@client_tool\ndef browser(url: str): pass\n")
            _write(root / "models/primary.py", "primary = LiteLLMModel.from_openai_environment()\n")
            projection = inspect_workspace(root)
            kinds = {block.name: block.kind for block in projection.blocks}
            self.assertEqual(kinds["browser"], "tool")
            self.assertEqual(kinds["primary"], "model")
            self.assertEqual(len(projection.connections), 2)

    def test_inspection_finds_declarations_and_graph_without_execution(self) -> None:
        """Top-level side effects and business function bodies remain fully opaque."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            _write(root / "config.yaml", _CONFIG)
            _write(
                root / "agent.py",
                f'''from pathlib import Path
from harnest.agent import AgentDefinition, tool
from harnest.graph import START, Edge, Graph

Path({str(marker)!r}).write_text("executed")

@tool(description="Look up a case", permission="cases.read")
def lookup(case_id: str) -> dict:
    """This body must be opaque even when it contains declaration-like text."""
    hidden = AgentDefinition(name="hidden", model="secret-model")
    return {{"id": case_id, "hidden": hidden}}

def normalize(value: dict) -> dict:
    marker = "opaque-normalizer-body"
    return {{**value, "marker": marker}}

root_agent = Graph(
    name="support",
    description="Support flow",
    nodes={{
        "lookup": lookup,
        "normalize": normalize,
        "respond": AgentDefinition(
            name="responder",
            model="openai/gpt-5.2",
            instruction="Do not persist this instruction",
        ),
    }},
    edges=(
        Edge(START, "lookup"),
        Edge("lookup", "normalize", route="found"),
        Edge("normalize", "respond"),
    ),
)
''',
            )

            projection = inspect_workspace(root)

            self.assertFalse(marker.exists())
            blocks = {block.name: block for block in projection.blocks}
            self.assertEqual(blocks["lookup"].kind, "tool")
            self.assertEqual(blocks["normalize"].kind, "function")
            self.assertEqual(blocks["root_agent"].kind, "graph")
            self.assertEqual(blocks["respond"].kind, "agent")
            self.assertNotIn("hidden", blocks)
            self.assertEqual(len(projection.connections), 3)
            self.assertEqual(
                [edge.route for edge in projection.connections].count("found"), 1
            )
            serialized = json.dumps(projection.to_dict())
            self.assertNotIn("studio-inspector-secret", serialized)
            self.assertNotIn("production-key", serialized)
            self.assertNotIn("Do not persist this instruction", serialized)
            self.assertNotIn("opaque-normalizer-body", serialized)

    def test_inspection_uses_shared_ignores_and_content_digest(self) -> None:
        """Generated/cache trees cannot perturb the Studio source identity."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='one', model='model')\n")
            for ignored in (".git", ".harnest", ".venv", "cache", "__pycache__"):
                _write(root / ignored / "ignored.py", "ignored = Graph()\n")

            first = inspect_workspace(root)
            _write(root / ".git" / "ignored.py", "changed = True\n")
            second = inspect_workspace(root)
            _write(root / "agent.py", "root_agent = Agent(name='two', model='model')\n")
            third = inspect_workspace(root)

            self.assertEqual(first.source_digest, second.source_digest)
            self.assertNotEqual(second.source_digest, third.source_digest)
            self.assertEqual(
                [item.path for item in first.files], ["agent.py", "config.yaml"]
            )

    def test_model_connections_project_safe_positional_configuration(self) -> None:
        """Expose reusable model settings without importing provider code or secrets."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(
                root / "lib/models.py",
                '''from harnest.model import LiteLLMModel

primary_model = LiteLLMModel(
    "openai/gpt-5.2",
    temperature=0.2,
    api_base="https://models.example.test/v1",
    api_key="must-not-be-projected",
)
''',
            )

            projection = inspect_workspace(root)
            model = next(block for block in projection.blocks if block.name == "primary_model")

            self.assertEqual(model.kind, "model")
            self.assertEqual(model.config["model"], "openai/gpt-5.2")
            self.assertEqual(model.config["temperature"], 0.2)
            self.assertEqual(model.config["api_base"], "https://models.example.test/v1")
            self.assertNotIn("must-not-be-projected", json.dumps(projection.to_dict()))

    def test_plain_agent_projects_its_discovered_tools_and_sandbox(self) -> None:
        """Show the complete compiled composition of a non-graph managed agent."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(
                root / "agent.py",
                '''from harnest.agent import Agent
from harnest.model import LiteLLMModel

root_agent = Agent(
    name="chrome_researcher",
    model=LiteLLMModel.from_openai_environment(api_key="hidden"),
    sandboxes=["chrome"],
)
''',
            )
            _write(
                root / "tools/browse_page.py",
                "from harnest.agent import tool\n@tool\ndef browse_page(url: str):\n    return url\n",
            )
            _write(
                root / "tools/search.py",
                "from harnest.agent import tool\n@tool\ndef search(query: str):\n    return query\n",
            )
            _write(
                root / "sandbox/chrome.py",
                '''from harnest.extensions.docker import docker

chrome = docker.sandbox(
    image="harnest-chrome:1.0",
    timeout_seconds=45,
    max_output_bytes=32768,
)
''',
            )

            projection = inspect_workspace(root)
            blocks = {block.name: block for block in projection.blocks}
            agent = blocks["root_agent"]

            self.assertEqual(agent.config["name"], "chrome_researcher")
            self.assertEqual(agent.config["sandboxes"], ["chrome"])
            self.assertEqual(
                agent.config["model"],
                {"constructor": "LiteLLMModel.from_openai_environment"},
            )
            self.assertEqual(blocks["chrome"].kind, "sandbox")
            self.assertEqual(blocks["chrome"].config["image"], "harnest-chrome:1.0")
            self.assertEqual(blocks["chrome"].config["timeout_seconds"], 45)
            self.assertEqual(
                {(edge.source, edge.target) for edge in projection.connections},
                {
                    (agent.id, blocks["browse_page"].id),
                    (agent.id, blocks["search"].id),
                    (agent.id, blocks["chrome"].id),
                },
            )
            self.assertNotIn("hidden", json.dumps(projection.to_dict()))

    def test_syntax_errors_are_diagnostics_and_config_is_required(self) -> None:
        """Syntax problems remain visible while a missing project marker is fatal."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(StudioInspectionError, "requires config.yaml"):
                inspect_workspace(root)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "def broken(:\n")
            projection = inspect_workspace(root)
            self.assertEqual(projection.diagnostics[0].code, "python_parse_error")
            self.assertEqual(projection.diagnostics[0].path, "agent.py")

    def test_conventional_mcp_factory_is_projected_without_execution(self) -> None:
        """Only a zero-argument direct MCPClient return opens the narrow body exception."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            _write(root / "config.yaml", _CONFIG)
            _write(
                root / "mcp/knowledge.py",
                f'''from pathlib import Path
from harnest.mcp import MCPClient

Path({str(marker)!r}).write_text("executed")

def client():
    """Build the statically named knowledge client."""
    return MCPClient.streamable_http(
        "https://example.test/private-url",
        tools=("search", "fetch"),
        prefix="knowledge",
        timeout_seconds=12,
    )
''',
            )
            _write(
                root / "mcp/dynamic.py",
                """from harnest.mcp import MCPClient
def client():
    endpoint = "https://example.test"
    return MCPClient.sse(endpoint)
""",
            )

            projection = inspect_workspace(root)
            blocks = {block.name: block for block in projection.blocks}

            self.assertFalse(marker.exists())
            self.assertEqual(blocks["knowledge"].kind, "mcp")
            self.assertEqual(blocks["knowledge"].config["transport"], "streamable_http")
            self.assertEqual(blocks["knowledge"].config["tools"], ["search", "fetch"])
            self.assertNotIn("dynamic", blocks)
            self.assertNotIn("private-url", json.dumps(projection.to_dict()))

    def test_lifecycle_context_and_skill_metadata_are_projected_safely(self) -> None:
        """Folder and decorator conventions expose roles without source bodies."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(
                root / "lifecycle/resources.py",
                '''from harnest import context
from harnest import lifecycle

@lifecycle.tool.before
def audit(context, call):
    return "opaque-lifecycle-body"

@context.provider("request_cache", order=2)
def request_cache():
    return "opaque-context-body"

@lifecycle.resource
@context.provider("memory")
def memory():
    return "opaque-combined-body"
''',
            )
            _write(
                root / "skills/incident-triage/SKILL.md",
                """---
name: incident-triage
description: Triage public incident reports.
api_key: frontmatter-secret
---

# Private procedure

opaque-skill-instructions
""",
            )

            projection = inspect_workspace(root)
            blocks = {block.name: block for block in projection.blocks}

            self.assertEqual(blocks["audit"].kind, "lifecycle")
            self.assertEqual(blocks["audit"].config["role"], "tool.before")
            self.assertEqual(blocks["request_cache"].kind, "context")
            self.assertEqual(blocks["request_cache"].config["name"], "request_cache")
            self.assertEqual(blocks["memory"].config["role"], "resource")
            self.assertEqual(blocks["memory"].config["context"], "memory")
            self.assertEqual(
                blocks["incident-triage"].config,
                {
                    "name": "incident-triage",
                    "description": "Triage public incident reports.",
                },
            )
            serialized = json.dumps(projection.to_dict())
            self.assertNotIn("frontmatter-secret", serialized)
            self.assertNotIn("opaque-skill-instructions", serialized)
            self.assertNotIn("opaque-lifecycle-body", serialized)

    def test_agent_plugins_and_extensions_are_workspace_resource_blocks(self) -> None:
        """Portable and in-process packages project their public manifests only."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(
                root / "plugins/support-tools/plugin.json",
                json.dumps(
                    {
                        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                        "name": "support-tools",
                        "version": "1.2.0",
                        "description": "Portable support capabilities.",
                        "extensions": {"private_token": "manifest-secret"},
                    }
                ),
            )
            _write(
                root / "plugins/support-tools/skills/respond/SKILL.md",
                "---\nname: respond\ndescription: Draft a response.\n---\n\nprivate steps\n",
            )
            _write(
                root / "extensions/audit/extension.yaml",
                """apiVersion: harnest.dev/v1alpha1
kind: Extension
metadata:
  name: audit
  version: 0.1.0
runtime:
  entrypoint: extension:extension
capabilities:
  - lifecycle.tool
requires:
  extensions:
    - storage
private_token: extension-secret
""",
            )
            _write(root / "extensions/audit/extension.py", "extension_secret = 'hidden'\n")

            projection = inspect_workspace(root)
            blocks = {(block.kind, block.name): block for block in projection.blocks}

            plugin = blocks[("agent_plugin", "support-tools")]
            self.assertEqual(plugin.path, "plugins/support-tools/plugin.json")
            self.assertEqual(plugin.config["standard"], "Agent Plugins 1.0")
            self.assertTrue(plugin.config["skills"])
            extension = blocks[("extension", "audit")]
            self.assertEqual(extension.config["capabilities"], ["lifecycle.tool"])
            self.assertEqual(extension.config["requires"], ["storage"])
            self.assertIn(("skill", "respond"), blocks)
            serialized = json.dumps(projection.to_dict())
            self.assertNotIn("manifest-secret", serialized)
            self.assertNotIn("extension-secret", serialized)
            self.assertNotIn("hidden", serialized)

    def test_invalid_package_manifests_are_safe_diagnostics(self) -> None:
        """Malformed package descriptors cannot abort inspection or leak source."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "plugins/broken/plugin.json", '{"name":"secret","name":')
            _write(root / "extensions/broken/extension.yaml", "metadata: [secret\n")

            projection = inspect_workspace(root)

            self.assertEqual(
                {item.code for item in projection.diagnostics},
                {"plugin_manifest_error", "extension_manifest_error"},
            )
            self.assertNotIn("secret", json.dumps(projection.to_dict()))


class LibImportTests(unittest.TestCase):
    """Verify static `harnest.lib` import resolution without executing source."""

    def test_submodule_and_symbol_imports_resolve_to_lib_files(self) -> None:
        """Both `import a submodule` and `import a symbol` resolve without ambiguity."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='root')\n")
            _write(root / "lib/validation.py", "def normalize(x): return x\n")
            _write(root / "lib/storage/queries.py", "def get_conn(): return None\n")
            _write(
                root / "tools/lookup.py",
                "from harnest.lib.validation import normalize\n"
                "from harnest.lib.storage import queries\n"
                "import harnest.lib.storage.queries as q2\n"
                "@tool\ndef lookup(x: str) -> str:\n    return normalize(x)\n",
            )
            projection = inspect_workspace(root)
            files = {item.path: item.lib_imports for item in projection.files}
            self.assertEqual(
                set(files["tools/lookup.py"]),
                {"lib/validation.py", "lib/storage/queries.py"},
            )

    def test_symbol_defined_in_module_resolves_to_parent_file(self) -> None:
        """Importing a name defined inside a lib module still resolves to that file."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='root')\n")
            _write(root / "lib/storage.py", "def get_conn(): return None\n")
            _write(
                root / "tools/lookup.py",
                "from harnest.lib.storage import get_conn\n"
                "@tool\ndef lookup() -> str:\n    return str(get_conn())\n",
            )
            projection = inspect_workspace(root)
            files = {item.path: item.lib_imports for item in projection.files}
            self.assertEqual(files["tools/lookup.py"], ("lib/storage.py",))

    def test_nested_and_unrelated_imports(self) -> None:
        """Function-body imports are still found; unresolvable ones are dropped silently."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='root')\n")
            _write(root / "lib/storage.py", "def get_conn(): return None\n")
            _write(
                root / "tools/lookup.py",
                "from harnest.other import unrelated\n"
                "@tool\ndef lookup() -> str:\n"
                "    from harnest.lib.storage import get_conn\n"
                "    from harnest.lib.missing import nope\n"
                "    return str(get_conn())\n",
            )
            projection = inspect_workspace(root)
            files = {item.path: item.lib_imports for item in projection.files}
            self.assertEqual(files["tools/lookup.py"], ("lib/storage.py",))

    def test_files_without_lib_imports_have_no_field_noise(self) -> None:
        """Ordinary files report an empty tuple, not an omitted or null field."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "config.yaml", _CONFIG)
            _write(root / "agent.py", "root_agent = Agent(name='root')\n")
            projection = inspect_workspace(root)
            file = next(item for item in projection.files if item.path == "agent.py")
            self.assertEqual(file.lib_imports, ())
            self.assertEqual(file.to_dict()["lib_imports"], [])


def _write(path: Path, contents: str) -> None:
    """Create one fixture file and any conventional parent folder."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

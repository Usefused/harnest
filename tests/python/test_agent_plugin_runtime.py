"""Compiled portable packages cross real framework, HTTP, and MCP boundaries."""

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import warnings

from fastapi.testclient import TestClient

from harnest.agent_plugin_manifest import MCP_SCHEMA, PLUGIN_SCHEMA
from harnest.agent_plugin_runtime import PortableMCP, installation_id
from harnest.bundle import compile_artifact
from harnest.runtime import create_fastapi_app


_ADK_MODEL = '''
import json
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from harnest.agent import Agent

class ProbeModel(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        responses = [part.function_response for content in llm_request.contents
                     for part in content.parts or [] if part.function_response is not None]
        if len(responses) >= 2:
            part = types.Part(text=json.dumps([dict(item.response) for item in responses]))
        else:
            name = "list_skills" if not responses else next(
                name for name in llm_request.tools_dict if name.endswith("_proof"))
            part = types.Part(function_call=types.FunctionCall(
                id=f"probe-{len(responses)}", name=name, args={}))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))

root_agent = Agent(name="portable_probe", model=ProbeModel(model="deterministic-local"))
'''

_LANGGRAPH_MODEL = '''
import json
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from harnest.agent import Agent

class ProbeModel(BaseChatModel):
    proof_name: str = ""

    @property
    def _llm_type(self):
        return "portable-plugin-proof"

    def bind_tools(self, tools, **kwargs):
        names = [tool.name for tool in tools]
        if "list_skills" not in names:
            raise AssertionError("portable skills unavailable")
        self.proof_name = next(name for name in names if name.endswith("_proof"))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        responses = [message for message in messages if message.type == "tool"]
        if len(responses) >= 2:
            message = AIMessage(content=json.dumps([item.content for item in responses]))
        else:
            name = "list_skills" if not responses else self.proof_name
            message = AIMessage(content="", tool_calls=[{
                "name": name, "args": {}, "id": f"probe-{len(responses)}", "type": "tool_call"}])
        return ChatResult(generations=[ChatGeneration(message=message)])

root_agent = Agent(name="portable_probe", model=ProbeModel())
'''

_SERVER = '''
import json
import os
from pathlib import Path
import sys
from mcp.server.fastmcp import FastMCP

server = FastMCP("portable-proof")

@server.tool()
def proof() -> str:
    """Report standard runtime paths and persist one private proof record."""
    data = Path(os.environ["PLUGIN_DATA"])
    record = {
        "root": os.environ["PLUGIN_ROOT"], "data": str(data),
        "cwd": str(Path.cwd()), "literalEnv": os.environ["LITERAL"],
        "literalArg": sys.argv[1], "proof": "portable-mcp-ran",
    }
    with (data / "proof.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\\n")
    return json.dumps(record)

if __name__ == "__main__":
    server.run(transport="stdio")
'''


class AgentPluginRuntimeTests(unittest.TestCase):
    """Run two immutable generations against one installation's persistent state."""

    def setUp(self):
        """Scope every process path and client-owned state to a temporary workspace."""
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.source = self.root / "source"
        self.data = self.root / "persistent-data"
        environment = patch.dict(os.environ, {
            "HARNEST_PLUGIN_DATA_DIR": str(self.data),
            "UNRECOGNIZED_PROOF_VARIABLE": "must-not-expand",
            # Runtime privacy defaults predate portable plugins; seed them so
            # environment equality isolates accidental plugin variable injection.
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
            "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def _write(self, relative, content):
        """Author fixture files without materializing runtime state."""
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _application(self, framework):
        """Use deterministic model decisions while retaining genuine framework execution."""
        model = _ADK_MODEL if framework == "adk" else _LANGGRAPH_MODEL
        self._write("agent.py", model)
        self._write("instructions.md", "List skills, then call the portable proof tool.\n")
        self._write("agent-card.yaml", "name: Portable proof\ndescription: Agent Plugin runtime test.\n")
        self._write("lifecycle/storage.py", '''
            from harnest.checkpoint import MemoryStore
            from harnest.lifecycle import lifecycle
            from harnest.session import InMemorySessionStore

            @lifecycle.storage.sessions
            def sessions():
                return InMemorySessionStore()

            @lifecycle.storage.checkpoints
            def checkpoints():
                return MemoryStore()
        ''')
        self._package()

    def _package(self):
        """Bundle real server code and an unavailable peer in an unchanged standard package."""
        self._write("plugins/proof/plugin.json", json.dumps({
            "$schema": PLUGIN_SCHEMA, "name": "portable-proof", "version": "1.0.0",
        }))
        self._write("plugins/proof/mcp.json", json.dumps({
            "$schema": MCP_SCHEMA,
            "mcpServers": {
                "missing": {"type": "stdio", "command": "harnest-proof-missing-executable"},
                "working": {
                    "type": "stdio", "command": Path(sys.executable).name,
                    "args": ["${PLUGIN_ROOT}/scripts/server.py", "${UNRECOGNIZED_PROOF_VARIABLE}"],
                    "env": {
                        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
                        "LITERAL": "${UNRECOGNIZED_PROOF_VARIABLE}",
                    },
                },
            },
        }))
        self._write("plugins/proof/scripts/server.py", _SERVER)
        self._write("plugins/proof/skills/proof-skill/SKILL.md", '''
            ---
            name: proof-skill
            description: Explain the portable proof tool.
            ---
            Call the proof tool to report its standard package paths.
        ''')

    def _request(self, artifact):
        """Exercise process startup and tool calls through the released HTTP surface."""
        application = create_fastapi_app(artifact, playground_enabled=False)
        with TestClient(application) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            created = client.post("/sessions", json={"id": "portable-proof", "state": {}})
            self.assertEqual(created.status_code, 201, created.text)
            response = client.post("/responses", json={
                "input": "List the plugin skill and run its proof.", "sessionId": "portable-proof",
            })
            self.assertEqual(response.status_code, 200, response.text)
            output = response.json()["outputText"]
            self.assertIn("proof-skill", output)
            self.assertIn("portable-mcp-ran", output)
            self.assertIn("${UNRECOGNIZED_PROOF_VARIABLE}", output)

    def _assert_record(self, record, artifact, expected_data):
        """Distinguish immutable package locations from the stable writable installation."""
        package_root = artifact / "source" / "plugins" / "proof"
        self.assertEqual(record["root"], str(package_root))
        self.assertEqual(record["cwd"], str(package_root))
        self.assertEqual(record["data"], str(expected_data))
        self.assertEqual(record["literalEnv"], "${UNRECOGNIZED_PROOF_VARIABLE}")
        self.assertEqual(record["literalArg"], "${UNRECOGNIZED_PROOF_VARIABLE}")

    def _exercise(self, framework):
        """Both compiles stay read-only; both runtime generations share explicit source scope."""
        self._application(framework)
        artifacts = [self.root / "generation-one", self.root / "generation-two"]
        original_environment = dict(os.environ)
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            for artifact in artifacts:
                compile_artifact(self.source, artifact, framework=framework)
            self.assertFalse(self.data.exists())
            for artifact in artifacts:
                self._request(artifact)
        self.assertEqual(dict(os.environ), original_environment)
        expected_data = self.data / installation_id(self.source) / "portable-proof"
        records = [json.loads(line) for line in (expected_data / "proof.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 2)
        for record, artifact in zip(records, artifacts):
            self._assert_record(record, artifact, expected_data)
        diagnostic = "\n".join(str(item.message) for item in recorded)
        self.assertIn("unavailable", diagnostic)
        self.assertNotIn("must-not-expand", diagnostic)
        # Context scoping must reset after artifact import, rather than leaking into later applications.
        unrelated = PortableMCP.create(self.root / "other" / "plugins" / "proof", "portable-proof", "working")
        self.assertNotEqual(unrelated.scope, installation_id(self.source))

    def test_adk_standard_package_survives_unavailable_server_across_generations(self):
        self._exercise("adk")

    def test_langgraph_standard_package_survives_unavailable_server_across_generations(self):
        self._exercise("langgraph")


if __name__ == "__main__":
    unittest.main()

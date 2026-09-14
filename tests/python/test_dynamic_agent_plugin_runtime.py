"""Dynamic plugin skills cross the compiled HTTP and framework boundaries."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
import zipfile

from fastapi.testclient import TestClient

from harnest.agent_plugin_manifest import PLUGIN_SCHEMA
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
        if responses:
            part = types.Part(text=json.dumps(dict(responses[-1].response)))
        else:
            part = types.Part(function_call=types.FunctionCall(
                id="dynamic-skill", name="list_skills", args={}))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))

root_agent = Agent(name="dynamic_probe", model=ProbeModel(model="deterministic-local"))
'''


_LANGGRAPH_MODEL = '''
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from harnest.agent import Agent

class ProbeModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "dynamic-plugin-proof"

    def bind_tools(self, tools, **kwargs):
        if "list_skills" not in [tool.name for tool in tools]:
            raise AssertionError("dynamic plugin skill tools unavailable")
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        responses = [message for message in messages if message.type == "tool"]
        message = (
            AIMessage(content=responses[-1].content)
            if responses else
            AIMessage(content="", tool_calls=[{
                "name": "list_skills", "args": {}, "id": "dynamic-skill",
                "type": "tool_call"}])
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

root_agent = Agent(name="dynamic_probe", model=ProbeModel())
'''


def _plugin_descriptor() -> dict[str, object]:
    """Package a skill exactly as Agent Desktop would for session creation."""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr(
            "plugin.json",
            json.dumps({"$schema": PLUGIN_SCHEMA, "name": "desktop-plugin"}),
        )
        package.writestr(
            "skills/desktop-proof/SKILL.md",
            "---\nname: desktop-proof\ndescription: Prove a session plugin loaded.\n---\n"
            "Report that the desktop plugin is active.\n",
        )
    archive = output.getvalue()
    return {
        "type": "inline",
        "name": "desktop-plugin",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "source": {
            "type": "base64",
            "media_type": "application/zip",
            "data": base64.b64encode(archive).decode("ascii"),
        },
    }


class DynamicAgentPluginRuntimeTests(unittest.TestCase):
    """Compile first, then attach one plugin when the client creates a session."""

    def _exercise(self, framework: str) -> None:
        """Prove dynamic skill visibility through a real provider runtime."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            model = _ADK_MODEL if framework == "adk" else _LANGGRAPH_MODEL
            (source / "agent.py").write_text(
                textwrap.dedent(model).lstrip(), encoding="utf-8"
            )
            (source / "instructions.md").write_text(
                "List the skills available to this session.\n", encoding="utf-8"
            )
            (source / "agent-card.yaml").write_text(
                "name: Dynamic proof\ndescription: Dynamic plugin integration.\n",
                encoding="utf-8",
            )
            lifecycle = source / "lifecycle"
            lifecycle.mkdir()
            (lifecycle / "storage.py").write_text(
                textwrap.dedent(
                    '''
                    from harnest import lifecycle
                    from harnest.checkpoint import MemoryStore
                    from harnest.session import InMemorySessionStore

                    @lifecycle.storage.sessions
                    def sessions():
                        return InMemorySessionStore()

                    @lifecycle.storage.checkpoints
                    def checkpoints():
                        return MemoryStore()
                    '''
                ).lstrip(),
                encoding="utf-8",
            )
            artifact = root / "artifact"
            compile_artifact(source, artifact, framework=framework)

            app = create_fastapi_app(artifact, playground_enabled=False)
            with TestClient(app) as client:
                created = client.post(
                    "/sessions",
                    json={"id": "dynamic", "plugins": [_plugin_descriptor()]},
                )
                self.assertEqual(created.status_code, 201, created.text)
                self.assertEqual(
                    created.json()["metadata"]["plugins"][0]["name"],
                    "desktop-plugin",
                )
                response = client.post(
                    "/responses",
                    json={"input": "What is installed?", "sessionId": "dynamic"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("desktop-proof", response.json()["outputText"])

    def test_adk_session_plugin_skill_is_available_after_compilation(self) -> None:
        self._exercise("adk")

    def test_langgraph_session_plugin_skill_is_available_after_compilation(self) -> None:
        self._exercise("langgraph")


if __name__ == "__main__":
    unittest.main()

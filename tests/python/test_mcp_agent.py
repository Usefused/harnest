"""A real managed agent discovers context and retrieves it from a live MCP process."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

from harnest.agent import AgentDefinition
from harnest.application import CompiledApplication, RuntimeCapabilities
from harnest.backends.langgraph import ManagedAgentPlan
from harnest.mcp import MCPClient
from harnest.runtime_contract import InvocationRequest
from harnest.runtime_langgraph import LangGraphRuntimeDriver
from harnest.runtime_pipeline import build_runtime_pipeline


class _DiscoveryModel(BaseChatModel):
    """Choose URIs and prompt names from tool results, not a hard-coded catalogue."""

    @property
    def _llm_type(self):
        return "deterministic-mcp-integration"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        replies = [message for message in messages if isinstance(message, ToolMessage)]
        if len(replies) == 3:
            resource, prompt = (json.loads(message.content) for message in replies[1:])
            answer = resource["contents"][0]["text"] + " / " + prompt["messages"][0]["content"]["text"]
            message = AIMessage(content=answer)
        else:
            operation, arguments = self._select(replies)
            message = AIMessage(content="", tool_calls=[{"name": "knowledge_harnest_" + operation, "args": arguments, "id": f"call-{len(replies)}"}])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _select(self, replies):
        """Use the catalogue's identifiers and argument schema to select the next read."""

        if not replies:
            return "inspect", {}
        catalog = json.loads(replies[0].content)
        if len(replies) == 1:
            return "read_resource", {"uri": catalog["resources"]["resources"][0]["uri"]}
        prompt = catalog["prompts"]["prompts"][0]
        return "get_prompt", {"name": prompt["name"], "arguments": {prompt["arguments"][0]["name"]: "MCP"}}


class MCPAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_agent_discovers_then_reads_resources_and_prompts(self):
        server = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_capabilities_server.py"
        configured = replace(MCPClient.stdio(sys.executable, str(server)), identity="knowledge", capability_id="knowledge")
        definition = AgentDefinition(name="knowledge_agent", model=_DiscoveryModel(), instruction="Discover available context before retrieving it.", mcp=(configured,))
        application = CompiledApplication(name="knowledge_agent", framework="langgraph", mode="managed", target=ManagedAgentPlan(definition, checkpointer=MemorySaver()))
        driver = build_runtime_pipeline(LangGraphRuntimeDriver(application), RuntimeCapabilities(), ())
        try:
            await driver.create_session(session_id="mcp-session", user_id="tester", state={})
            result = await driver.invoke(InvocationRequest(input="Find useful context", user_id="tester", session_id="mcp-session", invocation_id="mcp-run", metadata={}, state_delta={}))
            self.assertEqual(result.text, "# Handbook v0 / Summarize MCP")
        finally:
            await driver.close()

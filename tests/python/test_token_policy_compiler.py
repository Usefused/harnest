"""Compile authored policies through real discovery and both managed backends."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from harnest.bundle import compile_application
from harnest.runtime import _runtime_driver
from harnest.runtime_contract import InvocationRequest
from harnest.tokens import TokenBudgetExceeded, token_policy_scope
from _session_store_fixture import write_session_store


_ADK_MODEL = '''
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
class LocalModel(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))
model = LocalModel(model="test")
'''
_LANGGRAPH_MODEL = '''
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])
'''


class TokenCompilerTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_policy_enforces_and_can_be_disabled_per_invocation(self):
        """Prove the public authoring API survives compiler composition and serving."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_session_store(root)
                (root / "instructions.md").write_text("Answer the question.")
                model = _ADK_MODEL if framework == "adk" else _LANGGRAPH_MODEL
                (root / "agent.py").write_text(
                    model + '\nfrom harnest.agent import Agent\n'
                    'from harnest.tokens import TokenPolicy\n'
                    'root_agent = Agent(name="root", model=model, token_policy=TokenPolicy(enforce=True, max_input_tokens=1))\n'
                )
                application = compile_application(root, framework=framework, entrypoint="agent:root_agent")
                driver = _runtime_driver(application)
                try:
                    await driver.create_session(session_id="s", user_id="u", state={})
                    invocation = InvocationRequest(input="hello", session_id="s", user_id="u", invocation_id="i", metadata={}, state_delta={})
                    with self.assertRaises(TokenBudgetExceeded):
                        await driver.invoke(invocation)
                    with token_policy_scope(None):
                        result = await driver.invoke(replace(invocation, invocation_id="j"))
                    self.assertEqual(result.text, "ok")
                finally:
                    await driver.close()

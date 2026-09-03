"""Exercise explicitly assigned legacy sandboxes through native model loops."""

import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path

from harnest.bundle import compile_artifact
from harnest.runtime import run_agent_message
from harnest.runtime_contract import NoCustomerFacingOutputError

from _session_store_fixture import write_session_store


_ADK_MODEL = '''
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from pydantic import PrivateAttr
from pathlib import Path
from harnest.agent import Agent


class ProbeModel(BaseLlm):
    """Exercise ADK's executable-code processor without a network model."""

    _calls: int = PrivateAttr(default=0)

    async def generate_content_async(self, request, stream=False):
        """Ask for code once, then report the native execution response."""
        self._calls += 1
        with Path(MODEL_JOURNAL).open("a", encoding="utf-8") as stream:
            stream.write(str(self._calls) + "\\n")
        assert self._calls <= 2, "ADK did not return the sandbox result to the model"
        # ADK lowers its native result part to fenced text before calling models.
        results = [
            part.text
            for content in request.contents
            for part in content.parts or []
            if part.text and part.text.startswith("```tool_output")
        ]
        if results:
            part = types.Part(text="completed:" + results[-1])
        else:
            part = FIRST_PART
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]) if part else None,
            finish_reason=types.FinishReason.STOP,
        )


root_agent = Agent(name="sandbox_probe", model=ProbeModel(model="offline"))
'''


_ADK_FIRST_PARTS = {
    "executable": (
        'types.Part(executable_code=types.ExecutableCode('
        'code="print(42)", language="PYTHON"))'
    ),
    "fenced": 'types.Part(text="```python\\nprint(42)\\n```")',
    "empty": "None",
    "text": 'types.Part(text="ordinary final answer")',
}


_LANGGRAPH_MODEL = '''
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from harnest.agent import Agent


class ProbeModel(BaseChatModel):
    """Exercise LangGraph's native tool loop using deterministic messages."""

    @property
    def _llm_type(self):
        """Identify this offline model to LangChain."""
        return "sandbox-offline"

    def bind_tools(self, tools, **kwargs):
        """Require compiler-installed sandbox tools before generating calls."""
        assert "harnest_execute_python" in {tool.name for tool in tools}
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Request one execution and expose the framework's tool response."""
        if getattr(messages[-1], "type", None) == "tool":
            response = AIMessage(content="completed:" + messages[-1].content)
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "harnest_execute_python", "args": {"code": "print(42)"},
                "id": "sandbox-call", "type": "tool_call",
            }])
        return ChatResult(generations=[ChatGeneration(message=response)])


root_agent = Agent(name="sandbox_probe", model=ProbeModel())
'''


_PROVIDER = '''
import json
from pathlib import Path
from harnest.sandbox import Sandbox, SandboxResult


class OfflineBackend:
    """Simulate a provider boundary without evaluating Python on the host."""

    def execute(self, request):
        """Expose the portable scope to the deterministic model for assertions."""
        assert request.code == "print(42)"
        assert request.timeout_seconds == 8
        assert request.metadata["region"] == "eu"
        assert request.context.agent_name == "sandbox_probe"
        assert request.context.user_id == "_harnest_direct"
        assert request.context.session_id
        assert request.context.invocation_id
        return SandboxResult(stdout=json.dumps({
            "value": 42, "agent": request.context.agent_name,
            "session": request.context.session_id,
            "user": request.context.user_id,
            "invocation": request.context.invocation_id,
        }), metadata={"job": "job-1"})


def build():
    """Journal runtime construction so compilation cannot accidentally start it."""
    with Path(JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write("built\\n")
    return OfflineBackend()


sandbox = Sandbox.provider(build, name="offline", timeout_seconds=8, metadata={"region": "eu"})
'''


_TOOL_HOOKS = '''
from pathlib import Path
from harnest.lifecycle import lifecycle


@lifecycle.before_tool
def before(context, request):
    """Record the normal Harnest tool boundary surrounding the sandbox."""
    with Path(JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write("before:" + context.tool_name + "\\n")
    return context.next()


@lifecycle.after_tool
def after(context, result):
    """Record completion through the same lifecycle as authored tools."""
    with Path(JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write("after:" + context.tool_name + "\\n")
    return context.next()
'''


class SandboxFrameworkTests(unittest.TestCase):
    """Compile portable declarations and execute each framework's native loop."""

    def _write(self, path, source):
        """Materialize one isolated temporary authored resource."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")

    def _source(self, root, framework, journal, graph=False, adk_case="executable", sandbox=True):
        """Author identical sandbox capabilities around native offline models."""
        model = _ADK_MODEL if framework == "adk" else _LANGGRAPH_MODEL
        model = model.replace("FIRST_PART", _ADK_FIRST_PARTS[adk_case]).replace(
            "MODEL_JOURNAL", repr(str(journal.with_name("model-calls.txt"))),
        )
        if sandbox:
            # Legacy code-executor coverage is explicit; registry presence alone
            # must no longer grant any agent sandbox access.
            model = "from harnest.lib.provider import sandbox\n" + model.replace(
                'name="sandbox_probe",', 'name="sandbox_probe", sandbox=sandbox,',
            )
        if graph:
            model += (
                "\nfrom harnest.graph import START, Edge, Graph\n"
                "root_agent = Graph(name='parent_graph', nodes={'worker': root_agent}, "
                "edges=(Edge(START, 'worker'),))\n"
            )
        self._write(root / "agent.py", model)
        self._write(root / "instructions.md", "Use isolated code execution.\n")
        self._write(root / "agent-card.yaml", """
            name: Sandbox probe
            description: Runs deterministic offline sandbox execution.
            version: 0.1.0
        """)
        if sandbox:
            self._write(root / "lib" / "provider.py", _PROVIDER.replace("JOURNAL", repr(str(journal))))
        if framework == "langgraph":
            hook_journal = journal.with_name("tool-hooks.txt")
            self._write(root / "extensions" / "tool_hooks.py", _TOOL_HOOKS.replace("JOURNAL", repr(str(hook_journal))))
        write_session_store(root)

    def _run_framework(self, framework, graph=False, adk_case="executable"):
        """Verify compiler laziness and code execution inside the real runtime."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            journal = Path(directory) / "provider-starts.txt"
            self._source(root, framework, journal, graph, adk_case)
            compile_artifact(root, output, framework=framework)
            self.assertFalse(journal.exists(), "compilation started the sandbox provider")
            result = asyncio.run(run_agent_message(output, "calculate"))
            self.assertEqual(journal.read_text(encoding="utf-8"), "built\n")
            if framework == "adk":
                self.assertEqual(
                    journal.with_name("model-calls.txt").read_text(encoding="utf-8"),
                    "1\n2\n",
                )
            if framework == "langgraph":
                self.assertEqual(
                    journal.with_name("tool-hooks.txt").read_text(encoding="utf-8"),
                    "before:harnest_execute_python\nafter:harnest_execute_python\n",
                )
        self.assertIn("completed:", result["text"])
        self.assertIn("42", result["text"])
        self.assertIn("sandbox_probe", result["text"])
        self.assertIn("_harnest_direct", result["text"])
        self.assertNotIn('"session": null', result["text"])
        self.assertNotIn('"invocation": null', result["text"])
        return result

    def test_explicit_adk_sandbox_runs_native_code_execution_loop(self):
        """Executable-code parts reach the neutral provider through ADK."""
        result = self._run_framework("adk")
        self.assertIn("job-1", result["text"])

    def test_adk_fenced_code_stop_continues_to_final_model_receipt(self):
        """Text-model code fences with STOP also resume ADK after execution."""
        result = self._run_framework("adk", adk_case="fenced")
        self.assertIn("job-1", result["text"])

    def _run_adk_without_execution(self, adk_case, sandbox):
        """Ensure non-code STOP responses never cause an extra model turn."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "artifact"
            journal = Path(directory) / "provider-starts.txt"
            self._source(root, "adk", journal, adk_case=adk_case, sandbox=sandbox)
            compile_artifact(root, output, framework="adk")
            try:
                if adk_case == "empty":
                    with self.assertRaises(NoCustomerFacingOutputError):
                        asyncio.run(run_agent_message(output, "calculate"))
                else:
                    result = asyncio.run(run_agent_message(output, "calculate"))
                    self.assertEqual(result["text"], "ordinary final answer")
            finally:
                self.assertFalse(journal.exists(), "non-code response started the provider")
                self.assertEqual(
                    journal.with_name("model-calls.txt").read_text(encoding="utf-8"),
                    "1\n",
                )

    def test_adk_genuine_empty_stop_remains_failure_without_retry(self):
        """Do not turn an unrelated empty completion into an unbounded retry."""
        self._run_adk_without_execution("empty", sandbox=True)

    def test_adk_text_stop_with_sandbox_remains_final(self):
        """A sandbox-enabled agent can still terminate with ordinary text."""
        self._run_adk_without_execution("text", sandbox=True)

    def test_adk_text_stop_without_sandbox_remains_final(self):
        """Agents without a sandbox retain the native final-response behavior."""
        self._run_adk_without_execution("text", sandbox=False)

    def test_explicit_langgraph_sandbox_runs_native_tool_loop(self):
        """Tool calls reach the neutral provider and preserve result metadata."""
        result = self._run_framework("langgraph")
        self.assertIn("job-1", result["text"])

    def test_explicit_graph_node_sandbox_preserves_child_scope(self):
        """A graph node's explicit sandbox retains the executing agent identity."""
        self._run_framework("langgraph", graph=True)


if __name__ == "__main__":
    unittest.main()

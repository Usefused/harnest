"""Exercise sandbox capabilities inside authored tools in both framework loops."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from harnest.bundle import compile_artifact
from harnest.runtime import run_agent_message

from _session_store_fixture import write_session_store


_ADK_MODEL = '''
import json
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from harnest import Agent


class ProbeModel(BaseLlm):
    """Request business functions without exposing arbitrary sandbox execution."""

    async def generate_content_async(self, request, stream=False):
        """Check exposure and report metadata returned by the actual tool loop."""
        exposed = set(request.tools_dict)
        expected = {name + "_summary" for name in ASSIGNED} | {"verify_unassigned"}
        assert expected.issubset(exposed), exposed
        assert not any(name.startswith("harnest_sandbox_") for name in exposed)
        assert "harnest_execute_python" not in exposed
        results = {
            part.function_response.name: part.function_response.response
            for content in request.contents for part in content.parts or []
            if part.function_response is not None
        }
        missing = sorted(expected - results.keys())
        part = (
            types.Part(function_call=types.FunctionCall(
                id=missing[0], name=missing[0], args={},
            )) if missing else types.Part(text="completed:" + json.dumps(results))
        )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


root_agent = Agent(name="named_probe", model=ProbeModel(model="offline"), sandboxes=ASSIGNED)
'''


_LANGGRAPH_MODEL = '''
import json
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from harnest import Agent


class ProbeModel(BaseChatModel):
    """Request authored business tools without a network model."""

    @property
    def _llm_type(self):
        """Identify the deterministic test model."""
        return "named-sandbox-offline"

    def bind_tools(self, tools, **kwargs):
        """Require business tools and reject any automatic code-execution tool."""
        exposed = {tool.name for tool in tools}
        assert {name + "_summary" for name in ASSIGNED}.issubset(exposed), exposed
        assert "verify_unassigned" in exposed
        assert not any(name.startswith("harnest_sandbox_") for name in exposed)
        assert "harnest_execute_python" not in exposed
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Run allowed tools sequentially and preserve every returned receipt."""
        results = {message.name: message.content for message in messages
                   if getattr(message, "type", None) == "tool"}
        expected = {name + "_summary" for name in ASSIGNED} | {"verify_unassigned"}
        missing = sorted(expected - results.keys())
        response = (
            AIMessage(content="", tool_calls=[{
                "name": missing[0], "args": {},
                "id": missing[0], "type": "tool_call",
            }]) if missing else AIMessage(content="completed:" + json.dumps(results))
        )
        return ChatResult(generations=[ChatGeneration(message=response)])


root_agent = Agent(name="named_probe", model=ProbeModel(), sandboxes=ASSIGNED)
'''


_PROVIDER = '''
import json
from dataclasses import asdict
from pathlib import Path
from harnest import Sandbox, SandboxResult


def record(value):
    """Journal only synthetic provider calls outside the compiled artifact."""
    with Path(JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\\n")


class Backend:
    """Return independent named receipts without evaluating host Python."""

    def execute(self, request):
        """Check immutable provider policy and managed invocation identity."""
        assert request.code == "print(42)"
        assert request.timeout_seconds == 8
        assert request.metadata["provider"] == NAME
        assert request.metadata["properties"]["region"] == "eu"
        assert request.context.agent_name == "named_probe"
        assert request.context.user_id == "_harnest_direct"
        assert request.context.session_id and request.context.invocation_id
        record({"event": "execute", "name": NAME, "context": asdict(request.context)})
        return SandboxResult(stdout="42", metadata={"receipt": NAME + "-receipt"})


def build():
    """Fail loudly if registry discovery accidentally grants unassigned access."""
    record({"event": "build", "name": NAME})
    assert NAME != "forbidden", "unassigned provider was constructed"
    return Backend()


EXPORT = Sandbox.provider(build, name=NAME, timeout_seconds=8,
    metadata={"provider": NAME, "properties": {"region": "eu"}})
'''


_BUSINESS_TOOL = '''
from harnest import context, tool
from harnest.sandbox_types import sandbox_metadata_to_dict


@tool
ASYNCdef EXPORT() -> dict:
    """Return a fixed synthetic business calculation, never model-supplied code."""
    result = AWAITcontext.sandboxes[NAME].METHOD("print(42)")
    return {"value": result.stdout, "metadata": sandbox_metadata_to_dict(result.metadata)}
'''


_DENIAL_TOOL = '''
from harnest import context, tool
from harnest.context import ContextResourceError


@tool
def verify_unassigned() -> dict:
    """Prove authored code cannot use an unassigned registry provider."""
    denied = []
    for name in DENIED:
        try:
            context.sandboxes[name].execute("print(42)")
        except ContextResourceError:
            denied.append(name)
    assert denied == DENIED, "unassigned provider execution was permitted"
    return {"denied": denied}
'''


_ADK_PARENT = '''


class ParentModel(BaseLlm):
    """Transfer to the child using ADK's real native delegation loop."""

    async def generate_content_async(self, request, stream=False):
        """Keep root capabilities distinct from the child executing tools."""
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(
                name="transfer_to_agent", args={"agent_name": "named_probe"},
            )),
        ]))


root_agent = Agent(name="sandbox_parent", model=ParentModel(model="offline-parent"),
    sandboxes=["calculations"], subagents=[root_agent])
'''


_LANGGRAPH_PARENT = '''
from harnest.graph import START, Edge, Graph


class ParentModel(BaseChatModel):
    """Finish the first graph node without invoking its sandbox capability."""

    @property
    def _llm_type(self):
        """Identify the parent graph model."""
        return "sandbox-parent-offline"

    def bind_tools(self, tools, **kwargs):
        """Accept ordinary framework tools without invoking them."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Let the graph hand control to its separately granted worker node."""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ready"))])


_parent = Agent(name="sandbox_parent", model=ParentModel(), sandboxes=["calculations"],
    instruction="Return ready.")
root_agent = Graph(name="sandbox_graph", nodes={"parent": _parent, "worker": root_agent},
    edges=(Edge(START, "parent"), Edge("parent", "worker")))
'''


class NamedSandboxFrameworkTests(unittest.TestCase):
    """Compile explicit capabilities and consume them only from authored tools."""

    def _write(self, path, source):
        """Author an isolated source tree rather than changing checked-in examples."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def _exercise(self, framework, assigned, child=False):
        """Assert lazy discovery, independent execution, and no implicit grants."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            journal = root / "provider.jsonl"
            model = _ADK_MODEL if framework == "adk" else _LANGGRAPH_MODEL
            if child:
                model = self._child_model(model, assigned, framework)
            self._write(source / "agent.py", model.replace("ASSIGNED", repr(assigned)))
            self._write(source / "instructions.md", "Use the authored business tools.")
            self._write(source / "agent-card.yaml", "name: Named sandbox probe\ndescription: Offline probe\nversion: 0.1.0\n")
            for name in ("calculations", "research", "forbidden"):
                provider = _PROVIDER.replace("NAME", repr(name)).replace("EXPORT", name)
                self._write(source / "sandbox" / f"{name}.py", provider.replace("JOURNAL", repr(str(journal))))
            if not child:
                self._write_business_tools(source, assigned)
            write_session_store(source)
            compile_artifact(source, root / "artifact", framework=framework)
            self.assertFalse(journal.exists(), "compilation constructed a provider")
            response = asyncio.run(run_agent_message(root / "artifact", "calculate"))
            records = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
            self.assertEqual({item["name"] for item in records}, set(assigned))
            self.assertEqual(len(records), 2 * len(assigned))
            self.assertIn("completed:", response["text"])
            self.assertIn("forbidden", response["text"])
            for name in assigned:
                self.assertIn(name + "-receipt", response["text"])
            return records

    def _write_business_tools(self, source, assigned):
        """Cover synchronous and asynchronous capability calls with fixed code."""
        for name in assigned:
            self._write(source / "tools" / f"{name}_summary.py", self._business_tool_source(name))
        denied = [name for name in ("calculations", "research", "forbidden") if name not in assigned]
        self._write(source / "tools" / "verify_unassigned.py", _DENIAL_TOOL.replace("DENIED", repr(denied)))

    def _business_tool_source(self, name):
        """Produce the same fixed business function for root and child authors."""
        async_mode = name == "research"
        tool = _BUSINESS_TOOL.replace("NAME", repr(name)).replace("EXPORT", name + "_summary")
        tool = tool.replace("ASYNC", "async " if async_mode else "")
        tool = tool.replace("AWAIT", "await " if async_mode else "")
        return tool.replace("METHOD", "aexecute" if async_mode else "execute")

    def _child_model(self, model, assigned, framework):
        """Attach authored child tools before adding a separately granted parent."""
        denied = [name for name in ("calculations", "research", "forbidden") if name not in assigned]
        tools = "\n".join(self._business_tool_source(name) for name in assigned)
        tools += _DENIAL_TOOL.replace("DENIED", repr(denied))
        names = [name + "_summary" for name in assigned] + ["verify_unassigned"]
        model = model.replace("sandboxes=ASSIGNED)",
            "sandboxes=ASSIGNED, instruction='Use authored business tools.', tools=[" + ",".join(names) + "])")
        parent = _ADK_PARENT if framework == "adk" else _LANGGRAPH_PARENT
        return tools + model + parent

    def test_adk_executes_only_explicitly_assigned_named_sandboxes(self):
        """ADK business tools preserve independent providers and metadata."""
        self._exercise("adk", ["calculations", "research"])

    def test_langgraph_executes_only_explicitly_assigned_named_sandboxes(self):
        """LangGraph business tools preserve independent providers and metadata."""
        self._exercise("langgraph", ["calculations", "research"])

    def test_adk_registry_without_assignment_exposes_no_sandbox(self):
        """A populated registry alone grants no ADK provider access."""
        self.assertEqual(self._exercise("adk", []), [])

    def test_langgraph_registry_without_assignment_exposes_no_sandbox(self):
        """A populated registry alone grants no LangGraph provider access."""
        self.assertEqual(self._exercise("langgraph", []), [])

    def test_adk_child_tool_uses_own_grant_and_denies_parent_grant(self):
        """Native ADK transfer cannot let a child borrow its parent's sandbox."""
        self._exercise("adk", ["research"], child=True)

    def test_adk_child_without_grants_cannot_borrow_parent_sandbox(self):
        """Native child tools with no grants are denied before provider startup."""
        self.assertEqual(self._exercise("adk", [], child=True), [])

    def test_langgraph_worker_tool_uses_own_grant_and_denies_other_node(self):
        """A graph node cannot borrow the preceding agent node's capability."""
        self._exercise("langgraph", ["research"], child=True)

    def test_langgraph_worker_without_grants_cannot_borrow_other_node(self):
        """An unassigned graph worker cannot inherit another node's sandbox."""
        self.assertEqual(self._exercise("langgraph", [], child=True), [])

if __name__ == "__main__":
    unittest.main()

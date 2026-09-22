"""Native model-loop integration for opt-in skill selection on ADK and LangGraph."""

import unittest

from google.adk.apps import App
from google.adk.models import LlmResponse
from google.genai import types
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from harnest.agent import Agent
from harnest.application import CompiledApplication
from harnest.backends.langgraph import lower_agent
from harnest.context import ContextValue
from harnest.decisions import Decisions
from harnest.lifecycle_runtime import LifecycleRuntimeDriver
from harnest.runtime_adk import ADKRuntimeDriver
from harnest.runtime_langgraph import LangGraphRuntimeDriver
from harnest.runtime_contract import InvocationRequest
from harnest.skills import DecisionSkillSelector, SkillContext, SkillRegistry, SkillScope
from test_skill_selection import Provider, Source
from test_token_policy_backends import RecordingADK, RecordingLangGraph, lookup


class TwoPassADK(RecordingADK):
    """Exercise a real tool round trip before finishing the model response."""

    async def generate_content_async(self, llm_request, stream=False):
        """Return a tool call once and then a final native response."""
        self.requests.append(llm_request)
        part = types.Part(text="ok") if len(self.requests) > 1 else types.Part(
            function_call=types.FunctionCall(name="lookup", args={}))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class CompiledSkillADK(RecordingADK):
    """Reject compiled requests that omit the selected filesystem skill."""

    async def generate_content_async(self, llm_request, stream=False):
        """Check the actual model boundary after artifact reload and runtime selection."""
        assert "Use this skill for triage." in str(llm_request.config.system_instruction)
        async for response in super().generate_content_async(llm_request, stream):
            yield response


class CompiledSkillLangGraph(RecordingLangGraph):
    """Reject compiled requests that omit the selected filesystem skill."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Check native messages without relying on compiler internals."""
        assert "Use this skill for triage." in str(messages[0])
        return super()._generate(messages, stop, run_manager, **kwargs)


def driver_for(framework, selector, source, provider, *, loop=False):
    """Use real framework models and runtime scopes with an offline decision provider."""
    registry = SkillRegistry({"root": SkillScope({"remote": source})})
    if framework == "adk":
        model = TwoPassADK(model="test") if loop else RecordingADK(model="test")
        target = Agent(name="root", model=model, instruction="Base instruction", skill_selection=selector, tools=[lookup] if loop else []).build()
        application = CompiledApplication(name="root", framework=framework, mode="managed", target=target,
                                          native_app=App(name="root", root_agent=target))
        native = ADKRuntimeDriver(application)
    else:
        responses = [AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": "call"}])] if loop else []
        model = RecordingLangGraph(responses=[*responses, AIMessage(content="ok")])
        target = lower_agent(Agent(name="root", model=model, instruction="Base instruction", skill_selection=selector, tools=[lookup] if loop else []),
                             checkpointer=InMemorySaver())
        application = CompiledApplication(name="root", framework=framework, mode="managed", target=target)
        native = LangGraphRuntimeDriver(application)
    decisions = Decisions(providers={"test": provider}, bindings=())
    driver = LifecycleRuntimeDriver(native, [], skill_registry=registry,
                                    context_values=(ContextValue("decisions", decisions, "test"),))
    return driver, model


class SkillSelectionBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_native_requests_include_selected_bodies_without_persisting_them(self):
        """Both serving paths select again per turn, preserve history and send only current task."""
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework):
                await self._exercise(framework)

    async def _exercise(self, framework):
        """Observe actual model payloads and stored sessions across two invocations."""
        source, provider = Source(), Provider()
        driver, model = driver_for(framework, DecisionSkillSelector(max_skills=1, fallback=DecisionSkillSelector.ERROR), source, provider)
        try:
            await driver.create_session(session_id="s", user_id="u", state={"private": "not sent"})
            for message in ("old task", "current task"):
                request = InvocationRequest(input=message, session_id="s", user_id="u", invocation_id=message, metadata={}, state_delta={})
                await driver.invoke(request)
            self.assertEqual(len(provider.requests), 2)
            self.assertEqual(dict(provider.requests[-1].state["input"]), {"task": "current task"})
            visible = str(model.requests[-1].config.system_instruction) if framework == "adk" else str(model.requests[-1][0])
            self.assertIn("Base instruction", visible)
            self.assertIn("PRIVATE BODY triage", visible)
            self.assertNotIn("PRIVATE BODY authentication", visible)
            transcript = await driver.get_session_messages(session_id="s", user_id="u")
            self.assertNotIn("PRIVATE BODY", str(transcript))
            self.assertIn("old task", str(transcript))
        finally:
            await driver.close()

    async def test_native_callbacks_receive_existing_skill_context(self):
        """Framework adapters provide task and state without inventing another context type."""
        for framework in ("adk", "langgraph"):
            seen = []
            def input_for(context):
                """Opt into task text while checking that callback scope matches the executing agent."""
                self.assertIsInstance(context, SkillContext)
                seen.append((context.framework, context.agent_name, context.task, context.state))
                return {"task": context.task}
            source, provider = Source(), Provider()
            driver, _ = driver_for(framework, DecisionSkillSelector(input=input_for, fallback=DecisionSkillSelector.ERROR), source, provider)
            try:
                await driver.create_session(session_id="s", user_id="u", state={"product": "api"})
                await driver.invoke(InvocationRequest(input="help", session_id="s", user_id="u", invocation_id="i", metadata={}, state_delta={}))
                self.assertEqual(seen[0][:3], (framework, "root", "help"))
                self.assertNotIn("messages", seen[0][3])
                self.assertEqual(seen[0][3]["product"], "api")
            finally:
                await driver.close()

    async def test_tool_loop_reuses_selection_and_injects_once_per_model_request(self):
        """Repeated model calls reuse loaded bodies without repeated decisions or prompt duplication."""
        for framework in ("adk", "langgraph"):
            source, provider = Source(), Provider()
            driver, model = driver_for(framework, DecisionSkillSelector(max_skills=1), source, provider, loop=True)
            try:
                await driver.create_session(session_id="s", user_id="u", state={})
                await driver.invoke(InvocationRequest(input="help", session_id="s", user_id="u", invocation_id="i", metadata={}, state_delta={}))
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(len(source.loads), 1)
                self.assertEqual(len(model.requests), 2)
                for request in model.requests:
                    text = str(request.config.system_instruction) if framework == "adk" else str(request[0])
                    self.assertEqual(text.count("PRIVATE BODY triage"), 1)
            finally:
                await driver.close()

    async def test_disabled_selector_keeps_existing_models_unchanged(self):
        """Default agent configuration performs no skill source or decision work."""
        for framework in ("adk", "langgraph"):
            source, provider = Source(), Provider()
            driver, _ = driver_for(framework, None, source, provider)
            try:
                await driver.create_session(session_id="s", user_id="u", state={})
                await driver.invoke(InvocationRequest(input="help", session_id="s", user_id="u", invocation_id="i", metadata={}, state_delta={}))
                self.assertEqual(source.lists, [])
                self.assertEqual(provider.requests, [])
            finally:
                await driver.close()


class SkillSelectionCompilerTests(unittest.TestCase):
    def test_artifact_discovers_skill_files_and_preserves_selector(self):
        """Compile lazily, then select real SKILL.md files through the served artifact."""
        from pathlib import Path
        import tempfile
        from harnest.bundle import compile_artifact
        from harnest.runtime import create_fastapi_app
        from fastapi.testclient import TestClient
        from test_decision_frameworks import write
        from _session_store_fixture import write_session_store

        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as directory:
                root, artifact = Path(directory) / "agent", Path(directory) / "compiled"
                journal = Path(directory) / "selected"
                model = "CompiledSkillADK(model='test')" if framework == "adk" else "CompiledSkillLangGraph(responses=[AIMessage(content='ok')])"
                write(root / "agent.py", f'''
                    from harnest.agent import Agent
                    from harnest.skills import DecisionSkillSelector, SkillContext
                    from test_skill_selection_backends import CompiledSkillADK, CompiledSkillLangGraph
                    from langchain_core.messages import AIMessage
                    def selection_input(context: SkillContext):
                        return {{"task": context.task}}
                    root_agent = Agent(name="root", model={model},
                        skill_selection=DecisionSkillSelector(input=selection_input, fallback=DecisionSkillSelector.ERROR))
                ''')
                write(root / "lifecycle/decisions.py", f'''
                    from contextlib import contextmanager
                    from pathlib import Path
                    from harnest import context, lifecycle
                    from harnest.decisions import Decisions
                    from test_skill_selection import Provider

                    class TrackedProvider(Provider):
                        async def evaluate(self, request):
                            assert dict(request.state["input"]) == {{"task": "help me"}}
                            assert "Handle support incidents." in request.definition.questions[0].instructions
                            Path({str(journal)!r}).write_text("evaluated")
                            return await super().evaluate(request)

                    @lifecycle.resource
                    @context.provider("decisions")
                    @contextmanager
                    def decisions():
                        yield Decisions(providers={{"test": TrackedProvider()}}, bindings=())
                ''')
                write(root / "instructions.md", "Use the right skill.")
                write(root / "agent-card.yaml", "name: Root\ndescription: Skill selection test\n")
                write(root / "skills" / "triage" / "SKILL.md", "---\nname: triage\ndescription: Handle support incidents.\n---\nUse this skill for triage.\n")
                write_session_store(root)
                # Compilation must retain configuration without evaluating any
                # provider or resolving application-owned runtime resources.
                compile_artifact(root, artifact, framework=framework)
                self.assertFalse(journal.exists())
                self.assertTrue((artifact / "source/skills/triage/SKILL.md").is_file())
                with TestClient(create_fastapi_app(artifact)) as client:
                    self.assertEqual(client.get("/agent").status_code, 200)
                    session = client.post("/sessions", json={})
                    self.assertEqual(session.status_code, 201, session.text)
                    response = client.post("/responses", json={"input": "help me", "sessionId": session.json()["id"]})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["outputText"], "ok")
                self.assertEqual(journal.read_text(), "evaluated")

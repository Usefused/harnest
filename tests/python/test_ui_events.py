"""Authored UI event validation, transport projection, and streaming lifetime."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from harnest import context, ui
from harnest.bundle import compile_artifact
from harnest.context import activate_agent_scope, ContextUnavailableError
from harnest.lifecycle_runtime import LifecycleRuntimeDriver
from harnest.runtime import create_fastapi_app
from harnest.runtime_agui import _AGUIEncoder
from harnest.runtime_contract import InvocationResult
from harnest.runtime_continuation import public_output
from harnest.runtime_sse import stream_frame
from test_lifecycle_runtime import FakeDriver, request
from _session_store_fixture import write_session_store


class UIDriver(FakeDriver):
    async def invoke(self, request):
        """Emit display data from a child scope without mutating the model response."""
        value = {"message":request.invocation_id}
        with activate_agent_scope("child"):
            await ui.emit("app.progress", value)
        value["message"] = "mutated"
        return InvocationResult(text="done", events=({"type":"message", "text":"done"},), result=None, session_id=request.session_id, metadata={})

    async def stream(self, request):
        """Pause after a UI event to prove it arrives before backend execution completes."""
        try:
            await ui.emit("app.progress", {"message":request.invocation_id})
            await asyncio.sleep(10)
            yield {"type":"message", "text":"done"}
        finally:
            self.closed = True


class UIEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_detached_output_and_transport_projection(self):
        """Keep events isolated and public while preserving their exact AG-UI name/value."""
        runtime = LifecycleRuntimeDriver(UIDriver(), ())
        self.addAsyncCleanup(runtime.close)
        first, second = await asyncio.gather(runtime.invoke(request()), runtime.invoke(replace(request(), invocation_id="other", session_id="other")))
        event = first.events[0]
        self.assertEqual(event["value"], {"message":"invoke-1"})
        self.assertEqual(event["agent"], "child")
        self.assertEqual(second.events[0]["value"], {"message":"other"})
        self.assertEqual(public_output(first.events)[0]["type"], "ui_event")
        name, frame = stream_frame(event, sequence=1, response_id="response", session_id="session")
        self.assertEqual(name, "response.ui_event")
        wire = _AGUIEncoder(run_id="run").encode(name, frame)
        self.assertIn('"type": "CUSTOM"', wire)
        self.assertIn('"name": "app.progress"', wire)
        with self.assertRaises(ContextUnavailableError):
            await ui.emit("app.orphan", {})

    async def test_stream_arrives_during_tool_wait_and_disconnect_closes_backend(self):
        """No active agent context leaks to the consumer and cancellation owns its producer."""
        driver = UIDriver()
        runtime = LifecycleRuntimeDriver(driver, ())
        self.addAsyncCleanup(runtime.close)
        source = runtime.stream(request())
        event = await asyncio.wait_for(anext(source), .5)
        self.assertEqual(event["name"], "app.progress")
        with self.assertRaises(ContextUnavailableError):
            context.current()
        await source.aclose()
        self.assertTrue(driver.closed)

    async def test_json_reserved_names_and_budgets_fail_without_echoing_payloads(self):
        """Prevent custom display events from spoofing control events or exhausting buffers."""
        from harnest.ui_output import UIOutput
        output = UIOutput()
        invalid = [("harnest.client_tool", {}), ("thinking", {}), ("bad name", {}), ("app.a", float("nan")), ("app.a", {"secret":set()}), ("app.a", "private" * 10000)]
        for name, value in invalid:
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                await output.emit(name, value, agent="root")
            self.assertNotIn("private", str(caught.exception))
        for _ in range(128):
            await output.emit("app.a", {}, agent="root")
        with self.assertRaises(ValueError):
            await output.emit("app.a", {}, agent="root")


class CompiledUIEventTests(unittest.TestCase):
    def test_authored_events_and_state_reach_agui_on_both_frameworks(self):
        """Compile public authoring code and exercise real AG-UI/native HTTP serialization."""
        source = '''from harnest import ui
from harnest.graph import START, Edge, Event, Graph

async def respond(value):
    """Emit display content separately from authored session state and model output."""
    await ui.emit("app.card", {"title":"<script>inert</script>", "items":[1,2]})
    return Event(message="done", state_delta={"progress":1})

root_agent = Graph(name="ui_test", nodes={"respond":respond}, edges=(Edge(START,"respond"),))
'''
        for framework in ("adk", "langgraph"):
            with self.subTest(framework=framework), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "source"
                root.mkdir()
                (root / "agent.py").write_text(source)
                (root / "instructions.md").write_text("Return the graph result.")
                (root / "agent-card.yaml").write_text("name: UI Test\ndescription: Offline custom event fixture.\nversion: 1.0.0\n")
                write_session_store(root)
                artifact = Path(directory) / "compiled"
                compile_artifact(root, artifact, framework=framework)
                with TestClient(create_fastapi_app(artifact)) as client:
                    response = client.post("/agui", json={"messages":[{"id":"one", "role":"user", "content":"show"}]})
                    self.assertEqual(response.status_code, 200, response.text)
                    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                    custom = [item for item in events if item.get("name") == "app.card"]
                    self.assertEqual(len(custom), 1)
                    self.assertEqual(custom[0]["value"]["items"], [1,2])
                    self.assertTrue(any(item["type"] == "STATE_DELTA" for item in events))
                    result = client.post("/responses", json={"input":"show"}).json()
                    self.assertEqual(len([item for item in result["output"] if item["type"] == "ui_event"]), 1)

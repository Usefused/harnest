import types as python_types
import unittest

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from harnest.application import CompiledApplication
from harnest.neutral_runtime import (
    InvocationRequest,
    RuntimeDriver,
    SessionConflictError,
)
from harnest.output import OutputPolicy
from harnest.runtime_adk import ADKRuntimeDriver, _ADKEventNormalizer


class DeterministicLlm(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        del llm_request, stream
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part(text="deterministic response")]
            )
        )


def _application() -> CompiledApplication:
    agent = LlmAgent(
        name="root",
        description="Deterministic test agent",
        model=DeterministicLlm(model="deterministic"),
        instruction="Answer deterministically.",
    )
    app = App(name="root", root_agent=agent)
    return CompiledApplication(
        name="root",
        framework="adk",
        mode="managed",
        target=agent,
        native_app=app,
    )


def _request(session_id: str, *, invocation_id: str = "invocation-1"):
    return InvocationRequest(
        input="private prompt",
        user_id="test-user",
        session_id=session_id,
        invocation_id=invocation_id,
        metadata={"request_kind": "test"},
        state_delta={},
    )


class ADKRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = ADKRuntimeDriver(
            _application(),
            card={"name": "Test card", "description": "Card description"},
            extra_endpoints={"native": "/run"},
        )

    async def asyncTearDown(self):
        await self.driver.close()

    async def test_implements_contract_and_owns_session_crud(self):
        self.assertIsInstance(self.driver, RuntimeDriver)
        self.assertEqual(self.driver.info.name, "Test card")
        self.assertEqual(self.driver.info.framework, "adk")
        self.assertEqual(self.driver.info.extra_endpoints, {"native": "/run"})

        created = await self.driver.create_session(
            session_id="session-1",
            user_id="test-user",
            state={"count": 1},
        )
        self.assertEqual(created.id, "session-1")
        self.assertEqual(created.user_id, "test-user")
        self.assertEqual(created.state, {"count": 1})

        with self.assertRaises(SessionConflictError):
            await self.driver.create_session(
                session_id="session-1",
                user_id="test-user",
                state={},
            )

        updated = await self.driver.update_session(
            session_id="session-1",
            user_id="test-user",
            state_delta={"count": 2, "ready": True},
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, {"count": 2, "ready": True})
        listed = await self.driver.list_sessions(user_id="test-user")
        self.assertEqual([session.id for session in listed], ["session-1"])

        self.assertTrue(
            await self.driver.delete_session(
                session_id="session-1", user_id="test-user"
            )
        )
        self.assertFalse(
            await self.driver.delete_session(
                session_id="session-1", user_id="test-user"
            )
        )
        self.assertIsNone(
            await self.driver.get_session(
                session_id="session-1", user_id="test-user"
            )
        )

    async def test_injected_adk_session_service_is_used_by_runner(self):
        service = InMemorySessionService()
        driver = ADKRuntimeDriver(_application(), session_service=service)
        try:
            await driver.create_session(
                session_id="injected", user_id="test-user", state={"ready": True}
            )
            stored = await service.get_session(
                app_name="root", user_id="test-user", session_id="injected"
            )
            self.assertEqual(stored.state, {"ready": True})
        finally:
            await driver.close()

    async def test_invoke_and_stream_use_the_same_normalized_events(self):
        await self.driver.create_session(
            session_id="invoke-session", user_id="test-user", state={}
        )
        result = await self.driver.invoke(_request("invoke-session"))
        self.assertEqual(result.text, "deterministic response")
        self.assertEqual(result.session_id, "invoke-session")
        self.assertEqual(result.metadata, {"request_kind": "test"})
        self.assertEqual(
            [event["type"] for event in result.events], ["message"]
        )

        await self.driver.create_session(
            session_id="stream-session", user_id="test-user", state={}
        )
        events = [
            event
            async for event in self.driver.stream(
                _request("stream-session", invocation_id="invocation-2")
            )
        ]
        self.assertEqual(
            events,
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "text": "deterministic response",
                }
            ],
        )

    async def test_close_is_idempotent_and_rejects_new_work(self):
        await self.driver.close()
        await self.driver.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.driver.list_sessions(user_id="test-user")

    async def test_normalizer_filters_thoughts_and_preserves_tool_trace(self):
        event = python_types.SimpleNamespace(
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[
                    python_types.SimpleNamespace(text="hidden", thought=True),
                    python_types.SimpleNamespace(text="visible", thought=False),
                ]
            ),
            output={"route": "done"},
            node_info=None,
            get_function_calls=lambda: [
                python_types.SimpleNamespace(
                    id="call-1", name="lookup", args={"query": "safe"}
                )
            ],
            get_function_responses=lambda: [
                python_types.SimpleNamespace(
                    id="call-1", name="lookup", response={"value": 3}
                )
            ],
        )
        self.assertEqual(
            _ADKEventNormalizer().feed(event),
            [
                {"type": "message", "role": "assistant", "text": "visible"},
                {
                    "type": "graph_output",
                    "output": {"route": "done"},
                    "result": {"route": "done"},
                },
                {
                    "type": "tool_call",
                    "id": "call-1",
                    "name": "lookup",
                    "arguments": {"query": "safe"},
                },
                {
                    "type": "tool_result",
                    "id": "call-1",
                    "name": "lookup",
                    "result": {"value": 3},
                },
            ],
        )
        thought_only = python_types.SimpleNamespace(
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="private", thought=True)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )
        self.assertEqual(_ADKEventNormalizer().feed(thought_only), [])

    async def test_subagent_pre_tool_narration_is_configurable(self):
        partial = python_types.SimpleNamespace(
            author="researcher",
            partial=True,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="I'll ", thought=False)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )
        completed = python_types.SimpleNamespace(
            author="researcher",
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[
                    python_types.SimpleNamespace(
                        text="I'll inspect the page", thought=False
                    )
                ]
            ),
            output=None,
            get_function_calls=lambda: [
                python_types.SimpleNamespace(
                    id="call-1", name="inspect", args={"selector": "main"}
                )
            ],
            get_function_responses=lambda: [],
        )
        canonical = python_types.SimpleNamespace(
            author="root",
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="Done", thought=False)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )

        suppressed = _ADKEventNormalizer(root_agent_name="root")
        suppressed_events = [
            *suppressed.feed(partial),
            *suppressed.feed(completed),
            *suppressed.feed(canonical),
        ]
        included = _ADKEventNormalizer(
            OutputPolicy(subagent_messages="include"), root_agent_name="root"
        )
        included_events = [
            *included.feed(partial),
            *included.feed(completed),
            *included.feed(canonical),
        ]

        self.assertEqual(
            [event["type"] for event in suppressed_events], ["tool_call", "message"]
        )
        self.assertEqual(suppressed_events[-1]["text"], "Done")
        self.assertEqual(
            "".join(
                event["text"]
                for event in included_events
                if event["type"] == "message"
            ),
            "I'll inspect the pageDone",
        )
        self.assertIn("tool_call", [event["type"] for event in included_events])

        terminal_child = _ADKEventNormalizer(root_agent_name="root")
        terminal = python_types.SimpleNamespace(
            author="researcher",
            partial=False,
            content=python_types.SimpleNamespace(
                parts=[python_types.SimpleNamespace(text="Final answer", thought=False)]
            ),
            output=None,
            get_function_calls=lambda: [],
            get_function_responses=lambda: [],
        )
        self.assertEqual(terminal_child.feed(terminal), [])
        self.assertEqual(
            terminal_child.finish(),
            [{"type": "message", "role": "assistant", "text": "Final answer"}],
        )


if __name__ == "__main__":
    unittest.main()

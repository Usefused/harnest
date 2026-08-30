from __future__ import annotations

import asyncio
import types as python_types
import unittest
from typing import Annotated
from unittest.mock import patch

from google.adk.events import Event
from google.adk.sessions import Session
from google.genai import types
from pydantic import BaseModel

from harnest.approval import ApprovalRun
from harnest.assets import AssetMediaMetadata
from harnest.client_tool import (
    ClientToolExecution,
    InMemoryClientToolStore,
    client_tool_execution,
    current_transient_media,
)
from harnest.content import Image, ImageConstraints
from harnest.runtime_adk import (
    _ADKEventNormalizer,
    _adk_session_messages,
    _asset_content_plugin,
    _session_record,
)
from harnest.tool import client_tool
from harnest.transient_media import (
    TransientMediaAccess,
    TransientMediaLeaseStore,
    TransientMediaScope,
)


_PRIVATE_BASE64 = "cHJpdmF0ZS1zY3JlZW5zaG90"
_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class _CaptureResult(BaseModel):
    screenshot: Annotated[Image, ImageConstraints(max_bytes=100)]


class TransientMediaPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = TransientMediaScope("user-a", "session-a", "call-a")
        self.store = TransientMediaLeaseStore(max_total_bytes=1_000)
        self.lease = self.store.stage(
            scope=self.scope,
            kind="image",
            media_type="image/jpeg",
            data=b"private-screenshot",
            metadata=AssetMediaMetadata(width=1, height=1),
        )
        self.access = TransientMediaAccess(self.store, self.scope)
        self.marker = {
            "type": "image",
            "mediaType": "image/jpeg",
            "content": "attached",
        }

    async def asyncSetUp(self) -> None:
        """Bind media inside the test task on every supported Python version."""

        self.access.bind((self.lease.lease_id,))

    def assert_private_values_absent(self, value: object) -> None:
        rendered = repr(value)
        self.assertNotIn(_PRIVATE_BASE64, rendered)
        self.assertNotIn(self.lease.lease_id, rendered)
        self.assertNotIn("harnestTransient", rendered)
        self.assertNotIn("leaseId", rendered)

    def event(self, *, output: object | None = None) -> Event:
        return Event(
            id="event-a",
            invocationId=self.scope.call_id,
            author="vision_child",
            content=types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="tool-a",
                            name="capture",
                            response={
                                "screenshot": self.marker,
                                "unrelated": "safe",
                            },
                        )
                    )
                ],
            ),
            output=output,
        )

    def test_public_tool_and_graph_events_never_expose_private_markers(self):
        event = self.event(output={"nested": self.marker})

        public = _ADKEventNormalizer().feed(event)

        self.assert_private_values_absent(public)
        tool_result = next(item for item in public if item["type"] == "tool_result")
        self.assertEqual(
            tool_result["result"]["screenshot"],
            {
                "type": "image",
                "mediaType": "image/jpeg",
                "content": "attached",
            },
        )
        graph_output = next(
            item for item in public if item["type"] == "graph_output"
        )
        self.assertEqual(graph_output["output"]["nested"]["content"], "attached")

    def test_messages_session_state_and_native_metadata_are_private(self):
        event = self.event()
        # Exercise defense in depth for an authored state projection containing
        # the same private marker; normal continuation state should never do so.
        session = Session(
            id=self.scope.session_id,
            appName="privacy",
            userId=self.scope.user_id,
            state={"lastCapture": self.marker},
            events=[event],
            lastUpdateTime=1.0,
        )

        messages = _adk_session_messages(session)
        record = _session_record(session)

        self.assert_private_values_absent(messages)
        self.assert_private_values_absent(record)
        self.assertEqual(
            messages[0].content["screenshot"]["content"], "attached"
        )
        self.assertEqual(record.state["lastCapture"]["content"], "attached")

    async def test_committed_stale_marker_is_sanitized_but_never_reattached(self):
        original = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="tool-a",
                        name="capture",
                        response={"screenshot": self.marker},
                    )
                )
            ],
        )
        callback = python_types.SimpleNamespace(
            user_id=self.scope.user_id,
            session=python_types.SimpleNamespace(id=self.scope.session_id),
            invocation_id=self.scope.call_id,
            agent_name="vision_child",
            branch="root.vision_child",
            node_path="root/vision_child",
        )
        plugin = _asset_content_plugin(None)
        first = python_types.SimpleNamespace(contents=[original])

        with patch(
            "harnest.runtime_adk.current_transient_media", return_value=self.access
        ):
            await plugin.before_model_callback(
                callback_context=callback, llm_request=first
            )
            await plugin.after_model_callback(
                callback_context=callback,
                llm_response=python_types.SimpleNamespace(),
            )
            next_turn = types.Content(
                role="user", parts=[types.Part(text="next turn")]
            )
            stale = python_types.SimpleNamespace(contents=[original, next_turn])
            await plugin.before_model_callback(
                callback_context=callback, llm_request=stale
            )

        self.assertIsNone(self.access.peek(self.lease.lease_id))
        self.assert_private_values_absent(stale.contents)
        self.assertEqual([len(content.parts) for content in stale.contents], [1, 1])
        response = stale.contents[0].parts[0].function_response.response
        self.assertEqual(response["screenshot"]["content"], "attached")

    async def test_adk_tool_binding_survives_callback_context_handoff(self):
        callback = python_types.SimpleNamespace(
            user_id=self.scope.user_id,
            session=python_types.SimpleNamespace(id=self.scope.session_id),
            invocation_id=self.scope.call_id,
            agent_name="vision_child",
            branch="root.vision_child",
            node_path="root/vision_child",
        )
        original = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="tool-a",
                        name="capture",
                        response={"screenshot": self.marker},
                    )
                )
            ],
        )
        plugin = _asset_content_plugin(None)
        with patch(
            "harnest.runtime_adk.current_transient_media", return_value=self.access
        ):
            await plugin.after_tool_callback(
                tool=python_types.SimpleNamespace(name="capture"),
                tool_args={},
                tool_context=callback,
                result={"screenshot": self.marker},
            )

        request = python_types.SimpleNamespace(contents=[original])
        with patch("harnest.runtime_adk.current_transient_media", return_value=None):
            await plugin.before_model_callback(
                callback_context=callback, llm_request=request
            )

        self.assertEqual(
            request.contents[0].parts[-1].inline_data.data,
            b"private-screenshot",
        )
        self.assertNotIn(self.lease.lease_id, repr(original))
        await plugin.after_model_callback(
            callback_context=callback,
            llm_response=python_types.SimpleNamespace(),
        )
        self.assertIsNone(self.access.peek(self.lease.lease_id))

    async def test_identical_parallel_branches_cannot_cross_attach(self):
        client_tools = InMemoryClientToolStore()
        scope = TransientMediaScope("user-a", "session-a", "parallel-call")
        first = client_tools.transient_media.stage(
            scope=scope,
            kind="image",
            media_type="image/png",
            data=b"branch-one",
            metadata=AssetMediaMetadata(width=1, height=1),
        )
        second = client_tools.transient_media.stage(
            scope=scope,
            kind="image",
            media_type="image/png",
            data=b"branch-two",
            metadata=AssetMediaMetadata(width=1, height=1),
        )
        placeholder = {
            "type": "image",
            "mediaType": "image/png",
            "content": "attached",
        }
        plugin = _asset_content_plugin(None)
        ready: asyncio.Queue[None] = asyncio.Queue()
        release = asyncio.Event()

        async def branch(lease, name: str) -> bytes:
            run = ApprovalRun("run-a", scope.user_id, scope.session_id, scope.call_id)
            execution = ClientToolExecution(client_tools, run)
            callback = python_types.SimpleNamespace(
                user_id=scope.user_id,
                session=python_types.SimpleNamespace(id=scope.session_id),
                invocation_id=scope.call_id,
                agent_name=name,
                branch=f"root.{name}",
                node_path=f"root/{name}",
            )
            request = python_types.SimpleNamespace(
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=f"tool-{name}",
                                    name="capture",
                                    response={"screenshot": placeholder},
                                )
                            )
                        ],
                    )
                ]
            )
            with client_tool_execution(execution):
                access = current_transient_media()
                assert access is not None
                access.bind((lease.lease_id,))
                ready.put_nowait(None)
                await release.wait()
                await plugin.before_model_callback(
                    callback_context=callback, llm_request=request
                )
                attached = [
                    part.inline_data.data
                    for content in request.contents
                    for part in content.parts
                    if part.inline_data is not None
                ]
                await plugin.after_model_callback(
                    callback_context=callback,
                    llm_response=python_types.SimpleNamespace(),
                )
                self.assertEqual(len(attached), 1)
                return attached[0]

        branches = [
            asyncio.create_task(branch(first, "vision_one")),
            asyncio.create_task(branch(second, "vision_two")),
        ]
        await ready.get()
        await ready.get()
        release.set()

        self.assertEqual(await asyncio.gather(*branches), [b"branch-one", b"branch-two"])
        self.assertEqual(client_tools.transient_media.total_bytes, 0)

    async def test_client_tool_audit_logs_never_include_media_or_lease_id(self):
        @client_tool(output_schema=_CaptureResult)
        def capture() -> _CaptureResult:
            """Capture one private screenshot."""

            raise AssertionError("client tool declarations do not execute")

        client_tools = InMemoryClientToolStore()
        run = ApprovalRun("run-a", "user-a", "session-a", "call-a")

        async def invoke() -> object:
            with client_tool_execution(ClientToolExecution(client_tools, run)):
                return await capture()

        task = asyncio.create_task(invoke())
        run.task = task
        _, pending = await run.notifications.get()
        with self.assertLogs(
            "harnest.agent.client_tool.audit", level="INFO"
        ) as captured:
            await client_tools.submit(
                pending.id,
                user_id=run.user_id,
                output={
                    "screenshot": {
                        "data": _PNG_BASE64,
                        "mediaType": "image/png",
                    }
                },
            )
        result = await task
        logs = "\n".join(captured.output)

        self.assertNotIn(_PNG_BASE64, logs)
        self.assertNotIn("transient_media_", logs)
        self.assertNotIn("harnestTransient", repr(result))
        self.assertNotIn("leaseId", repr(result))
        self.assertEqual(client_tools.transient_media.total_bytes, 0)

    def test_private_repr_and_errors_do_not_echo_bytes_or_identifiers(self):
        self.assert_private_values_absent(self.scope)
        self.assertNotIn(self.lease.lease_id, repr(self.lease))
        self.assertNotIn("private-screenshot", repr(self.lease))

        other = TransientMediaAccess(
            self.store,
            TransientMediaScope("user-b", self.scope.session_id, self.scope.call_id),
        )
        with self.assertRaisesRegex(ValueError, "positive integer") as raised:
            TransientMediaLeaseStore(max_total_bytes=0)
        self.assert_private_values_absent(raised.exception)
        self.assertIsNone(other.peek(self.lease.lease_id))


if __name__ == "__main__":
    unittest.main()

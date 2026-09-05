import base64
import json
import sys
import unittest
from dataclasses import dataclass, replace
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import BaseModel

from harnest.agent import Agent, AgentDefinition
from harnest.approval import ApprovalPolicy
from harnest.application import CompiledApplication
from harnest.assets import AssetMediaMetadata, AssetScope, MemoryAssetStore
from harnest.backends.langgraph import ManagedAgentPlan, ManagedGraphPlan
from harnest.content import ContentPart, Image, Text
from harnest.context import context
from harnest.checkpoint_langgraph import _decode_thread_id
from harnest.durable import NativeDurableSuspended
from harnest.graph import START, Edge, Graph
from harnest.lifecycle import LifecycleListener
from harnest.mcp_context import MCPContextUnavailableError
from harnest.mcp import MCPClient
from harnest.mcp_lifecycle import (
    MCPClientContext,
    MCPClientLifecycle,
    MCPHTTPClientOptions,
)
from harnest.neutral_runtime import InvocationRequest, SessionConflictError
from harnest.output import OutputPolicy
from harnest.runtime_langgraph import (
    LangGraphRuntimeDriver,
    _MODEL_ASSET_SCOPE,
    _langgraph_asset_middleware,
    _message_thinking,
    _message_tool_events,
)
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.session import InMemorySessionStore
from harnest.transient_media import (
    TransientMediaAccess,
    TransientMediaLeaseStore,
    TransientMediaScope,
)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _RecordingSessionStore(InMemorySessionStore):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def close(self):
        self.closed = True
        await super().close()


class _Message:
    def __init__(
        self,
        content,
        *,
        message_type="ai",
        tool_calls=(),
        tool_call_id=None,
        name=None,
        additional_kwargs=None,
        response_metadata=None,
        usage_metadata=None,
    ):
        self.content = content
        self.type = message_type
        self.tool_calls = list(tool_calls)
        self.tool_call_id = tool_call_id
        self.name = name
        self.additional_kwargs = dict(additional_kwargs or {})
        self.response_metadata = dict(response_metadata or {})
        self.usage_metadata = dict(usage_metadata or {})

    def model_dump(self, **_kwargs):
        return {
            "type": self.type,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "additional_kwargs": self.additional_kwargs,
            "response_metadata": self.response_metadata,
            "usage_metadata": self.usage_metadata,
        }


class _HumanMessage:
    type = "human"

    def __init__(self, content):
        self.content = content

    def model_dump(self, **_kwargs):
        return {"type": self.type, "content": self.content}


class _CopyMessage(_HumanMessage):
    def model_copy(self, *, update):
        return _CopyMessage(update.get("content", self.content))


class _ToolMessage:
    """Model the LangChain envelope needed by native ToolCall recovery tests."""

    type = "tool"

    def __init__(self, content, *, tool_call_id, name, status) -> None:
        self.content = content
        self.tool_call_id = tool_call_id
        self.name = name
        self.status = status

    def model_copy(self, *, update):
        """Preserve the test envelope while applying framework identity updates."""

        return _ToolMessage(
            update.get("content", self.content),
            tool_call_id=update.get("tool_call_id", self.tool_call_id),
            name=update.get("name", self.name),
            status=update.get("status", self.status),
        )


def _format_tool_output(result, _artifact, call_id, name, status):
    """Mirror the pinned formatter fields without importing optional LangChain."""

    return _ToolMessage(
        json.dumps(result, sort_keys=True),
        tool_call_id=call_id,
        name=name,
        status=status,
    )


@dataclass
class _ModelResponse:
    result: list
    structured_response: object = None


class _Target:
    def __init__(self):
        self.inputs = []
        self.configs = []
        self.durabilities = []
        self.closed = 0

    def _result(self, graph_input):
        counter = graph_input.get("counter", 0) + 1
        return {
            "messages": [
                _Message(
                    "",
                    tool_calls=[
                        {"id": "call-1", "name": "echo", "args": {"text": "ok"}}
                    ],
                ),
                _Message(
                    "ok",
                    message_type="tool",
                    tool_call_id="call-1",
                    name="echo",
                ),
                _Message(
                    [
                        {
                            "type": "thinking",
                            "thinking": "considering",
                            "signature": "private-signature",
                        },
                        {"type": "text", "text": f"answer-{counter}"},
                    ]
                ),
            ],
            "counter": counter,
            "value": f"value-{counter}",
        }

    async def ainvoke(self, graph_input, *, config, durability=None):
        self.inputs.append(graph_input)
        self.configs.append(config)
        self.durabilities.append(durability)
        return self._result(graph_input)

    async def astream(
        self, graph_input, *, config, stream_mode, durability=None
    ):
        self.inputs.append(graph_input)
        self.configs.append(config)
        self.durabilities.append(durability)
        assert stream_mode == ["messages", "tasks", "values"]
        result = self._result(graph_input)
        yield "tasks", {
            "id": "private-reply-task-1",
            "name": "reply",
            "input": {"private": "node-input"},
            "triggers": ["private-trigger"],
        }
        yield "messages", (
            _Message("answer-"),
            {"langgraph_node": "reply", "private": "metadata"},
        )
        yield "messages", (result["messages"][0], {"langgraph_node": "reply"})
        yield "tasks", {
            "id": "private-reply-task-1",
            "name": "reply",
            "error": None,
            "interrupts": [],
            "result": {"private": "node-result"},
        }
        yield "tasks", {
            "id": "private-tools-task",
            "name": "tools",
            "input": {"private": "tool-input"},
            "triggers": ["private-trigger"],
        }
        yield "messages", (result["messages"][1], {"langgraph_node": "tools"})
        yield "tasks", {
            "id": "private-tools-task",
            "name": "tools",
            "error": None,
            "interrupts": [],
            "result": {"private": "tool-result"},
        }
        yield "tasks", {
            "id": "private-reply-task-2",
            "name": "reply",
            "input": {"private": "final-input"},
            "triggers": ["private-trigger"],
        }
        yield "messages", (
            _Message(
                [
                    {
                        "type": "reasoning",
                        "reasoning": "considering",
                        "signature": "private-stream-signature",
                    },
                    {"type": "text", "text": str(result["counter"])},
                ]
            ),
            {"langgraph_node": "reply"},
        )
        yield "tasks", {
            "id": "private-reply-task-2",
            "name": "reply",
            "error": None,
            "interrupts": [],
            "result": {"private": "final-result"},
        }
        yield "values", result

    async def aclose(self):
        self.closed += 1


class _MetadataTarget(_Target):
    """Return the same metadata-bearing AI message from invoke and stream."""

    def __init__(self, message, stream_metadata):
        super().__init__()
        self.message = message
        self.stream_metadata = stream_metadata

    def _result(self, graph_input):
        return {
            "messages": [*graph_input.get("messages", ()), self.message],
            "value": "answer",
        }

    async def astream(
        self, graph_input, *, config, stream_mode, durability=None
    ):
        self.inputs.append(graph_input)
        self.configs.append(config)
        self.durabilities.append(durability)
        assert stream_mode == ["messages", "tasks", "values"]
        result = self._result(graph_input)
        yield "tasks", {
            "id": "private-task-id",
            "name": "researcher",
            "input": {"private": "task-input"},
            "triggers": [],
        }
        yield "messages", (self.message, self.stream_metadata)
        yield "tasks", {
            "id": "private-task-id",
            "name": "researcher",
            "error": None,
            "interrupts": [],
            "result": {"private": "task-result"},
        }
        yield "values", result


class _ContentRequest(BaseModel):
    prompt: str
    parts: list[ContentPart]


async def _chunks(value):
    yield value


class _MCPAdapterClient:
    instances = []

    def __init__(self, connections, *, tool_interceptors, tool_name_prefix):
        self.connections = connections
        self.tool_interceptors = tool_interceptors
        self.tool_name_prefix = tool_name_prefix
        self.requests = []
        self.http_clients = []
        self.tool_calls = []
        self.closed = 0
        self.__class__.instances.append(self)

    async def get_tools(self, *, server_name):
        self.requests.append(server_name)
        factory = self.connections[server_name].get("httpx_client_factory")
        if factory is not None:
            self.http_clients.append(
                factory(headers={"X-Adapter": "langgraph"}, timeout=1.0)
            )
        async def invoke(arguments, config=None, **kwargs):
            del config, kwargs
            envelope = arguments.get("type") == "tool_call"
            payload = arguments.get("args", {}) if envelope else arguments
            self.tool_calls.append((server_name, dict(payload)))
            if payload.get("fail"):
                if envelope:
                    return SimpleNamespace(
                        type="tool",
                        status="error",
                        content="private-provider-response",
                    )
                raise RuntimeError("private-provider-response")
            result = dict(payload)
            if envelope:
                return SimpleNamespace(
                    type="tool", status="success", content=result
                )
            return result

        return [
            SimpleNamespace(name=f"{server_name}_echo", ainvoke=invoke),
            SimpleNamespace(name=f"{server_name}_hidden", ainvoke=invoke),
        ]

    async def aclose(self):
        for client in self.http_clients:
            await client.aclose()
        self.closed += 1


class _DuplicateMCPAdapterClient(_MCPAdapterClient):
    """Return an invalid duplicate discovery result for collision coverage."""

    async def get_tools(self, *, server_name):
        async def invoke(arguments, config=None, **kwargs):
            del config, kwargs
            return dict(arguments)

        name = f"{server_name}_echo"
        return [
            SimpleNamespace(name=name, ainvoke=invoke),
            SimpleNamespace(name=name, ainvoke=invoke),
        ]


class _TrackedHTTPClient(httpx.AsyncClient):
    """Expose adapter-owned session cleanup ordering to the runtime test."""

    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events

    async def aclose(self):
        self.events.append("session-close")
        await super().aclose()


class _RuntimeMCPLifecycle(MCPClientLifecycle):
    """Record the application and session ownership boundaries."""

    def __init__(self):
        self.events = []

    async def start(self, context: MCPClientContext):
        self.events.append(f"start:{context.framework}")

    def create_http_client(
        self, options: MCPHTTPClientOptions, context: MCPClientContext
    ):
        self.events.append(f"create:{context.framework}")
        return _TrackedHTTPClient(
            self.events,
            headers=dict(options.headers),
            timeout=options.timeout,
            auth=options.auth,
        )

    async def close(self, context: MCPClientContext):
        self.events.append(f"lifecycle-close:{context.framework}")


def _request(
    session_id="session-1", *, state_delta=None, invocation_id="invocation-1"
):
    return InvocationRequest(
        input="hello",
        user_id="user-1",
        session_id=session_id,
        invocation_id=invocation_id,
        metadata={"source": "test"},
        state_delta=state_delta or {},
    )


def _application(target, *, bridge=None, kind=None):
    return CompiledApplication(
        name="portable",
        framework="langgraph",
        mode="advanced" if bridge is not None else "managed",
        target=target,
        kind=kind or ("advanced" if bridge is not None else "agent"),
        bridge=bridge,
    )


def _listener(phase, callback, *, order=0):
    return LifecycleListener(
        phase, callback, order, f"{phase}.py", 1, callback.__name__
    )


class _MCPDispatchTarget:
    """Exercise both native ToolNode-shaped and context-based MCP dispatch."""

    def __init__(self, tool):
        self.tool = tool
        self.retained = None

    async def ainvoke(self, graph_input, *, config):
        del graph_input, config
        native = await self.tool.ainvoke({"source": "native"})
        client = context.mcp("legacy")
        direct = await client.call_tool("echo", {"source": "context"})
        recovered = await client.call_tool("echo", {"fail": True})
        finished = await client.call_tool("echo", {"finish": True})
        self.retained = client
        return {
            "messages": [
                _Message(str((native, direct, recovered, finished)))
            ]
        }

    async def aclose(self):
        return None


class _MCPNativeTarget:
    """Model a directly owned LangGraph target with no Harnest context."""

    def __init__(self, tool):
        self.tool = tool

    async def ainvoke(self, graph_input, *, config):
        del graph_input, config
        result = await self.tool.ainvoke({"source": "direct"})
        return {"messages": [_Message(str(result))]}

    async def aclose(self):
        return None


class _MCPNativeErrorTarget:
    """Exercise native ToolCall error recovery without exposing provider content."""

    def __init__(self, tool):
        self.tool = tool
        self.result = None

    async def ainvoke(self, graph_input, *, config):
        del graph_input, config
        self.result = await self.tool.ainvoke(
            {
                "type": "tool_call",
                "name": self.tool.name,
                "args": {"fail": True},
                "id": "native-error-1",
            }
        )
        return {"messages": [_Message(str(self.result))]}

    async def aclose(self):
        return None


class LangGraphRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _MCPAdapterClient.instances.clear()
        langchain_core = ModuleType("langchain_core")
        langchain_core.__path__ = []
        messages = ModuleType("langchain_core.messages")
        messages.HumanMessage = _HumanMessage
        tools = ModuleType("langchain_core.tools")
        tools.__path__ = []
        tools_base = ModuleType("langchain_core.tools.base")
        tools_base._format_output = _format_tool_output
        self.langchain_modules = patch.dict(
            sys.modules,
            {
                "langchain_core": langchain_core,
                "langchain_core.messages": messages,
                "langchain_core.tools": tools,
                "langchain_core.tools.base": tools_base,
            },
        )
        self.langchain_modules.start()
        self.target = _Target()
        self.driver = LangGraphRuntimeDriver(
            _application(self.target),
            card={"name": "Portable agent", "description": "Deterministic"},
        )

    async def asyncTearDown(self):
        await self.driver.close()
        self.langchain_modules.stop()

    def test_reasoning_content_fallback_is_ai_only_and_text_only(self):
        message = _Message(
            "",
            additional_kwargs={
                "reasoning_content": "checking constraints",
                "signature": "private-signature",
            },
        )
        tool = _Message(
            [{"type": "thinking", "thinking": "tool-internal"}],
            message_type="tool",
            name="inspect",
        )

        self.assertEqual(_message_thinking(message), "checking constraints")
        self.assertEqual(_message_thinking(tool), "")
        self.assertNotIn("private-signature", _message_thinking(message))

    async def test_agent_metadata_is_normalized_with_stream_invoke_parity(self):
        message = _Message(
            "answer",
            name="researcher",
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 5,
                "total_tokens": 16,
                "input_token_details": {"private": "usage-detail"},
            },
            response_metadata={
                "model_name": "test-model",
                "model_provider": "test-provider",
                "finish_reason": "stop",
                "private": "response-detail",
            },
            additional_kwargs={"private": "additional-detail"},
        )
        target = _MetadataTarget(
            message,
            {
                "langgraph_node": "researcher",
                "ls_model_name": "test-model",
                "ls_provider": "test-provider",
                "private": "stream-detail",
            },
        )
        driver = LangGraphRuntimeDriver(_application(target))
        await driver.create_session(
            session_id="metadata-invoke", user_id="user-1", state={}
        )
        await driver.create_session(
            session_id="metadata-stream", user_id="user-1", state={}
        )
        try:
            invoked = await driver.invoke(_request("metadata-invoke"))
            streamed = [
                event
                async for event in driver.stream(_request("metadata-stream"))
            ]
        finally:
            await driver.close()

        invoked_metadata = next(
            event for event in invoked.events if event["type"] == "agent_metadata"
        )
        streamed_metadata = next(
            event for event in streamed if event["type"] == "agent_metadata"
        )
        expected = {
            "type": "agent_metadata",
            "framework": "langgraph",
            "agent": "researcher",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 5,
                "total_tokens": 16,
            },
            "model": "test-model",
            "provider": "test-provider",
            "finish_reason": "stop",
        }
        self.assertEqual(invoked_metadata, expected)
        self.assertEqual(streamed_metadata, expected)
        self.assertEqual(
            [event["type"] for event in invoked.events[-4:]],
            ["message", "agent_metadata", "agent_activity", "graph_output"],
        )
        rendered = repr((invoked.events, streamed))
        self.assertNotIn("response-detail", rendered)
        self.assertNotIn("usage-detail", rendered)
        self.assertNotIn("additional-detail", rendered)
        self.assertNotIn("stream-detail", rendered)

    async def test_raw_agent_metadata_namespaces_native_langgraph_mappings(self):
        message = _Message(
            "answer",
            name="researcher",
            usage_metadata={
                "input_token_details": {"cache_read": 3},
            },
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 4,
                    "total_tokens": 11,
                },
                "model": "alias-model",
                "provider_name": "alias-provider",
                "stop_reason": "end_turn",
                "private": "response-detail",
            },
            additional_kwargs={"private": "additional-detail"},
        )
        target = _MetadataTarget(
            message,
            {
                "langgraph_node": "researcher",
                "private": "stream-detail",
            },
        )
        application = replace(
            _application(target),
            output_policy=OutputPolicy(agent_metadata="raw"),
        )
        driver = LangGraphRuntimeDriver(application)
        await driver.create_session(
            session_id="raw-metadata", user_id="user-1", state={}
        )
        try:
            events = [
                event
                async for event in driver.stream(_request("raw-metadata"))
            ]
        finally:
            await driver.close()

        metadata = next(
            event for event in events if event["type"] == "agent_metadata"
        )
        self.assertEqual(
            metadata["usage"],
            {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11},
        )
        self.assertEqual(metadata["model"], "alias-model")
        self.assertEqual(metadata["provider"], "alias-provider")
        self.assertEqual(metadata["finish_reason"], "end_turn")
        self.assertEqual(
            set(metadata["raw"]),
            {
                "stream_metadata",
                "response_metadata",
                "usage_metadata",
                "additional_kwargs",
            },
        )
        self.assertEqual(
            metadata["raw"]["stream_metadata"]["private"], "stream-detail"
        )
        self.assertEqual(
            metadata["raw"]["response_metadata"]["private"], "response-detail"
        )
        self.assertEqual(
            metadata["raw"]["additional_kwargs"]["private"], "additional-detail"
        )

    async def test_agent_metadata_does_not_estimate_unreported_tokens(self):
        message = _Message(
            "answer",
            usage_metadata={"input_token_details": {"cache_read": 3}},
        )
        target = _MetadataTarget(message, {"langgraph_node": "researcher"})
        driver = LangGraphRuntimeDriver(_application(target))
        await driver.create_session(
            session_id="unreported-usage", user_id="user-1", state={}
        )
        try:
            result = await driver.invoke(_request("unreported-usage"))
        finally:
            await driver.close()

        self.assertNotIn(
            "agent_metadata", [event["type"] for event in result.events]
        )

    async def test_session_map_is_user_scoped_and_supports_crud(self):
        created = await self.driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 1}
        )
        await self.driver.create_session(
            session_id="session-2", user_id="user-1", state={}
        )
        await self.driver.create_session(
            session_id="session-1", user_id="user-2", state={"private": True}
        )

        self.assertEqual(created.state, {"counter": 1})
        self.assertEqual(self.driver.info.framework, "langgraph")
        self.assertEqual(self.driver.info.name, "Portable agent")
        self.assertEqual(
            [item.id for item in await self.driver.list_sessions(user_id="user-1")],
            ["session-1", "session-2"],
        )
        page = await self.driver.list_sessions(
            user_id="user-1", after="session-1", limit=1
        )
        self.assertEqual([item.id for item in page], ["session-2"])
        self.assertIsNone(
            await self.driver.get_session(
                session_id="session-2", user_id="another-user"
            )
        )

        updated = await self.driver.update_session(
            session_id="session-1",
            user_id="user-1",
            state_delta={"enabled": True},
        )
        self.assertEqual(updated.state, {"counter": 1, "enabled": True})
        self.assertTrue(
            await self.driver.delete_session(
                session_id="session-2", user_id="user-1"
            )
        )
        self.assertFalse(
            await self.driver.delete_session(
                session_id="session-2", user_id="user-1"
            )
        )
        with self.assertRaises(SessionConflictError):
            await self.driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )

    async def test_invoke_normalizes_events_and_persists_returned_state(self):
        await self.driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 1}
        )

        result = await self.driver.invoke(
            _request(state_delta={"request_flag": "present"})
        )

        self.assertEqual(result.text, "answer-2")
        self.assertEqual(result.session_id, "session-1")
        self.assertEqual(result.metadata, {"source": "test"})
        self.assertEqual(
            [event["type"] for event in result.events],
            ["tool_call", "tool_result", "thinking", "message", "graph_output"],
        )
        self.assertEqual(result.events[0]["arguments"], {"text": "ok"})
        self.assertEqual(result.events[1]["result"], "ok")
        self.assertEqual(result.events[2]["text"], "considering")
        self.assertEqual(result.events[3]["text"], "answer-2")
        self.assertNotIn("considering", result.text)
        self.assertNotIn("private-signature", repr(result.events))
        self.assertNotIn("private-signature", repr(result.result))
        self.assertEqual(result.result["counter"], 2)
        self.assertEqual(self.target.inputs[0]["request_flag"], "present")
        self.assertEqual(self.target.durabilities, ["sync"])

        session = await self.driver.get_session(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(session.state["counter"], 2)
        self.assertNotIn("messages", session.state)
        self.assertIn("messages", session.metadata["langgraph"])
        self.assertEqual(
            session.metadata["langgraph"]["messages"][-1]["content"][0]["text"],
            "answer-2",
        )
        messages = await self.driver.get_session_messages(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(
            [message.role for message in messages],
            ["assistant", "tool", "assistant"],
        )
        self.assertEqual(messages[-1].content, "answer-2")
        self.assertIn("langgraph", messages[-1].metadata)
        self.assertIsNone(
            await self.driver.get_session_messages(
                session_id="missing", user_id="user-1"
            )
        )

        await self.driver.invoke(_request(invocation_id="invocation-2"))
        thread_ids = [item["configurable"]["thread_id"] for item in self.target.configs]
        self.assertEqual(len(set(thread_ids)), 2)
        self.assertEqual(
            [_decode_thread_id(value).run_id for value in thread_ids],
            ["invocation-1", "invocation-2"],
        )
        self.assertNotIn("session-1", thread_ids)
        self.assertEqual(len(self.target.inputs[1]["messages"]), 4)

    async def test_managed_graph_keeps_session_state_public_and_namespaced(self):
        class StatefulGraphTarget(_Target):
            def _result(self, graph_input):
                state = dict(graph_input["_harnest_state"])
                state["count"] = state.get("count", 0) + 1
                return {
                    **graph_input,
                    "_harnest_state": state,
                    "value": f"count:{state['count']}",
                }

        target = StatefulGraphTarget()
        driver = LangGraphRuntimeDriver(_application(target, kind="graph"))
        await driver.create_session(
            session_id="graph-session",
            user_id="user-1",
            state={"count": 2, "tenant": "one"},
        )

        result = await driver.invoke(_request("graph-session"))
        session = await driver.get_session(
            session_id="graph-session", user_id="user-1"
        )

        self.assertEqual(result.result["value"], "count:3")
        self.assertEqual(session.state, {"count": 3, "tenant": "one"})
        self.assertIn("messages", session.metadata["langgraph"])
        self.assertEqual(
            session.metadata["langgraph"]["_harnest_state"],
            {"count": 3, "tenant": "one"},
        )
        self.assertEqual(
            target.inputs[0]["_harnest_state"], {"count": 2, "tenant": "one"}
        )
        await driver.close()

    async def test_content_input_stays_reference_only_in_graph_and_history(self):
        class EchoTarget(_Target):
            def _result(self, graph_input):
                return {
                    "messages": [*graph_input["messages"], _Message("done")],
                    "value": "done",
                }

        store = MemoryAssetStore(token_factory=lambda: "a" * 24)
        scope = AssetScope(user_id="user-1", session_id="content-session")
        record = await store.save(
            scope=scope,
            media_type="image/png",
            chunks=_chunks(b"private-image"),
        )
        target = EchoTarget()
        application = replace(
            _application(target), input_schema=_ContentRequest
        )
        driver = LangGraphRuntimeDriver(application, asset_store=store)
        await driver.create_session(
            session_id="content-session", user_id="user-1", state={}
        )
        request = InvocationRequest(
            input=_ContentRequest(
                prompt="inspect",
                parts=[
                    Text(text="ordered"),
                    Image(assetId=record.asset_id),
                ],
            ).model_dump(mode="json", by_alias=True),
            user_id="user-1",
            session_id="content-session",
            invocation_id="content-invocation",
            metadata={},
            state_delta={},
        )

        await driver.invoke(request)

        blocks = target.inputs[0]["messages"][-1].content
        self.assertEqual(
            [block["type"] for block in blocks],
            ["text", "text", "image"],
        )
        self.assertEqual(
            blocks[-1],
            {
                "type": "image",
                "assetId": record.asset_id,
                "store": "default",
            },
        )
        self.assertNotIn("base64", repr(target.inputs[0]))
        messages = await driver.get_session_messages(
            session_id="content-session", user_id="user-1"
        )
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].content[-1]["assetId"], record.asset_id)
        self.assertEqual(messages[0].metadata, {"langgraph": {"type": "human"}})
        self.assertNotIn("base64", repr(messages))
        await driver.close()

    async def test_inline_structured_input_is_leased_before_graph_persistence(self):
        class InlineRequest(BaseModel):
            prompt: str
            screenshot: Image

        target = _Target()
        application = replace(_application(target), input_schema=InlineRequest)
        driver = LangGraphRuntimeDriver(application)
        scope = TransientMediaScope("user-1", "inline-input", "inline-call")
        leases = TransientMediaLeaseStore(max_total_bytes=512)
        access = TransientMediaAccess(leases, scope)
        encoded = base64.b64encode(_PNG).decode("ascii")
        await driver.create_session(
            session_id=scope.session_id, user_id=scope.user_id, state={}
        )
        request = InvocationRequest(
            input={
                "prompt": "inspect",
                "screenshot": {
                    "type": "image",
                    "data": encoded,
                    "mediaType": "image/png",
                },
            },
            user_id=scope.user_id,
            session_id=scope.session_id,
            invocation_id=scope.call_id,
            metadata={},
            state_delta={},
        )
        try:
            with patch(
                "harnest.runtime_langgraph.current_transient_media",
                return_value=access,
            ):
                await driver.invoke(request)
            messages = await driver.get_session_messages(
                session_id=scope.session_id, user_id=scope.user_id
            )
        finally:
            await driver.close()

        authored = target.inputs[0]["messages"][-1].content
        self.assertNotIn("harnestTransient", repr(authored))
        self.assertNotIn("leaseId", repr(authored))
        self.assertIn("'content': 'attached'", repr(authored))
        self.assertNotIn(encoded, repr(authored))
        self.assertNotIn(encoded, repr(messages))
        # The model-boundary middleware owns successful consumption. This fake
        # target has no model call, so the test explicitly closes its lease.
        access.clear()

    async def test_model_middleware_materializes_then_stages_media(self):
        store = MemoryAssetStore(
            token_factory=iter(("a" * 24, "b" * 24)).__next__
        )
        scope = AssetScope(user_id="user-1", session_id="session-1")
        input_record = await store.save(
            scope=scope,
            media_type="image/png",
            chunks=_chunks(b"input-image"),
        )
        original = _CopyMessage(
            content=[{"type": "image", "assetId": input_record.asset_id}]
        )

        class Request:
            messages = [original]

            def override(self, **values):
                return SimpleNamespace(**values)

        seen = []

        async def handler(request):
            seen.append(request.messages[0].content)
            return _ModelResponse(
                result=[
                    _CopyMessage(
                        content=[
                            {"type": "text", "text": "rendered"},
                            {
                                "type": "image",
                                "base64": base64.b64encode(_PNG).decode(),
                                "mime_type": "image/png",
                            },
                        ]
                    )
                ]
            )

        langchain = ModuleType("langchain")
        langchain.__path__ = []
        agents = ModuleType("langchain.agents")
        agents.__path__ = []
        middleware_module = ModuleType("langchain.agents.middleware")
        middleware_module.AgentMiddleware = type("AgentMiddleware", (), {})
        with patch.dict(
            sys.modules,
            {
                "langchain": langchain,
                "langchain.agents": agents,
                "langchain.agents.middleware": middleware_module,
            },
        ):
            middleware = _langgraph_asset_middleware(store)
            token = _MODEL_ASSET_SCOPE.set(scope)
            try:
                response = await middleware.awrap_model_call(Request(), handler)
            finally:
                _MODEL_ASSET_SCOPE.reset(token)

        self.assertEqual(
            base64.b64decode(seen[0][0]["base64"]), b"input-image"
        )
        self.assertEqual(original.content[0]["assetId"], input_record.asset_id)
        output = response.result[0].content
        self.assertEqual(output[0], {"type": "text", "text": "rendered"})
        self.assertEqual(output[1]["type"], "image")
        self.assertIn("assetId", output[1])
        self.assertNotIn("base64", repr(response))
        self.assertIsNotNone(
            await store.stat(scope=scope, asset_id=output[1]["assetId"])
        )

    async def test_client_tool_media_is_private_retry_safe_model_content(self):
        scope = TransientMediaScope("user-1", "session-1", "call-1")
        leases = TransientMediaLeaseStore(max_total_bytes=128)
        lease = leases.stage(
            scope=scope,
            kind="image",
            media_type="image/png",
            data=b"private-image",
            metadata=AssetMediaMetadata(width=1, height=1),
        )
        access = TransientMediaAccess(store=leases, scope=scope)
        access.bind((lease.lease_id,))
        marker = {
            "type": "image",
            "mediaType": "image/png",
            "content": "attached",
        }
        original = _CopyMessage(content=json.dumps({"screenshot": marker}))

        class Request:
            messages = [original]

            def override(self, **values):
                return SimpleNamespace(**values)

        attempts = []

        async def handler(request):
            attempts.append(request.messages[0].content)
            if len(attempts) == 1:
                raise RuntimeError("provider failed")
            return _ModelResponse(result=[])

        langchain = ModuleType("langchain")
        langchain.__path__ = []
        agents = ModuleType("langchain.agents")
        agents.__path__ = []
        middleware_module = ModuleType("langchain.agents.middleware")
        middleware_module.AgentMiddleware = type("AgentMiddleware", (), {})
        with patch.dict(
            sys.modules,
            {
                "langchain": langchain,
                "langchain.agents": agents,
                "langchain.agents.middleware": middleware_module,
            },
        ):
            middleware = _langgraph_asset_middleware(None)
            with patch(
                "harnest.runtime_langgraph.current_transient_media",
                return_value=access,
            ):
                with self.assertRaisesRegex(RuntimeError, "provider failed"):
                    await middleware.awrap_model_call(Request(), handler)
                self.assertIsNotNone(access.peek(lease.lease_id))
                await middleware.awrap_model_call(Request(), handler)

        self.assertNotIn("harnestTransient", original.content)
        self.assertNotIn(lease.lease_id, original.content)
        for content in attempts:
            self.assertNotIn("harnestTransient", repr(content))
            self.assertEqual(
                base64.b64decode(content[-1]["base64"]), b"private-image"
            )
        self.assertIsNone(access.peek(lease.lease_id))
        tool = _Message(
            original.content,
            message_type="tool",
            tool_call_id="tool-1",
            name="capture",
        )
        self.assertNotIn("harnestTransient", repr(_message_tool_events(tool)))

    async def test_stream_emits_only_canonical_events_and_updates_state(self):
        await self.driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 3}
        )

        events = [event async for event in self.driver.stream(_request())]

        self.assertEqual(
            [event["type"] for event in events],
            [
                "agent_activity",
                "message",
                "tool_call",
                "agent_activity",
                "agent_activity",
                "tool_result",
                "agent_activity",
                "agent_activity",
                "thinking",
                "message",
                "agent_activity",
                "graph_output",
            ],
        )
        self.assertEqual(
            [
                (event["agent"], event["activity"])
                for event in events
                if event["type"] == "agent_activity"
            ],
            [
                ("reply", "started"),
                ("reply", "completed"),
                ("tools", "started"),
                ("tools", "completed"),
                ("reply", "started"),
                ("reply", "completed"),
            ],
        )
        self.assertEqual(
            events[8],
            {"type": "thinking", "text": "considering", "agent": "reply"},
        )
        self.assertEqual(
            "".join(event["text"] for event in events if event["type"] == "message"),
            "answer-4",
        )
        self.assertEqual(events[5]["agent"], "tools")
        self.assertNotIn("node-input", repr(events))
        self.assertNotIn("node-result", repr(events))
        self.assertNotIn("private-reply-task", repr(events))
        self.assertNotIn("private-stream-signature", repr(events))
        self.assertEqual(events[-1]["output"]["counter"], 4)
        session = await self.driver.get_session(
            session_id="session-1", user_id="user-1"
        )
        self.assertEqual(session.state["counter"], 4)
        self.assertEqual(self.target.durabilities, ["sync"])

    async def test_stream_completes_after_reconciliation_but_not_on_interrupt(self):
        class ReconciledTarget(_Target):
            async def astream(
                self, graph_input, *, config, stream_mode, durability=None
            ):
                del graph_input, config, stream_mode, durability
                yield "messages", (
                    _Message("com"),
                    {"langgraph_node": "worker"},
                )
                yield "values", {
                    "messages": [_Message("complete", name="worker")],
                    "value": "complete",
                }

        class InterruptedTarget(_Target):
            async def astream(
                self, graph_input, *, config, stream_mode, durability=None
            ):
                del graph_input, config, stream_mode, durability
                yield "messages", (
                    _Message("waiting"),
                    {"langgraph_node": "worker"},
                )
                yield "values", {
                    "messages": [_Message("waiting", name="worker")],
                    "__interrupt__": ("approval",),
                }

        reconciled = LangGraphRuntimeDriver(_application(ReconciledTarget()))
        interrupted = LangGraphRuntimeDriver(_application(InterruptedTarget()))
        await reconciled.create_session(
            session_id="reconciled", user_id="user-1", state={}
        )
        await interrupted.create_session(
            session_id="interrupted", user_id="user-1", state={}
        )
        interrupted_events = []
        try:
            events = [
                event
                async for event in reconciled.stream(_request("reconciled"))
            ]
            with self.assertRaises(NativeDurableSuspended):
                async for event in interrupted.stream(_request("interrupted")):
                    interrupted_events.append(event)
        finally:
            await reconciled.close()
            await interrupted.close()

        self.assertEqual(
            [event["type"] for event in events[-3:]],
            ["message", "agent_activity", "graph_output"],
        )
        self.assertEqual(events[-3]["text"], "plete")
        self.assertEqual(events[-3]["agent"], "worker")
        self.assertEqual(events[-2]["activity"], "completed")
        self.assertEqual(
            [event["activity"] for event in interrupted_events if "activity" in event],
            ["started"],
        )
        self.assertNotIn("graph_output", [event["type"] for event in interrupted_events])

    async def test_task_stream_projects_terminal_states_without_payloads(self):
        class TaskActivityTarget(_Target):
            async def astream(
                self, graph_input, *, config, stream_mode, durability=None
            ):
                del graph_input, config, durability
                self.modes = stream_mode
                yield "tasks", {
                    "id": "private-start-id",
                    "name": "worker",
                    "input": {"secret": "private-input"},
                    "triggers": ["private-trigger"],
                }
                yield "tasks", {
                    "id": "private-complete-id",
                    "name": "worker",
                    "error": None,
                    "interrupts": [],
                    "result": {"secret": "private-result"},
                }
                yield "tasks", {
                    "id": "private-failed-start-id",
                    "name": "worker",
                    "input": "private-failed-input",
                    "triggers": [],
                }
                yield "tasks", {
                    "id": "private-failed-id",
                    "name": "worker",
                    "error": "private-error-text",
                    "interrupts": [],
                    "result": {},
                }
                yield "tasks", {
                    "id": "private-interrupt-start-id",
                    "name": "worker",
                    "input": "private-interrupt-input",
                    "triggers": [],
                }
                yield "tasks", {
                    "id": "private-interrupt-id",
                    "name": "worker",
                    "error": None,
                    "interrupts": [{"value": "private-interrupt-value"}],
                    "result": {},
                }
                yield "tasks", {
                    "id": "private-reserved-id",
                    "name": "__interrupt__",
                    "input": "private-reserved-input",
                    "triggers": [],
                }
                yield "tasks", {
                    "id": "private-parallel-1",
                    "name": "parallel-worker",
                    "input": {},
                    "triggers": [],
                }
                yield "tasks", {
                    "id": "private-parallel-2",
                    "name": "parallel-worker",
                    "input": {},
                    "triggers": [],
                }
                yield "tasks", {
                    "id": "private-parallel-1",
                    "name": "parallel-worker",
                    "error": None,
                    "interrupts": [],
                    "result": {},
                }
                yield "tasks", {
                    "id": "private-parallel-2",
                    "name": "parallel-worker",
                    "error": None,
                    "interrupts": [],
                    "result": {},
                }
                yield "values", {
                    "messages": [_Message("done")],
                    "value": "done",
                }

        target = TaskActivityTarget()
        driver = LangGraphRuntimeDriver(_application(target))
        await driver.create_session(
            session_id="task-activity", user_id="user-1", state={}
        )
        try:
            events = [
                event
                async for event in driver.stream(_request("task-activity"))
            ]
        finally:
            await driver.close()

        self.assertEqual(target.modes, ["messages", "tasks", "values"])
        self.assertEqual(
            [event["activity"] for event in events if "activity" in event],
            [
                "started",
                "completed",
                "started",
                "failed",
                "started",
                "interrupted",
                "started",
                "started",
                "completed",
                "completed",
            ],
        )
        rendered = repr(events)
        for private in (
            "private-start-id",
            "private-input",
            "private-result",
            "private-error-text",
            "private-interrupt-value",
            "private-reserved-input",
        ):
            self.assertNotIn(private, rendered)

    async def test_pre_tool_narration_is_suppressed_by_default_and_can_be_included(self):
        class NarratingTarget(_Target):
            def _result(self, graph_input):
                return {
                    "messages": [
                        *graph_input.get("messages", ()),
                        _Message(
                            "I'll inspect. ",
                            name="researcher",
                            tool_calls=[
                                {
                                    "id": "call-1",
                                    "name": "inspect",
                                    "args": {"selector": "main"},
                                }
                            ],
                        ),
                        _Message(
                            "highlighted",
                            message_type="tool",
                            tool_call_id="call-1",
                            name="inspect",
                        ),
                        _Message("Done.", name="researcher"),
                    ],
                    "value": "Done.",
                }

            async def astream(self, graph_input, *, config, stream_mode):
                self.inputs.append(graph_input)
                self.configs.append(config)
                result = self._result(graph_input)
                for message in result["messages"][-3:]:
                    yield "messages", (message, {"node": "researcher"})
                yield "values", result

        async def run(policy):
            target = NarratingTarget()
            application = replace(_application(target), output_policy=policy)
            driver = LangGraphRuntimeDriver(application)
            await driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            invoked = await driver.invoke(_request())
            streamed = [
                event
                async for event in driver.stream(
                    _request(invocation_id="invocation-2")
                )
            ]
            await driver.close()
            return invoked, streamed

        suppressed, suppressed_stream = await run(OutputPolicy())
        included, included_stream = await run(
            OutputPolicy(subagent_messages="include")
        )

        self.assertEqual(suppressed.text, "Done.")
        self.assertEqual(
            (
                suppressed.events[0]["agent"],
                suppressed.events[0]["activity"],
                suppressed.events[4]["agent"],
                suppressed.events[4]["activity"],
            ),
            ("researcher", "started", "researcher", "completed"),
        )
        self.assertEqual(suppressed.events[2]["type"], "tool_result")
        self.assertNotIn("agent", suppressed.events[2])
        self.assertEqual(suppressed.events[3]["agent"], "researcher")
        self.assertNotIn(
            "I'll inspect.",
            "".join(
                event.get("text", "") for event in suppressed_stream
            ),
        )
        self.assertIn(
            "tool_call", [event["type"] for event in suppressed_stream]
        )
        self.assertEqual(included.text, "I'll inspect. Done.")
        self.assertEqual(
            "".join(
                event.get("text", "") for event in included_stream
            ),
            "I'll inspect. Done.",
        )
        self.assertIn("tool_call", [event["type"] for event in included_stream])

    async def test_advanced_bridge_controls_input_and_visible_output(self):
        target = _Target()
        bridge = Agent.advanced(
            target=target,
            input_adapter=lambda text, state: {
                "counter": state["counter"],
                "prompt": text.upper(),
            },
            output_adapter=lambda result: f"bridge:{result['counter']}",
        )
        driver = LangGraphRuntimeDriver(_application(target, bridge=bridge))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 8}
        )

        result = await driver.invoke(_request())

        self.assertEqual(target.inputs[0]["prompt"], "HELLO")
        self.assertEqual(result.text, "bridge:9")
        self.assertEqual(result.result, "bridge:9")
        await driver.close()

    async def test_driver_materializes_and_closes_one_mcp_client(self):
        configured = replace(
            MCPClient.sse(
                "https://mcp.example/sse",
                prefix="legacy",
                tools=("echo",),
            ),
            capability_id="mcp__legacy",
            approval=ApprovalPolicy(
                message="Approve echo?",
                tools=("echo",),
            ),
        )
        definition = AgentDefinition(
            name="mcp_agent",
            model="openai:test",
            instruction="Use MCP tools.",
            mcp=(configured,),
        )
        plan = ManagedAgentPlan(definition, tools=("local-tool",))
        target = _Target()
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 0}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        with patch.dict(
            sys.modules,
            {
                "langchain_mcp_adapters": adapters,
                "langchain_mcp_adapters.client": client_module,
            },
        ), patch(
            "harnest.backends.langgraph.materialize_agent",
            return_value=target,
        ) as materialize:
            first = await driver.invoke(_request())
            second = await driver.invoke(_request())

        self.assertEqual(first.text, "answer-1")
        self.assertEqual(second.text, "answer-2")
        self.assertEqual(len(_MCPAdapterClient.instances), 1)
        client = _MCPAdapterClient.instances[0]
        self.assertEqual(client.requests, ["mcp__legacy"])
        self.assertEqual(client.connections["mcp__legacy"]["transport"], "sse")
        self.assertTrue(client.tool_name_prefix)
        self.assertEqual(len(client.tool_interceptors), 1)
        passed_plan, passed_tools = materialize.call_args.args
        self.assertIs(passed_plan, plan)
        self.assertEqual([tool.name for tool in passed_tools], ["mcp__legacy_echo"])

        await driver.close()

        self.assertEqual(target.closed, 1)
        self.assertEqual(client.closed, 1)

    async def test_mcp_native_and_context_dispatch_share_one_governed_path(self):
        events = []

        def before_mcp(call_context, request):
            events.append(("mcp-before", call_context.client_name))
            if request.arguments.get("finish"):
                return call_context.finish({"stopped": True})
            return call_context.next()

        def after_mcp(call_context, result):
            events.append(("mcp-after", call_context.agent_name))
            return call_context.next(result)

        def on_mcp_error(call_context, error):
            events.append(("mcp-error", type(error).__name__, str(error)))
            return call_context.finish({"recovered": True})

        def before_tool(call_context, request):
            events.append(("tool-before", call_context.tool_name))
            return call_context.next(request)

        def after_tool(call_context, result):
            events.append(("tool-after", call_context.tool_name))
            return call_context.next(result)

        def on_tool_error(call_context, error):
            events.append(("tool-error", type(error).__name__, str(error)))
            return call_context.next()

        listeners = tuple(
            _listener(phase, callback)
            for phase, callback in (
                ("before_mcp", before_mcp),
                ("after_mcp", after_mcp),
                ("on_mcp_error", on_mcp_error),
                ("before_tool", before_tool),
                ("after_tool", after_tool),
                ("on_tool_error", on_tool_error),
            )
        )
        configured = replace(
            MCPClient.sse("https://mcp.example/sse", tools=("echo",)),
            identity="legacy",
            capability_id="mcp__legacy",
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="mcp_agent",
                model="openai:test",
                instruction="Use MCP tools.",
                mcp=(configured,),
            )
        )
        application = replace(_application(plan), extensions=listeners)
        backend = LangGraphRuntimeDriver(application)
        driver = ExtensionRuntimeDriver(backend, listeners)
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient
        target = None

        def materialize(_plan, tools):
            nonlocal target
            target = _MCPDispatchTarget(tools[0])
            return target

        try:
            with patch.dict(
                sys.modules,
                {
                    "langchain_mcp_adapters": adapters,
                    "langchain_mcp_adapters.client": client_module,
                },
            ), patch(
                "harnest.backends.langgraph.materialize_agent",
                side_effect=materialize,
            ), self.assertLogs(
                "harnest.agent.mcp.audit", level="INFO"
            ) as captured:
                result = await driver.invoke(_request())

            self.assertIn("'recovered': True", result.text)
            self.assertIn("'stopped': True", result.text)
            self.assertEqual(
                [item[0] for item in events].count("tool-before"), 3
            )
            self.assertEqual(
                [item[0] for item in events].count("mcp-before"), 4
            )
            self.assertTrue(
                all(
                    item[1] == "legacy"
                    for item in events
                    if item[0] == "mcp-before"
                )
            )
            rendered = repr(events)
            self.assertNotIn("private-provider-response", rendered)
            self.assertIn("MCPToolCallError", rendered)
            self.assertEqual(
                [record.outcome for record in captured.records],
                ["committed", "committed", "recovered", "finished"],
            )
            self.assertTrue(
                all(record.client == "mcp__legacy" for record in captured.records)
            )
            self.assertTrue(all(record.tool == "echo" for record in captured.records))
            self.assertEqual(target.tool.name, "mcp__legacy_echo")
            with self.assertRaises(MCPContextUnavailableError):
                await target.retained.call_tool("echo")
        finally:
            await driver.close()

    async def test_native_mcp_error_uses_safe_error_hook_and_keeps_call_identity(self):
        observed = []

        def recover(call_context, error):
            observed.append((type(error).__name__, str(error)))
            return call_context.finish({"recovered": True})

        listener = _listener("on_mcp_error", recover)
        configured = replace(
            MCPClient.sse("https://mcp.example/sse", tools=("echo",)),
            identity="legacy",
            capability_id="mcp__legacy",
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="mcp_agent",
                model="openai:test",
                instruction="Use MCP tools.",
                mcp=(configured,),
            )
        )
        application = replace(_application(plan), extensions=(listener,))
        backend = LangGraphRuntimeDriver(application)
        driver = ExtensionRuntimeDriver(backend, (listener,))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient
        target = None

        def materialize(_plan, tools):
            nonlocal target
            target = _MCPNativeErrorTarget(tools[0])
            return target

        try:
            with patch.dict(
                sys.modules,
                {
                    "langchain_mcp_adapters": adapters,
                    "langchain_mcp_adapters.client": client_module,
                },
            ), patch(
                "harnest.backends.langgraph.materialize_agent",
                side_effect=materialize,
            ), self.assertLogs(
                "harnest.agent.mcp.audit", level="INFO"
            ) as captured:
                await driver.invoke(_request())

            self.assertEqual(
                observed,
                [("MCPToolCallError", "managed MCP tool returned an error")],
            )
            self.assertNotIn("private-provider-response", repr(observed))
            self.assertEqual(target.result.status, "success")
            self.assertEqual(target.result.tool_call_id, "native-error-1")
            self.assertEqual(target.result.name, "mcp__legacy_echo")
            self.assertIn("recovered", target.result.content)
            self.assertEqual(captured.records[0].outcome, "recovered")
        finally:
            await driver.close()

    async def test_mcp_context_rejects_duplicate_public_client_names(self):
        clients = (
            replace(
                MCPClient.sse("https://one.example/sse"),
                identity="billing",
                capability_id="mcp__billing",
            ),
            replace(
                MCPClient.sse("https://two.example/sse"),
                identity="billing",
                capability_id="plugin__payments__mcp__billing",
            ),
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="mcp_agent",
                model="openai:test",
                instruction="Use MCP tools.",
                mcp=clients,
            )
        )
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        try:
            with patch.dict(
                sys.modules,
                {
                    "langchain_mcp_adapters": adapters,
                    "langchain_mcp_adapters.client": client_module,
                },
            ), self.assertRaisesRegex(ValueError, "duplicate.*public name"):
                await driver.invoke(_request())
        finally:
            await driver.close()

    async def test_direct_driver_preserves_native_mcp_without_managed_context(self):
        configured = replace(
            MCPClient.sse("https://mcp.example/sse", tools=("echo",)),
            identity="legacy",
            capability_id="mcp__legacy",
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="mcp_agent",
                model="openai:test",
                instruction="Use MCP tools.",
                mcp=(configured,),
            )
        )
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        def materialize(_plan, tools):
            return _MCPNativeTarget(tools[0])

        try:
            with patch.dict(
                sys.modules,
                {
                    "langchain_mcp_adapters": adapters,
                    "langchain_mcp_adapters.client": client_module,
                },
            ), patch(
                "harnest.backends.langgraph.materialize_agent",
                side_effect=materialize,
            ):
                result = await driver.invoke(_request())

            self.assertIn("'source': 'direct'", result.text)
            self.assertEqual(
                _MCPAdapterClient.instances[-1].tool_calls,
                [("mcp__legacy", {"source": "direct"})],
            )
        finally:
            await driver.close()

    async def test_mcp_context_rejects_duplicate_public_tool_aliases(self):
        configured = replace(
            MCPClient.sse("https://mcp.example/sse"),
            identity="legacy",
            capability_id="mcp__legacy",
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="mcp_agent",
                model="openai:test",
                instruction="Use MCP tools.",
                mcp=(configured,),
            )
        )
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _DuplicateMCPAdapterClient

        try:
            with patch.dict(
                sys.modules,
                {
                    "langchain_mcp_adapters": adapters,
                    "langchain_mcp_adapters.client": client_module,
                },
            ), self.assertRaisesRegex(
                ValueError, "duplicate.*public tool name"
            ):
                await driver.invoke(_request())
        finally:
            await driver.close()

    async def test_mcp_gateway_lifecycle_wraps_adapter_owned_sessions(self):
        lifecycle = _RuntimeMCPLifecycle()
        configured = replace(
            MCPClient.streamable_http(
                "https://gateway.example/mcp",
                lifecycle=lifecycle,
            ),
            capability_id="mcp__gateway",
        )
        plan = ManagedAgentPlan(
            AgentDefinition(
                name="gateway_agent",
                model="openai:test",
                instruction="Use the gateway.",
                mcp=(configured,),
            ),
            tools=(),
        )
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        with patch.dict(
            sys.modules,
            {
                "langchain_mcp_adapters": adapters,
                "langchain_mcp_adapters.client": client_module,
            },
        ), patch(
            "harnest.backends.langgraph.materialize_agent",
            return_value=_Target(),
        ):
            await driver.invoke(_request())

        self.assertEqual(
            lifecycle.events,
            ["start:langgraph", "create:langgraph"],
        )
        await driver.close()
        self.assertEqual(
            lifecycle.events,
            [
                "start:langgraph",
                "create:langgraph",
                "session-close",
                "lifecycle-close:langgraph",
            ],
        )

    async def test_driver_materializes_mcp_agents_inside_graph(self):
        definition = AgentDefinition(
            name="graph_agent",
            model="openai:test",
            instruction="Use MCP tools.",
            mcp=(
                MCPClient.sse(
                    "https://mcp.example/sse", prefix="graph", tools=("echo",)
                ),
            ),
        )
        graph = Graph(
            name="mcp_graph",
            nodes={"primary": definition, "secondary": definition},
            edges=(Edge(START, "primary"), Edge(START, "secondary")),
        )
        plan = ManagedGraphPlan(graph)
        target = _Target()
        driver = LangGraphRuntimeDriver(_application(plan))
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 0}
        )
        adapters = ModuleType("langchain_mcp_adapters")
        adapters.__path__ = []
        client_module = ModuleType("langchain_mcp_adapters.client")
        client_module.MultiServerMCPClient = _MCPAdapterClient

        with patch.dict(
            sys.modules,
            {
                "langchain_mcp_adapters": adapters,
                "langchain_mcp_adapters.client": client_module,
            },
        ), patch(
            "harnest.backends.langgraph.materialize_graph",
            return_value=target,
        ) as materialize:
            result = await driver.invoke(_request())

        self.assertEqual(result.text, "answer-1")
        self.assertEqual(len(_MCPAdapterClient.instances), 1)
        client = _MCPAdapterClient.instances[-1]
        self.assertEqual(client.requests, ["graph"])
        self.assertEqual(client.tool_interceptors, [])
        passed_plan, tool_groups = materialize.call_args.args
        self.assertIs(passed_plan, plan)
        self.assertEqual([tool.name for tool in tool_groups[0]], ["graph_echo"])
        self.assertIs(tool_groups[0], tool_groups[1])

        await driver.close()
        self.assertEqual(target.closed, 1)
        self.assertEqual(client.closed, 1)

    async def test_close_is_idempotent_and_rejects_new_work(self):
        await self.driver.close()
        await self.driver.close()

        self.assertEqual(self.target.closed, 1)
        with self.assertRaises(RuntimeError):
            await self.driver.list_sessions(user_id="user-1")

    async def test_injected_session_store_is_authoritative_and_not_owned(self):
        store = _RecordingSessionStore()
        target = _Target()
        driver = LangGraphRuntimeDriver(
            _application(target),
            session_store=store,
        )
        self.assertIs(driver.session_context_store, store)
        await driver.create_session(
            session_id="session-1", user_id="user-1", state={"counter": 4}
        )

        result = await driver.invoke(_request())
        stored = await store.get(session_id="session-1", user_id="user-1")
        await driver.close()

        self.assertEqual(result.text, "answer-5")
        self.assertEqual(stored.state["counter"], 5)
        self.assertFalse(store.closed)


if __name__ == "__main__":
    unittest.main()

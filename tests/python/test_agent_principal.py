import asyncio
from types import SimpleNamespace
from typing import ClassVar
import unittest
from unittest.mock import AsyncMock

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types as genai_types
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START as LANGGRAPH_START
from langgraph.graph import StateGraph

from harnest import AgentRuntimePermissionError, AgentRuntimePrincipal
from harnest.agent_principal import (
    activate_agent_principal,
    active_agent_principal,
    attach_required_permissions,
    create_agent_principal_binding,
    resolve_nested_agent_principal,
    revoke_agent_principal,
)
from harnest.agent import Agent
from harnest.backends.langgraph import (
    build_agent,
    build_graph,
    _graph_agent_principal_projection_complete,
    _langchain_tools,
    _project_principal_tools,
)
from harnest.application import CompiledApplication
from harnest.approval import require_human_approval
from harnest.client_tool import client_tool
from harnest.context_agent import _resolve_invocation_agent_principal
from harnest.context import context
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.mcp import MCPClient
from harnest.graph import START, Edge, Graph
from harnest.mcp_context import (
    MCPClientUnavailableError,
    MCPToolUnavailableError,
    _activate_mcp_context,
    _managed_mcp_tool,
    mcp,
)
from harnest.neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
)
from harnest.runtime_extensions import ExtensionRuntimeDriver
from harnest.runtime_adk import ADKRuntimeDriver
from harnest.runtime_langgraph import LangGraphRuntimeDriver
from harnest.tool import tool
from harnest.tool_adk import _project_model_tools


def _request(principal=None):
    return InvocationRequest(
        input="hello",
        user_id="user-1",
        session_id="session-1",
        invocation_id="invocation-1",
        metadata={},
        state_delta={},
        agent_principal=principal,
    )


class _ADKProjectionModel(BaseLlm):
    """Capture the actual ADK model request produced by the managed runner."""

    observed_tools: ClassVar[list[tuple[str, ...]]] = []

    async def generate_content_async(self, llm_request, stream=False):
        del stream
        type(self).observed_tools.append(tuple(llm_request.tools_dict))
        yield LlmResponse(
            content=genai_types.Content(
                role="model", parts=[genai_types.Part(text="complete")]
            )
        )


class _LangGraphProjectionModel(BaseChatModel):
    """Capture the invocation-local tools bound at the LangGraph model edge."""

    observed_tools: ClassVar[list[tuple[str, ...]]] = []

    @property
    def _llm_type(self):
        return "agent-principal-projection"

    def bind_tools(self, tools, **kwargs):
        del kwargs
        type(self).observed_tools.append(
            tuple(str(getattr(item, "name", "")) for item in tools)
        )
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del messages, stop, run_manager, kwargs
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="complete"))]
        )


class _RecordingDriver:
    def __init__(self):
        self.info = AgentInfo(
            id="support",
            name="support",
            description="Support",
            card={},
            framework="langgraph",
            mode="managed",
            agent_principal_projection_complete=True,
        )
        self.seen = []

    async def invoke(self, request):
        self.seen.append((active_agent_principal(), hasattr(context, "agent_principal")))
        return InvocationResult("ok", (), None, request.session_id, {})

    async def stream(self, request):
        for index in range(2):
            self.seen.append(active_agent_principal())
            yield {"type": "message", "text": str(index)}


class AgentRuntimePrincipalTests(unittest.IsolatedAsyncioTestCase):
    def test_permission_identifiers_are_validated_at_authoring_boundaries(self):
        with self.assertRaisesRegex(ValueError, "permission must start"):
            AgentRuntimePrincipal.create(permissions={"bad permission"})
        with self.assertRaisesRegex(ValueError, "permission must start"):
            MCPClient.sse(
                "https://mcp.invalid/sse",
                tool_permissions={"read": "bad permission"},
            )
        with self.assertRaisesRegex(ValueError, "permission must start"):
            tool(permission="bad permission")

    def test_principals_can_only_be_derived_with_fewer_permissions(self):
        principal = AgentRuntimePrincipal.create(
            permissions={"billing.read", "billing.write"}
        )

        restricted = principal.restrict({"billing.read"})

        self.assertEqual(restricted.permissions, frozenset({"billing.read"}))
        self.assertNotEqual(restricted.id, principal.id)
        with self.assertRaises(AgentRuntimePermissionError):
            principal.restrict({"admin.all"})

    def test_nested_invocations_inherit_and_cannot_amplify_permissions(self):
        principal = AgentRuntimePrincipal.create(permissions={"billing.read"})
        binding = create_agent_principal_binding(principal)
        try:
            with activate_agent_principal(binding):
                self.assertIs(resolve_nested_agent_principal(None), principal)
                restricted = resolve_nested_agent_principal(
                    AgentRuntimePrincipal.create(permissions=())
                )
                with self.assertRaises(AgentRuntimePermissionError):
                    resolve_nested_agent_principal(
                        AgentRuntimePrincipal.create(
                            permissions={"billing.read", "billing.write"}
                        )
                    )
        finally:
            revoke_agent_principal(binding)

        self.assertEqual(restricted.permissions, frozenset())

    async def test_permissioned_tool_is_unrestricted_without_a_principal(self):
        @tool(permission="billing.write")
        async def charge():
            """Charge the customer."""

            return "charged"

        self.assertEqual(await charge(), "charged")

    async def test_permissioned_tool_rechecks_the_active_principal(self):
        @tool(permission="billing.write")
        async def charge():
            """Charge the customer."""

            return "charged"

        denied = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"billing.read"})
        )
        allowed = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"billing.write"})
        )
        try:
            with activate_agent_principal(denied):
                with self.assertRaises(AgentRuntimePermissionError):
                    await charge()
            with activate_agent_principal(allowed):
                self.assertEqual(await charge(), "charged")
        finally:
            revoke_agent_principal(denied)
            revoke_agent_principal(allowed)

    async def test_client_tool_rechecks_before_requesting_client_execution(self):
        @client_tool(permission="device.location")
        async def location():
            """Read the device location."""

        binding = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(binding):
                with self.assertRaises(AgentRuntimePermissionError):
                    await location()
        finally:
            revoke_agent_principal(binding)

    async def test_permission_precedes_approval_in_both_decorator_orders(self):
        @require_human_approval(message="Approve charge?")
        @tool(permission="billing.write")
        async def outer_approval():
            """Charge with approval outside the tool marker."""

        @tool(permission="billing.write")
        @require_human_approval(message="Approve charge?")
        async def outer_tool():
            """Charge with the tool marker outside approval."""

        binding = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(binding):
                for operation in (outer_approval, outer_tool):
                    with self.assertRaises(AgentRuntimePermissionError):
                        await operation()
        finally:
            revoke_agent_principal(binding)

    async def test_runtime_binds_principal_privately_for_invoke(self):
        driver = _RecordingDriver()
        wrapped = ExtensionRuntimeDriver(driver, [])
        principal = AgentRuntimePrincipal.create(permissions={"support.read"})

        await wrapped.invoke(_request(principal))

        self.assertEqual(driver.seen, [(principal, False)])
        self.assertIsNone(active_agent_principal())

    async def test_advanced_runtime_enforces_harnest_owned_boundaries(self):
        driver = _RecordingDriver()
        driver.info = AgentInfo(
            id="support",
            name="support",
            description="Support",
            card={},
            framework="langgraph",
            mode="advanced",
        )
        wrapped = ExtensionRuntimeDriver(driver, [])
        principal = AgentRuntimePrincipal.create(permissions={"support.read"})

        await wrapped.invoke(_request(principal))

        self.assertEqual(driver.seen, [(principal, False)])

    async def test_managed_runtime_rejects_incomplete_nested_projection(self):
        native_builder = StateGraph(dict)
        native_builder.add_node("complete", lambda state: state)
        native_builder.add_edge(LANGGRAPH_START, "complete")
        native = native_builder.compile()
        graph = Graph(
            name="mixed",
            nodes={"native": native},
            edges=(Edge(START, "native"),),
        )
        application = CompiledApplication(
            name="mixed",
            framework="langgraph",
            mode="managed",
            kind="graph",
            target=build_graph(graph),
        )
        wrapped = ExtensionRuntimeDriver(LangGraphRuntimeDriver(application), [])
        start_resources = AsyncMock()
        wrapped._start_resources = start_resources

        try:
            with self.assertRaisesRegex(
                AgentRuntimePermissionError, "complete tool projection"
            ):
                await wrapped.invoke(_request(AgentRuntimePrincipal.create()))
        finally:
            await wrapped.close()

        start_resources.assert_not_awaited()

    def test_native_langgraph_nodes_make_principal_projection_incomplete(self):
        graph = SimpleNamespace(
            nodes={"native": Agent.advanced(SimpleNamespace())}
        )

        self.assertFalse(
            _graph_agent_principal_projection_complete(graph, type("Pregel", (), {}))
        )

    def test_root_omission_is_unrestricted_but_cron_omission_fails_closed(self):
        self.assertIsNone(
            _resolve_invocation_agent_principal(None, trigger="user")
        )

        cron_principal = _resolve_invocation_agent_principal(None, trigger="cron")

        self.assertIsInstance(cron_principal, AgentRuntimePrincipal)
        self.assertEqual(cron_principal.permissions, frozenset())

    async def test_stream_scope_does_not_leak_to_the_caller_between_events(self):
        driver = _RecordingDriver()
        wrapped = ExtensionRuntimeDriver(driver, [])
        principal = AgentRuntimePrincipal.create(permissions={"support.read"})
        observed = []

        async for _event in wrapped.stream(_request(principal)):
            observed.append(active_agent_principal())

        self.assertEqual(driver.seen, [principal, principal])
        self.assertEqual(observed, [None, None])

    async def test_child_task_cannot_retain_principal_after_invocation(self):
        release = asyncio.Event()

        class ChildDriver(_RecordingDriver):
            async def invoke(self, request):
                async def retained():
                    await release.wait()
                    return active_agent_principal()

                self.retained = asyncio.create_task(retained())
                return await super().invoke(request)

        driver = ChildDriver()
        wrapped = ExtensionRuntimeDriver(driver, [])
        principal = AgentRuntimePrincipal.create(permissions={"support.read"})

        await wrapped.invoke(_request(principal))
        release.set()

        with self.assertRaises(AgentRuntimePermissionError):
            await driver.retained

    def test_langgraph_projects_only_available_tools(self):
        public = SimpleNamespace(name="public")
        read = attach_required_permissions(
            SimpleNamespace(name="read"), ("billing.read",)
        )
        write = attach_required_permissions(
            SimpleNamespace(name="write"), ("billing.write",)
        )

        class Request:
            tools = [public, read, write]

            def override(self, *, tools):
                return SimpleNamespace(tools=tools)

        binding = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"billing.read"})
        )
        try:
            with activate_agent_principal(binding):
                projected = _project_principal_tools(Request())
        finally:
            revoke_agent_principal(binding)

        self.assertEqual([item.name for item in projected.tools], ["public", "read"])

    def test_adk_projects_matching_tool_declarations(self):
        public = SimpleNamespace(name="public")
        hidden = attach_required_permissions(
            SimpleNamespace(name="hidden"), ("admin.use",)
        )
        group = SimpleNamespace(
            function_declarations=[
                SimpleNamespace(name="public"),
                SimpleNamespace(name="hidden"),
            ]
        )
        request = SimpleNamespace(
            tools_dict={"public": public, "hidden": hidden},
            config=SimpleNamespace(tools=[group]),
        )
        binding = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(binding):
                _project_model_tools(request)
        finally:
            revoke_agent_principal(binding)

        self.assertEqual(tuple(request.tools_dict), ("public",))
        self.assertEqual(
            [item.name for item in group.function_declarations], ["public"]
        )

    def test_real_framework_wrappers_retain_permission_metadata(self):
        from google.adk.tools import FunctionTool
        from langchain.agents.middleware.types import ModelRequest

        @tool
        def public():
            """Return public data."""

        @tool(permission="records.private")
        def private():
            """Return private data."""

        langgraph_tools = _langchain_tools((public, private))
        langgraph_request = ModelRequest(
            model=object(), messages=[], tools=langgraph_tools
        )
        adk_request = SimpleNamespace(
            tools_dict={
                "public": FunctionTool(public),
                "private": FunctionTool(private),
            },
            config=SimpleNamespace(tools=[]),
        )
        binding = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(binding):
                langgraph_projected = _project_principal_tools(langgraph_request)
                _project_model_tools(adk_request)
        finally:
            revoke_agent_principal(binding)

        self.assertEqual(
            [item.name for item in langgraph_projected.tools], ["public"]
        )
        self.assertEqual(tuple(adk_request.tools_dict), ("public",))

    async def test_managed_adk_invocation_projects_tools_at_model_boundary(self):
        @tool
        def public():
            """Return public data."""

        @tool(permission="records.private")
        def private():
            """Return private data."""

        _ADKProjectionModel.observed_tools.clear()
        agent = LlmAgent(
            name="projection",
            model=_ADKProjectionModel(model="projection"),
            instruction="Answer without calling a tool.",
            tools=[public, private],
        )
        application = CompiledApplication(
            name="projection",
            framework="adk",
            mode="managed",
            target=agent,
            native_app=App(name="projection", root_agent=agent),
        )
        driver = ExtensionRuntimeDriver(ADKRuntimeDriver(application), [])
        try:
            await driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            await driver.invoke(_request(AgentRuntimePrincipal.create()))
        finally:
            await driver.close()

        self.assertEqual(_ADKProjectionModel.observed_tools, [("public",)])

    async def test_managed_langgraph_invocation_projects_tools_at_model_boundary(self):
        @tool
        def public():
            """Return public data."""

        @tool(permission="records.private")
        def private():
            """Return private data."""

        _LangGraphProjectionModel.observed_tools.clear()
        definition = Agent(
            name="projection",
            model=_LangGraphProjectionModel(),
            instruction="Answer without calling a tool.",
            tools=(public, private),
        )
        application = CompiledApplication(
            name="projection",
            framework="langgraph",
            mode="managed",
            target=build_agent(definition, checkpointer=MemorySaver()),
        )
        driver = ExtensionRuntimeDriver(LangGraphRuntimeDriver(application), [])
        try:
            await driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            await driver.invoke(_request(AgentRuntimePrincipal.create()))
        finally:
            await driver.close()

        self.assertEqual(_LangGraphProjectionModel.observed_tools, [("public",)])

    async def test_adk_mcp_discovery_applies_client_and_tool_permissions(self):
        discovered = 0

        class Toolset:
            async def get_tools(self, _readonly_context=None):
                nonlocal discovered
                discovered += 1
                return [
                    SimpleNamespace(name="read"),
                    SimpleNamespace(name="write"),
                ]

        client = MCPClient.sse(
            "https://mcp.invalid/sse",
            prefix="remote",
            permission="billing.connect",
            tool_permissions={"write": "billing.write"},
        )
        governed = client._adk_toolset_type(Toolset)()
        denied = create_agent_principal_binding(AgentRuntimePrincipal.create())
        allowed = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"billing.connect"})
        )
        try:
            with activate_agent_principal(denied):
                self.assertEqual(await governed.get_tools(), [])
            with activate_agent_principal(allowed):
                tools = await governed.get_tools()
        finally:
            revoke_agent_principal(denied)
            revoke_agent_principal(allowed)

        self.assertEqual(discovered, 1)
        self.assertEqual([item.name for item in tools], ["read"])

    async def test_adk_mcp_permissions_use_unprefixed_remote_names(self):
        class Toolset:
            async def get_tools(self, _readonly_context=None):
                # ADK adds the configured prefix in get_tools_with_prefix(),
                # after the governed get_tools() method returns.
                return [SimpleNamespace(name="remote_write")]

        client = MCPClient.sse(
            "https://mcp.invalid/sse",
            prefix="remote",
            tool_permissions={"remote_write": "billing.write"},
        )
        governed = client._adk_toolset_type(Toolset)()
        binding = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"billing.write"})
        )
        try:
            with activate_agent_principal(binding):
                tools = await governed.get_tools()
        finally:
            revoke_agent_principal(binding)

        self.assertEqual([item.name for item in tools], ["remote_write"])

    async def test_context_mcp_projects_tools_and_empty_clients(self):
        async def operation(arguments):
            return arguments

        read = _managed_mcp_tool(
            "billing", "read", operation, required_permissions=("billing.read",)
        )
        write = _managed_mcp_tool(
            "billing", "write", operation, required_permissions=("billing.write",)
        )
        admin = _managed_mcp_tool(
            "admin", "delete", operation, required_permissions=("admin.delete",)
        )
        active = create_agent_context(
            framework="langgraph",
            agent_name="root",
            invocation_id="invocation-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
            resources={},
        )
        binding = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"billing.read"})
        )
        try:
            with activate_context(active), activate_agent_principal(
                binding
            ), _activate_mcp_context(
                {
                    "billing": {"read": read, "write": write},
                    "admin": {"delete": admin},
                }
            ):
                self.assertEqual(
                    await mcp("billing").call_tool("read", {"page": 1}),
                    {"page": 1},
                )
                with self.assertRaises(MCPToolUnavailableError):
                    await mcp("billing").call_tool("write")
                with self.assertRaises(MCPClientUnavailableError):
                    mcp("admin")
        finally:
            revoke_context(active)
            revoke_agent_principal(binding)


if __name__ == "__main__":
    unittest.main()

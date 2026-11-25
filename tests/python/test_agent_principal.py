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

from harnest.frameworks.adk.mcp_tools import toolset_type
from harnest.agent import AgentRuntimePermissionError, AgentRuntimePrincipal
from harnest.agent_principal import (
    activate_agent_principal,
    active_agent_principal,
    attach_required_permissions,
    create_agent_principal_binding,
    resolve_nested_agent_principal,
    revoke_agent_principal,
)
from harnest.agent import Agent
from harnest.frameworks.langgraph.lowering import (
    build_agent,
    build_graph,
    _graph_agent_principal_projection_complete,
    _langchain_tools,
    _project_principal_tools,
)
from harnest.application import CompiledApplication
from harnest.agent.approval import require_human_approval
from harnest.client_tool import client_tool
from harnest._context_agent import _resolve_invocation_agent_principal
from harnest import context
from harnest.context import activate_context, create_agent_context, revoke_context
from harnest.mcp import MCPClient, MCPToolAnnotations, MCPToolInfo, mcp_tool_info_from_remote
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
from harnest.lifecycle_runtime import LifecycleRuntimeDriver
from harnest.frameworks.adk.runtime import ADKRuntimeDriver
from harnest.frameworks.langgraph.runtime import LangGraphRuntimeDriver
from harnest.agent import tool
from harnest.frameworks.adk.tools import _project_model_tools


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

    async def test_untagged_tool_is_denied_with_an_active_principal(self):
        """Treat principal presence as an explicit default-deny boundary."""

        @tool
        async def inspect_account():
            """Inspect one account."""

            return "inspected"

        binding = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(binding):
                with self.assertRaises(AgentRuntimePermissionError):
                    await inspect_account()
        finally:
            revoke_agent_principal(binding)

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
        wrapped = LifecycleRuntimeDriver(driver, [])
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
        wrapped = LifecycleRuntimeDriver(driver, [])
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
        wrapped = LifecycleRuntimeDriver(LangGraphRuntimeDriver(application), [])
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
        wrapped = LifecycleRuntimeDriver(driver, [])
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
        wrapped = LifecycleRuntimeDriver(driver, [])
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

        self.assertEqual([item.name for item in projected.tools], ["read"])

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

        self.assertEqual(tuple(request.tools_dict), ())
        self.assertEqual(
            [item.name for item in group.function_declarations], []
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

        self.assertEqual([item.name for item in langgraph_projected.tools], [])
        self.assertEqual(tuple(adk_request.tools_dict), ())

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
            framework_app=App(name="projection", root_agent=agent),
        )
        driver = LifecycleRuntimeDriver(ADKRuntimeDriver(application), [])
        try:
            await driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            await driver.invoke(_request(AgentRuntimePrincipal.create()))
        finally:
            await driver.close()

        self.assertEqual(_ADKProjectionModel.observed_tools, [()])

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
        driver = LifecycleRuntimeDriver(LangGraphRuntimeDriver(application), [])
        try:
            await driver.create_session(
                session_id="session-1", user_id="user-1", state={}
            )
            await driver.invoke(_request(AgentRuntimePrincipal.create()))
        finally:
            await driver.close()

        self.assertEqual(_LangGraphProjectionModel.observed_tools, [])

    async def test_adk_mcp_untagged_client_is_not_discovered_with_principal(self):
        """Deny an untagged MCP client before making its discovery request."""

        discovered = 0

        class Toolset:
            async def get_tools(self, _readonly_context=None):
                nonlocal discovered
                discovered += 1
                return [SimpleNamespace(name="read")]

        client = MCPClient.sse("https://mcp.invalid/sse")
        governed = toolset_type(client, Toolset)()
        binding = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(binding):
                self.assertEqual(await governed.get_tools(), [])
        finally:
            revoke_agent_principal(binding)

        self.assertEqual(discovered, 0)

    async def test_adk_mcp_discovery_applies_client_and_tool_permissions(self):
        discovered = 0

        class Toolset:
            async def get_tools(self, _readonly_context=None):
                nonlocal discovered
                discovered += 1
                from harnest.mcp_resources import DiscoveredMCPTools
                return DiscoveredMCPTools([
                    SimpleNamespace(name="read"),
                    SimpleNamespace(name="write"),
                ])

        client = MCPClient.sse(
            "https://mcp.invalid/sse",
            prefix="remote",
            permission="billing.connect",
            tool_permissions={"write": "billing.write"},
        )
        governed = toolset_type(client, Toolset)()
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
                from harnest.mcp_resources import DiscoveredMCPTools
                return DiscoveredMCPTools([SimpleNamespace(name="remote_write")])

        client = MCPClient.sse(
            "https://mcp.invalid/sse",
            prefix="remote",
            tool_permissions={"remote_write": "billing.write"},
        )
        governed = toolset_type(client, Toolset)()
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

    def test_dynamic_tool_permissions_authoring_validation_rejects_invalid_classifiers(self):
        """Reject non-mappings, classes, and async callables at authoring time."""

        async def async_classifier(_tool: MCPToolInfo) -> str:
            """Async function that must be rejected for sync permission resolution."""
            return "catalog.read"

        class AsyncCallableClassifier:
            """Callable instance with an async __call__ that must also be rejected."""

            async def __call__(self, _tool: MCPToolInfo) -> str:
                return "catalog.read"

        class ClassClassifier:
            """Class object that must not be accepted as a classifier function."""

            def __init__(self, _tool: MCPToolInfo) -> None:
                pass

        for bad in (123, ["catalog.read"], async_classifier, AsyncCallableClassifier(), ClassClassifier):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                MCPClient.streamable_http(
                    "https://mcp.invalid/mcp",
                    tool_permissions=bad,  # type: ignore[arg-type]
                )

    def test_dynamic_tool_permissions_classify_by_name_annotations_and_multi_grant(self):
        """Verify name patterns, read_only hints, multiple grants, client permission, and fail-closed errors."""

        def classify_catalog(tool: MCPToolInfo) -> str | tuple[str, ...] | None:
            """Classify read, write, audited, and denied tools from name and hints."""
            # Audit-sensitive tools require both read and audit grants simultaneously.
            if tool.name == "export_audit":
                return ("catalog.read", "audit.log")
            # Server-asserted read-only tools and read prefixes need only read permission.
            if tool.annotations.read_only is True or tool.name.startswith(("get_", "list_")):
                return "catalog.read"
            # Destructive or unmapped tools explicitly return None to stay default-deny.
            if tool.name == "drop_all":
                return None
            return "catalog.write"

        client = MCPClient.streamable_http(
            "https://mcp.invalid/mcp",
            permission="catalog.connect",
            tool_permissions=classify_catalog,
        )

        self.assertEqual(
            client.permissions_for_tool(MCPToolInfo(name="get_item")),
            ("catalog.connect", "catalog.read"),
        )
        self.assertEqual(
            client.permissions_for_tool(
                MCPToolInfo(
                    name="inspect_catalog",
                    annotations=MCPToolAnnotations(read_only=True),
                )
            ),
            ("catalog.connect", "catalog.read"),
        )
        self.assertEqual(
            client.permissions_for_tool(MCPToolInfo(name="update_item")),
            ("catalog.connect", "catalog.write"),
        )
        self.assertEqual(
            client.permissions_for_tool(MCPToolInfo(name="export_audit")),
            ("catalog.connect", "catalog.read", "audit.log"),
        )
        self.assertEqual(
            client.permissions_for_tool(MCPToolInfo(name="drop_all")),
            ("catalog.connect",),
        )
        # Without client-wide permission, returning None or () leaves the tool untagged (default-deny).
        no_client_perm = MCPClient.stdio(
            "python",
            "server.py",
            tool_permissions=lambda info: None if info.name == "drop_all" else (),
        )
        self.assertEqual(no_client_perm.permissions_for_tool("drop_all"), ())
        self.assertEqual(no_client_perm.permissions_for_tool("empty_tuple"), ())

    def test_dynamic_tool_permissions_fail_closed_on_bad_return_or_exception(self):
        """Propagate classifier errors and reject malformed permission return values."""

        async def _unused_coro() -> str:
            """Produce a coroutine object to test runtime rejection of async return values."""
            return "catalog.read"

        def bad_return_type(_tool: MCPToolInfo):
            """Return an unsupported non-string type."""
            return 123

        def bad_bytes(_tool: MCPToolInfo):
            """Return bytes, which is a Sequence but not valid permission strings."""
            return b"catalog.read"

        def bad_permission_name(_tool: MCPToolInfo) -> str:
            """Return a string that violates the permission identifier grammar."""
            return "bad permission"

        def bad_coro(_tool: MCPToolInfo):
            """Return an unawaited coroutine object from a synchronous lambda."""
            return _unused_coro()

        def raising_classifier(_tool: MCPToolInfo) -> str:
            """Raise an application error that must not be swallowed into allow."""
            raise RuntimeError("classifier failed")

        for classifier, expected in (
            (bad_return_type, TypeError),
            (bad_bytes, TypeError),
            (bad_coro, TypeError),
            (bad_permission_name, ValueError),
            (raising_classifier, RuntimeError),
        ):
            client = MCPClient.sse(
                "https://mcp.invalid/sse",
                tool_permissions=classifier,  # type: ignore[arg-type]
            )
            with self.subTest(classifier=classifier.__name__), self.assertRaises(expected):
                client.permissions_for_tool(MCPToolInfo(name="get_item"))

    def test_mcp_tool_info_from_remote_extracts_annotations_from_all_shapes(self):
        """Normalize MCP annotations across dicts, MCP SDK objects, ADK tools, and LangChain tools."""

        from mcp import types as mcp_types
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
        from harnest.frameworks.adk.mcp_tools import http_mcp_tool

        sdk_tool = mcp_types.Tool(
            name="search_products",
            description="Search the product catalog.",
            inputSchema={"type": "object", "properties": {}},
            annotations=mcp_types.ToolAnnotations(
                title="Search Products",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        dict_info = mcp_tool_info_from_remote(
            sdk_tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        adk_tool = http_mcp_tool(sdk_tool, SimpleNamespace())
        adk_info = mcp_tool_info_from_remote(adk_tool)
        lc_tool = convert_mcp_tool_to_langchain_tool(
            SimpleNamespace(),
            sdk_tool,
            server_name="catalog",
            tool_name_prefix=True,
        )
        lc_info = mcp_tool_info_from_remote(lc_tool, name="search_products")

        expected = MCPToolInfo(
            name="search_products",
            description="Search the product catalog.",
            annotations=MCPToolAnnotations(
                title="Search Products",
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=False,
            ),
        )
        self.assertEqual(dict_info, expected)
        self.assertEqual(adk_info, expected)
        self.assertEqual(lc_info, expected)

    async def test_adk_mcp_discovery_enforces_dynamic_tool_permissions(self):
        """Discover and filter ADK MCP tools with a dynamic classifier even without client permission."""

        from mcp import types as mcp_types
        from harnest.frameworks.adk.mcp_tools import http_mcp_tool
        from harnest.mcp_resources import DiscoveredMCPTools

        discovered = 0
        read_schema = mcp_types.Tool(
            name="search_items",
            description="Read items.",
            inputSchema={"type": "object", "properties": {}},
            annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
        )
        write_schema = mcp_types.Tool(
            name="update_item",
            description="Update one item.",
            inputSchema={"type": "object", "properties": {}},
            annotations=mcp_types.ToolAnnotations(readOnlyHint=False),
        )
        audit_schema = mcp_types.Tool(
            name="export_audit",
            description="Export audit report.",
            inputSchema={"type": "object", "properties": {}},
        )
        hidden_schema = mcp_types.Tool(
            name="delete_everything",
            description="Dangerous unmapped operation.",
            inputSchema={"type": "object", "properties": {}},
        )

        class Toolset:
            """Fake ADK toolset yielding real HTTPTool wrappers with MCP annotations."""

            async def get_tools(self, _readonly_context=None):
                nonlocal discovered
                discovered += 1
                calls = SimpleNamespace()
                return DiscoveredMCPTools(
                    [
                        http_mcp_tool(read_schema, calls),
                        http_mcp_tool(write_schema, calls),
                        http_mcp_tool(audit_schema, calls),
                        http_mcp_tool(hidden_schema, calls),
                    ]
                )

        def classify(tool: MCPToolInfo) -> str | tuple[str, ...] | None:
            """Map readOnlyHint, multi-grant audit, write, and None (default-deny)."""
            # Read-only annotated tools require only catalog.read.
            if tool.annotations.read_only is True:
                return "catalog.read"
            # Audit exports require two grants simultaneously.
            if tool.name == "export_audit":
                return ("catalog.read", "audit.log")
            # Explicitly returning None keeps dangerous tools denied under a principal.
            if tool.name.startswith("delete_"):
                return None
            return "catalog.write"

        client = MCPClient.sse(
            "https://mcp.invalid/sse",
            prefix="catalog",
            tool_permissions=classify,
        )
        governed = toolset_type(client, Toolset)()
        read_only = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"catalog.read"})
        )
        read_and_audit = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"catalog.read", "audit.log"})
        )
        empty = create_agent_principal_binding(AgentRuntimePrincipal.create())
        try:
            with activate_agent_principal(read_only):
                read_tools = await governed.get_tools()
            with activate_agent_principal(read_and_audit):
                audit_tools = await governed.get_tools()
            with activate_agent_principal(empty):
                empty_tools = await governed.get_tools()
        finally:
            revoke_agent_principal(read_only)
            revoke_agent_principal(read_and_audit)
            revoke_agent_principal(empty)

        self.assertEqual(discovered, 3)
        self.assertEqual([item.name for item in read_tools], ["search_items"])
        self.assertEqual(
            [item.name for item in audit_tools], ["search_items", "export_audit"]
        )
        self.assertEqual(empty_tools, [])

    def test_langgraph_mcp_discovery_enforces_dynamic_tool_permissions(self):
        """Attach dynamic classifier permissions on LangChain MCP tools and project them by principal."""

        from mcp import types as mcp_types
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
        from harnest.frameworks.langgraph.runtime import _apply_mcp_permissions

        schemas = (
            mcp_types.Tool(
                name="get_customer",
                description="Fetch one customer.",
                inputSchema={"type": "object", "properties": {}},
                annotations=mcp_types.ToolAnnotations(readOnlyHint=True),
            ),
            mcp_types.Tool(
                name="update_customer",
                description="Modify one customer.",
                inputSchema={"type": "object", "properties": {}},
            ),
            mcp_types.Tool(
                name="export_customers",
                description="Bulk export.",
                inputSchema={"type": "object", "properties": {}},
            ),
            mcp_types.Tool(
                name="purge_customers",
                description="Unclassified dangerous operation.",
                inputSchema={"type": "object", "properties": {}},
            ),
        )
        lc_tools = [
            convert_mcp_tool_to_langchain_tool(
                SimpleNamespace(),
                schema,
                server_name="crm",
                tool_name_prefix=True,
            )
            for schema in schemas
        ]
        seen_names: list[str] = []

        def classify(tool: MCPToolInfo) -> str | tuple[str, ...] | None:
            """Record canonical remote names and classify tools dynamically."""
            seen_names.append(tool.name)
            # Server-asserted read-only tools need only crm.read.
            if tool.annotations.read_only is True:
                return "crm.read"
            # Bulk exports require both read and export permissions.
            if tool.name == "export_customers":
                return ("crm.read", "crm.export")
            # Returning None leaves the tool untagged (default-deny).
            if tool.name == "purge_customers":
                return None
            return "crm.write"

        client = MCPClient.streamable_http(
            "https://mcp.invalid/mcp",
            permission="crm.connect",
            tool_permissions=classify,
        )
        _apply_mcp_permissions(lc_tools, "crm", client)
        # Classifier must receive unprefixed canonical remote names.
        self.assertEqual(
            seen_names,
            ["get_customer", "update_customer", "export_customers", "purge_customers"],
        )

        class Request:
            """Minimal ModelRequest stub for _project_principal_tools."""

            tools = lc_tools

            def override(self, *, tools):
                """Return a projected request carrying only allowed tools."""
                return SimpleNamespace(tools=tools)

        # Without client-wide crm.connect, even crm.read is insufficient.
        missing_connect = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"crm.read"})
        )
        read_principal = create_agent_principal_binding(
            AgentRuntimePrincipal.create(permissions={"crm.connect", "crm.read"})
        )
        export_principal = create_agent_principal_binding(
            AgentRuntimePrincipal.create(
                permissions={"crm.connect", "crm.read", "crm.export"}
            )
        )
        try:
            with activate_agent_principal(missing_connect):
                self.assertEqual(_project_principal_tools(Request()).tools, [])
            with activate_agent_principal(read_principal):
                # purge_customers returned None, so only crm.connect applies; wait — check explicit no-client-permission default-deny below!
                read_names = [item.name for item in _project_principal_tools(Request()).tools]
            with activate_agent_principal(export_principal):
                export_names = [item.name for item in _project_principal_tools(Request()).tools]
        finally:
            revoke_agent_principal(missing_connect)
            revoke_agent_principal(read_principal)
            revoke_agent_principal(export_principal)

        self.assertEqual(read_names, ["crm_get_customer", "crm_purge_customers"])
        self.assertEqual(
            export_names,
            ["crm_get_customer", "crm_export_customers", "crm_purge_customers"],
        )

        # Without a client-wide permission, a tool returning None is strictly hidden under a principal.
        no_base_client = MCPClient.streamable_http(
            "https://mcp.invalid/mcp",
            tool_permissions=classify,
        )
        _apply_mcp_permissions(lc_tools, "crm", no_base_client)
        wide_principal = create_agent_principal_binding(
            AgentRuntimePrincipal.create(
                permissions={"crm.read", "crm.write", "crm.export"}
            )
        )
        try:
            with activate_agent_principal(wide_principal):
                visible = [item.name for item in _project_principal_tools(Request()).tools]
        finally:
            revoke_agent_principal(wide_principal)
        self.assertEqual(
            visible,
            ["crm_get_customer", "crm_update_customer", "crm_export_customers"],
        )


if __name__ == "__main__":
    unittest.main()

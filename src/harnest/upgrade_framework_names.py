"""Reviewed compatibility inventory for the framework-adapter project migration."""

from __future__ import annotations


SYMBOL_RENAMES = {
    "harnest.lifecycle.adk_plugin": "harnest.lifecycle.framework_extension",
    "harnest.lifecycle.langgraph_middleware": "harnest.lifecycle.framework_extension",
    "harnest.lifecycle.native_extension": "harnest.lifecycle.framework_extension",
    "harnest.model.resolve_model_for": "harnest.model.resolve_model",
    "harnest.checkpoint.ADKStore": "harnest.frameworks.adk.ADKStore",
    "harnest.checkpoint.LangGraphStore": "harnest.frameworks.langgraph.LangGraphStore",
    "harnest.checkpoint.NativeStorage": "harnest.checkpoint.FrameworkStorage",
    "harnest.session.ADKSessionStorage": "harnest.frameworks.adk.ADKSessionStorage",
    "harnest.runtime.load_compiled_app": "harnest.runtime.load_framework_app",
    "harnest.runtime.load_native_app": "harnest.runtime.load_framework_app",
    "harnest.durable.NativeDurableCall": "harnest.durable.FrameworkDurableCall",
    "harnest.durable.NativeDurableSuspended": "harnest.durable.FrameworkDurableSuspended",
    "harnest.durable.NativeResumeInput": "harnest.durable.FrameworkResumeInput",
    "harnest.durable.native_durable_call": "harnest.durable.framework_durable_call",
    "harnest.durable.current_native_durable_call": "harnest.durable.current_framework_durable_call",
    "harnest.durable.is_native_suspension": "harnest.durable.is_framework_suspension",
    "harnest.frameworks.Capability.NATIVE_API_SERVER": "harnest.frameworks.Capability.FRAMEWORK_API_SERVER",
    "harnest.frameworks.Capability.NATIVE_SESSION_SERVICE": "harnest.frameworks.Capability.FRAMEWORK_SESSION_SERVICE",
    "harnest.frameworks.base.Capability.NATIVE_API_SERVER": "harnest.frameworks.base.Capability.FRAMEWORK_API_SERVER",
    "harnest.frameworks.base.Capability.NATIVE_SESSION_SERVICE": "harnest.frameworks.base.Capability.FRAMEWORK_SESSION_SERVICE",
}
ADAPTER_TYPES = frozenset({"harnest.frameworks.FrameworkAdapter", "harnest.frameworks.base.FrameworkAdapter"})
ADAPTER_RENAMES = {
    "native_extensions": "framework_extensions",
    "native_storage": "framework_storage",
    "exposes_native_server": "exposes_framework_server",
    "build_native_server": "build_framework_server",
    "native_extension_type": "framework_extension_type",
    "native_extension_label": "framework_extension_label",
    "native_session_store_label": "framework_session_store_label",
    "native_code_executor_label": "framework_code_executor_label",
    "is_native_code_executor": "is_framework_code_executor",
    "accepts_native_session_store": "accepts_framework_session_store",
    "validate_native_extensions": "validate_framework_extensions",
}
CAPABILITY_RENAMES = {
    "native.adk": "framework.extension",
    "native.langgraph": "framework.extension",
    "native.extension": "framework.extension",
}
CAPABILITY_VALUE_RENAMES = {
    "native-api-server": "framework-api-server",
    "native-session-service": "framework-session-service",
}
REMOVED_SYMBOLS = {
    "harnest.evaluation.adk_eval_agent_module": "use Harnest's evaluation entrypoint",
    "harnest.durable.adk_durable_tool": "declare @tool(durable=True) and let the compiler adapt it",
    "harnest.durable.langgraph_durable_callable": "declare @tool(durable=True) and let the compiler adapt it",
    "harnest.backends.Backend": "implement the current FrameworkAdapter contract manually",
}
REMOVED_METHODS = {
    "to_adk_toolset": "declare MCP clients on the agent or use context.mcp",
    "to_adk_executor": "declare a named sandbox and use context.sandboxes",
    "to_langchain_tool": "declare a named sandbox and use context.sandboxes",
}
METHOD_RENAMES = {
    "build_langgraph": "build",
    "build_for": "build",
    "to_langgraph_connection": "connection_options",
    "as_langgraph_checkpointer": "framework_checkpointer",
}
MODEL_TYPES = frozenset({"harnest.model.LiteLLMModel", "harnest.model.ModelConnector"})
GRAPH_TYPES = frozenset({"harnest.graph.Graph"})
MCP_TYPES = frozenset({"harnest.mcp.MCPClient", "harnest_fused.FusedMCPClient"})
STORE_TYPES = frozenset({
    "harnest.checkpoint.HarnestStore", "harnest.checkpoint.MemoryStore",
    "harnest.store.HarnestStore", "harnest.store.MemoryStore",
    "harnest.store.PostgresStore", "harnest.store.RedisStore",
    "harnest_postgres.PostgresStore", "harnest_redis.RedisStore",
})
FIELD_RENAMES = {
    "harnest.application.CompiledApplication": {
        "native_app": "framework_app", "native_extensions": "framework_extensions",
        "native_checkpointer": "framework_checkpointer",
    },
    "harnest.frameworks.AdvancedTarget": {"native_app": "framework_app"},
    "harnest.frameworks.base.AdvancedTarget": {"native_app": "framework_app"},
    "harnest.frameworks.RuntimeSessions": {"native": "framework_service"},
    "harnest.frameworks.base.RuntimeSessions": {"native": "framework_service"},
    "harnest.durable.ResumeArtifact": {"native_invocation_id": "framework_invocation_id"},
}
SERVER_KEYWORDS = {
    "adk_session_storage": "framework_session_storage",
    "native_session_storage": "framework_session_storage",
    "langgraph_session_store": "session_store",
}
CONSTRUCTORS = MODEL_TYPES | GRAPH_TYPES | MCP_TYPES | STORE_TYPES | frozenset(FIELD_RENAMES) | frozenset({
    "harnest.agent.Agent", "harnest.agent.AgentDefinition", "harnest.sandbox.Sandbox",
})
# Retired helper modules moved implementation, not a supported import contract.
# Reporting them avoids inventing replacements for changed helper signatures.
RETIRED_MODULES = frozenset({
    "harnest.backends", "harnest.backends.adk", "harnest.backends.langgraph",
    "harnest.runtime_adk", "harnest.runtime_langgraph", "harnest.runtime_langgraph_content",
    "harnest.checkpoint_langgraph", "harnest.session_adk", "harnest.context_adk",
    "harnest.credentials_adk", "harnest.agent_scope_adk", "harnest.agent_plugin_langgraph",
    "harnest.eval_adk", "harnest.eval_adk_cleanup", "harnest.eval_langgraph",
    "harnest.mcp_adk", "harnest.sandbox_adk", "harnest.token_adk", "harnest.token_langgraph",
    "harnest.tool_adk", "harnest.skill_selection_adapters", "harnest._adk_warnings",
})

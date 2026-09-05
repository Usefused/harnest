import asyncio
import importlib.util
import inspect
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from harnest import (
    Agent,
    AgentSource,
    BundleConventionError,
    BundleDuplicateError,
    BundleEvalError,
    BundleExportError,
    BundleImportError,
    BundleSkillError,
    EvalSuite,
    LiteLLMModel,
    MCPClient,
    OllamaModel,
    Orchestrator,
    bundle_agent,
    compile_agent,
    compile_artifact,
    discover_evals,
    instruction_file,
    tool,
)
from harnest.cli import load_orchestrator, main as cli_main
from harnest.runtime import create_fastapi_app, run_agent_message
from harnest.server_config import DEFAULT_SERVER_YAML
from harnest.testing import (
    AgentTestError,
    _EvalResultCollector,
    _adk_eval_output_filter,
    _eval_case_result_payload,
    _eval_config,
    _evaluate_eval_sets,
    _isolated_eval_logging,
    _run_adk_evals,
    _run_langgraph_evals,
    run_agent_tests,
)
from _session_store_fixture import write_session_store


def _recording_class(name):
    class RecordingClass:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.__dict__.update(kwargs)

    RecordingClass.__name__ = name
    return RecordingClass


def _deterministic_adk_source(*, advanced):
    if advanced:
        root = (
            "Agent.advanced(LlmAgent("
            "name='root', model=DeterministicLlm(model='deterministic'), "
            "instruction='Answer clearly.'))"
        )
    else:
        root = (
            "Agent(name='root', "
            "model=DeterministicLlm(model='deterministic'))"
        )
    return (
        "from harnest.agent import Agent\n"
        "from google.adk.agents import LlmAgent\n"
        "from google.adk.models import BaseLlm, LlmResponse\n"
        "from google.genai import types\n\n"
        "class DeterministicLlm(BaseLlm):\n"
        "    async def generate_content_async(self, llm_request, stream=False):\n"
        "        yield LlmResponse(content=types.Content(\n"
        "            role='model',\n"
        "            parts=[types.Part(text='official response')],\n"
        "        ))\n\n"
        f"root_agent = {root}\n"
    )


def _event_of_type(events, event_type):
    """Return one event by type so integration tests stay order-independent."""

    for event in events:
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"missing event type {event_type!r}")


def _fake_adk_modules(*, public_mcp_exports=True):
    """Isolate every supported ADK import path from previously loaded real modules."""
    google = types.ModuleType("google")
    google.__path__ = []
    adk = types.ModuleType("google.adk")
    adk.__path__ = []
    agents = types.ModuleType("google.adk.agents")
    agents.LlmAgent = _recording_class("LlmAgent")
    apps = types.ModuleType("google.adk.apps")
    apps.App = _recording_class("App")
    models = types.ModuleType("google.adk.models")
    models.__path__ = []
    models.BaseLlm = _recording_class("BaseLlm")
    lite_llm = types.ModuleType("google.adk.models.lite_llm")
    lite_llm.LiteLlm = _recording_class("LiteLlm")
    tools_package = types.ModuleType("google.adk.tools")
    tools_package.__path__ = []
    mcp_tool = types.ModuleType("google.adk.tools.mcp_tool")
    mcp_tool.__path__ = []
    mcp_tool.McpToolset = _recording_class("McpToolset")
    toolset_module = types.ModuleType("google.adk.tools.mcp_tool.mcp_toolset")
    toolset_module.McpToolset = mcp_tool.McpToolset
    session_manager = types.ModuleType(
        "google.adk.tools.mcp_tool.mcp_session_manager"
    )
    for class_name in (
        "SseConnectionParams",
        "StdioConnectionParams",
        "StreamableHTTPConnectionParams",
    ):
        connection_class = _recording_class(class_name)
        setattr(session_manager, class_name, connection_class)
        if public_mcp_exports:
            setattr(mcp_tool, class_name, connection_class)
    mcp = types.ModuleType("mcp")
    mcp.StdioServerParameters = _recording_class("StdioServerParameters")
    evaluation = types.ModuleType("google.adk.evaluation")
    evaluation.__path__ = []
    eval_set_module = types.ModuleType("google.adk.evaluation.eval_set")
    eval_config_module = types.ModuleType("google.adk.evaluation.eval_config")

    class EvalSet:
        @classmethod
        def model_validate_json(cls, payload):
            data = json.loads(payload)
            return types.SimpleNamespace(
                eval_set_id=data["eval_set_id"],
                eval_cases=[
                    types.SimpleNamespace(eval_id=item["eval_id"])
                    for item in data["eval_cases"]
                ],
            )

    class EvalConfig:
        @classmethod
        def model_validate_json(cls, payload):
            return json.loads(payload)

    eval_set_module.EvalSet = EvalSet
    eval_config_module.EvalConfig = EvalConfig
    return {
        "google": google,
        "google.adk": adk,
        "google.adk.agents": agents,
        "google.adk.apps": apps,
        "google.adk.models": models,
        "google.adk.models.lite_llm": lite_llm,
        "google.adk.tools": tools_package,
        "google.adk.tools.mcp_tool": mcp_tool,
        "google.adk.tools.mcp_tool.mcp_toolset": toolset_module,
        "google.adk.tools.mcp_tool.mcp_session_manager": session_manager,
        "google.adk.evaluation": evaluation,
        "google.adk.evaluation.eval_set": eval_set_module,
        "google.adk.evaluation.eval_config": eval_config_module,
        "mcp": mcp,
    }


class AuthoringTests(unittest.TestCase):
    def test_eval_logging_scope_removes_third_party_handlers(self):
        logger = logging.getLogger("harnest.test.eval.lifecycle")
        handler = logging.NullHandler()
        original_level = logger.level

        with _isolated_eval_logging():
            logger.addHandler(handler)
            logger.setLevel(logging.CRITICAL)
            self.assertIn(handler, logger.handlers)

        self.assertNotIn(handler, logger.handlers)
        self.assertEqual(logger.level, original_level)

    def test_eval_logging_scope_drops_closed_pytest_capture_stream(self):
        logger = logging.getLogger("harnest.agent.eval.closed_capture")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        stream.close()

        with _isolated_eval_logging():
            self.assertNotIn(handler, logger.handlers)
            logger.warning("native.plugin.teardown")

        self.assertNotIn(handler, logger.handlers)

    def _write(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        if path.name == "agent.py" and "subagents" not in path.parts:
            write_session_store(path.parent)

    def test_tool_preserves_function_and_requires_description(self):
        @tool
        def add(left: int, right: int) -> int:
            """Add two integers."""
            return left + right

        self.assertEqual(add(2, 3), 5)
        self.assertTrue(add.__harnest_tool__)

        with self.assertRaisesRegex(ValueError, "needs a docstring"):
            tool(lambda: None)

    def test_instruction_file_is_relative_to_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            anchor.write_text("# test", encoding="utf-8")
            (root / "instructions.md").write_text("  Be useful.\n", encoding="utf-8")
            self.assertEqual(instruction_file(anchor), "Be useful.")

    def test_agent_definition_validates_without_importing_adk(self):
        definition = Agent(name="helper", model="gemini-test", instruction="Help.")
        self.assertEqual(definition.name, "helper")
        self.assertEqual(definition.history, "session")
        with self.assertRaisesRegex(ValueError, "agent name"):
            Agent(name="bad-name", model="gemini-test", instruction="Help.")
        with self.assertRaisesRegex(ValueError, "agent name"):
            Agent(name="123helper", model="gemini-test", instruction="Help.")
        with self.assertRaisesRegex(ValueError, "agent name"):
            Agent(
                name="h\N{LATIN SMALL LETTER E WITH ACUTE}lper",
                model="gemini-test",
                instruction="Help.",
            )
        with self.assertRaisesRegex(TypeError, "tools must be a sequence"):
            Agent(name="helper", model="gemini-test", instruction="Help.", tools="search")
        with self.assertRaisesRegex(ValueError, "history"):
            Agent(
                name="helper",
                model="gemini-test",
                instruction="Help.",
                history="messages",
            )

    def test_agent_history_maps_to_explicit_adk_conversation_modes(self):
        modules = _fake_adk_modules()
        with patch.dict(sys.modules, modules):
            session_agent = Agent(
                name="session_agent",
                model="gemini-test",
                instruction="Remember.",
            ).build()
            turn_agent = Agent(
                name="turn_agent",
                model="gemini-test",
                instruction="Focus.",
                history="turn",
            ).build()

        self.assertEqual(session_agent.kwargs["mode"], "chat")
        self.assertEqual(session_agent.kwargs["include_contents"], "default")
        self.assertEqual(turn_agent.kwargs["mode"], "chat")
        self.assertEqual(turn_agent.kwargs["include_contents"], "none")

    def test_agent_advanced_is_the_only_public_advanced_boundary(self):
        target = object()
        input_adapter = lambda text, state: {**state, "input": text}
        output_adapter = lambda result: result["output"]

        advanced = Agent.advanced(
            target,
            name="custom",
            input_adapter=input_adapter,
            output_adapter=output_adapter,
        )

        self.assertIs(advanced.target, target)
        self.assertEqual(advanced.name, "custom")
        self.assertIs(advanced.input_adapter, input_adapter)
        self.assertIs(advanced.output_adapter, output_adapter)
        with self.assertRaisesRegex(ValueError, "Agent.advanced target is required"):
            Agent.advanced(None)
        with self.assertRaisesRegex(ValueError, "Agent.advanced name"):
            Agent.advanced(target, name=" ")

        import harnest
        import harnest.application

        self.assertFalse(hasattr(harnest, "NativeApp"))
        self.assertFalse(hasattr(harnest.application, "NativeApp"))

    def test_agent_builds_subagents_and_content_config_lazily(self):
        modules = _fake_adk_modules()
        genai = types.ModuleType("google.genai")
        genai.types = types.SimpleNamespace(
            GenerateContentConfig=_recording_class("GenerateContentConfig")
        )
        modules["google.genai"] = genai

        child = Agent(name="child", model="gemini-test", instruction="Help.")
        parent = Agent(
            name="parent",
            model="gemini-test",
            instruction="Delegate.",
            subagents=[child],
            generate_content_config={"temperature": 0.2},
        )
        with patch.dict(sys.modules, modules):
            built = parent.build()

        self.assertEqual(built.kwargs["name"], "parent")
        self.assertEqual(built.kwargs["sub_agents"][0].kwargs["name"], "child")
        self.assertEqual(
            built.kwargs["generate_content_config"].kwargs, {"temperature": 0.2}
        )

    def test_agent_build_has_actionable_error_when_adk_is_unavailable(self):
        definition = Agent(name="helper", model="gemini-test", instruction="Help.")
        with patch.dict(sys.modules, {"google.adk.agents": None}):
            with self.assertRaisesRegex(RuntimeError, "google-adk is required"):
                definition.build()

    def test_agent_preserves_string_and_custom_adk_models(self):
        class CustomModel:
            def build(self):
                raise AssertionError("custom ADK models must not be rebuilt")

        modules = _fake_adk_modules()
        custom_model = CustomModel()
        with patch.dict(sys.modules, modules):
            string_agent = Agent(
                name="string_model",
                model="gemini-test",
                instruction="Help.",
            ).build()
            custom_agent = Agent(
                name="custom_model",
                model=custom_model,
                instruction="Help.",
            ).build()

        self.assertEqual(string_agent.kwargs["model"], "gemini-test")
        self.assertIs(custom_agent.kwargs["model"], custom_model)

    def test_agent_keeps_portable_input_validation_outside_adk(self):
        from pydantic import BaseModel

        class Question(BaseModel):
            topic: str

        class Answer(BaseModel):
            value: str

        modules = _fake_adk_modules()
        with patch.dict(sys.modules, modules):
            built = Agent(
                name="structured",
                model="gemini-test",
                instruction="Answer structurally.",
                input_schema=Question,
                output_schema=Answer,
            ).build()

        # Harnest owns request validation because ADK flattens multipart input
        # before applying its node schema, which would reject portable media.
        self.assertNotIn("input_schema", built.kwargs)
        self.assertIs(built.kwargs["output_schema"], Answer)
        with self.assertRaisesRegex(TypeError, "Pydantic BaseModel class"):
            Agent(
                name="invalid",
                model="gemini-test",
                instruction="Answer.",
                output_schema=dict,  # type: ignore[arg-type]
            )

    def test_agent_serializes_validated_tool_models_before_adk(self):
        from pydantic import BaseModel

        class Count(BaseModel):
            value: int

        @tool
        def count() -> Count:
            """Return a validated count."""

            return Count(value=3)

        with patch.dict(sys.modules, _fake_adk_modules()):
            built = Agent(
                name="structured_tool",
                model="gemini-test",
                instruction="Count.",
                tools=[count],
            ).build()

        runtime_tool = built.kwargs["tools"][0]
        self.assertEqual(runtime_tool(), {"value": 3})
        self.assertEqual(count(), Count(value=3))
        self.assertIs(inspect.signature(runtime_tool).return_annotation, Count)

    def test_agent_keeps_runtime_metadata_out_of_adk_output_schema(self):
        from pydantic import BaseModel

        from harnest.structured import FrameworkMetadata

        class Details(BaseModel):
            framework: str

        class Answer(BaseModel):
            value: str
            metadata: FrameworkMetadata[Details]

        modules = _fake_adk_modules()
        with patch.dict(sys.modules, modules):
            built = Agent(
                name="structured_metadata",
                model="gemini-test",
                instruction="Answer structurally.",
                output_schema=Answer,
            ).build()

        self.assertNotIn("metadata", built.kwargs["output_schema"].model_fields)
        self.assertIn("metadata", Answer.model_fields)

    def test_ollama_model_builds_adk_litellm_lazily(self):
        connector = OllamaModel(
            "qwen3:8b",
            api_base=" http://ollama:11434 ",
            temperature=0.2,
            num_retries=2,
        )
        self.assertEqual(connector.litellm_model, "ollama_chat/qwen3:8b")

        modules = _fake_adk_modules()
        with patch.dict(sys.modules, modules):
            built_model = connector.build()
            built_agent = Agent(
                name="local_agent",
                model=connector,
                instruction="Help.",
            ).build()

        expected = {
            "model": "ollama_chat/qwen3:8b",
            "api_base": "http://ollama:11434",
            "temperature": 0.2,
            "num_retries": 2,
        }
        self.assertEqual(built_model.kwargs, expected)
        self.assertEqual(built_agent.kwargs["model"].kwargs, expected)

    def test_litellm_model_is_provider_neutral_and_forwards_completion_args(self):
        connector = LiteLLMModel(
            " openai/gpt-4.1-mini ",
            api_base="https://models.example.test/v1",
            api_key="secret-token",
            temperature=0.1,
            custom_provider_option={"region": "test"},
        )
        self.assertEqual(connector.model, "openai/gpt-4.1-mini")
        self.assertNotIn("secret-token", repr(connector))

        modules = _fake_adk_modules()
        with patch.dict(sys.modules, modules):
            built_model = connector.build()
            built_agent = Agent(
                name="neutral_agent",
                model=connector,
                instruction="Help.",
            ).build()

        expected = {
            "model": "openai/gpt-4.1-mini",
            "api_base": "https://models.example.test/v1",
            "api_key": "secret-token",
            "temperature": 0.1,
            "custom_provider_option": {"region": "test"},
        }
        self.assertEqual(built_model.kwargs, expected)
        self.assertEqual(built_agent.kwargs["model"].kwargs, expected)

    def test_litellm_model_reads_canonical_openai_model_environment(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_MODEL": "local-compatible-model",
                "OPENAI_API_KEY": "synthetic-openai-key",
                "OPENAI_BASE_URL": "http://models.example.test/v1",
            },
            clear=False,
        ):
            connector = LiteLLMModel.from_openai_environment()

        self.assertEqual(connector.model, "openai/local-compatible-model")
        # The OpenAI-compatible adapter owns key and endpoint lookup, which is
        # also how ADK judge and simulator models receive the same settings.
        self.assertEqual(connector.completion_args, {})
        self.assertNotIn("synthetic-openai-key", repr(connector))

    def test_openai_model_environment_rejects_native_provider_prefix(self):
        with patch.dict(
            os.environ,
            {"OPENAI_MODEL": "ollama_chat/qwen3.5:cloud"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "'openai/' prefix"):
                LiteLLMModel.from_openai_environment()

    def test_openai_model_environment_accepts_qualified_names_and_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                LiteLLMModel.from_openai_environment().model,
                "openai/gpt-4.1-mini",
            )
        with patch.dict(
            os.environ, {"OPENAI_MODEL": "openai/custom/model"}, clear=False
        ):
            self.assertEqual(
                LiteLLMModel.from_openai_environment().model,
                "openai/custom/model",
            )

    def test_litellm_model_supports_thinking_and_non_thinking_modes(self):
        thinking = LiteLLMModel("ollama_chat/qwen3.5:cloud", thinking=True)
        non_thinking = LiteLLMModel(
            "ollama_chat/qwen3.5:cloud", thinking=False
        )
        provider_default = LiteLLMModel("ollama_chat/qwen3.5:cloud")

        self.assertEqual(thinking.completion_args, {"reasoning_effort": "medium"})
        self.assertEqual(
            non_thinking.completion_args, {"reasoning_effort": "none"}
        )
        self.assertEqual(provider_default.completion_args, {})

        modules = _fake_adk_modules()
        with patch.dict(sys.modules, modules):
            self.assertEqual(
                non_thinking.build().kwargs["reasoning_effort"], "none"
            )

        chat_adapter = _recording_class("ChatLiteLLM")
        chat_adapter.model_fields = {
            "model": object(),
            "api_base": object(),
            "model_kwargs": object(),
        }
        langgraph_module = types.ModuleType("langchain_litellm")
        langgraph_module.ChatLiteLLM = chat_adapter
        with patch.dict(sys.modules, {"langchain_litellm": langgraph_module}):
            built = non_thinking.build_langgraph()

        self.assertEqual(
            built.kwargs,
            {
                "model": "ollama_chat/qwen3.5:cloud",
                "model_kwargs": {"reasoning_effort": "none"},
            },
        )
        ollama = OllamaModel(
            "qwen3.5:cloud",
            api_base="https://ollama.example",
            thinking=True,
        )
        with patch.dict(sys.modules, {"langchain_litellm": langgraph_module}):
            built_ollama = ollama.build_langgraph()
        self.assertEqual(built_ollama.kwargs["api_base"], "https://ollama.example")
        self.assertEqual(
            built_ollama.kwargs["model_kwargs"], {"reasoning_effort": "medium"}
        )

    def test_model_thinking_mode_rejects_ambiguous_configuration(self):
        with self.assertRaisesRegex(TypeError, "thinking must be a boolean"):
            LiteLLMModel("ollama_chat/qwen3.5:cloud", thinking="yes")
        with self.assertRaisesRegex(ValueError, "cannot be used together"):
            OllamaModel(
                thinking=True,
                reasoning_effort="high",
            )

    def test_litellm_model_requires_an_explicit_provider(self):
        for invalid in ("", "gpt-4.1-mini", "/gpt-4.1-mini", "openai/"):
            with self.subTest(model=invalid):
                with self.assertRaisesRegex(ValueError, "provider-qualified"):
                    LiteLLMModel(invalid)
        with self.assertRaisesRegex(ValueError, "whitespace"):
            LiteLLMModel("openai/a model")

        connector = LiteLLMModel("anthropic/claude-sonnet-4")
        with patch.dict(sys.modules, {"google.adk.models.lite_llm": None}):
            with self.assertRaisesRegex(RuntimeError, "LiteLLM support"):
                connector.build()

    def test_ollama_model_supports_completion_and_chat_providers(self):
        cloud = OllamaModel(api_key="ollama-cloud-token")
        completion = OllamaModel("qwen3:8b", chat=False)
        qualified = OllamaModel("ollama_chat/qwen3:8b", chat=False)
        nested_name = OllamaModel("hf.co/team/model:latest")

        self.assertEqual(cloud.litellm_model, "ollama_chat/qwen3.5:cloud")
        self.assertEqual(
            cloud.completion_args,
            {"api_key": "ollama-cloud-token"},
        )
        self.assertNotIn("ollama-cloud-token", repr(cloud))
        self.assertEqual(completion.litellm_model, "ollama/qwen3:8b")
        self.assertEqual(qualified.litellm_model, "ollama_chat/qwen3:8b")
        self.assertEqual(
            nested_name.litellm_model,
            "ollama_chat/hf.co/team/model:latest",
        )

    def test_ollama_model_validation_and_missing_dependency_error(self):
        with self.assertRaisesRegex(ValueError, "model name"):
            OllamaModel(" ")
        with self.assertRaisesRegex(ValueError, "api_base"):
            OllamaModel("qwen3", api_base=" ")
        with self.assertRaisesRegex(TypeError, "chat must be a boolean"):
            OllamaModel("qwen3", chat="yes")

        connector = OllamaModel("qwen3")
        with patch.dict(sys.modules, {"google.adk.models.lite_llm": None}):
            with self.assertRaisesRegex(RuntimeError, "LiteLLM support"):
                connector.build()

    def test_mcp_environment_expansion_is_deferred(self):
        client = MCPClient.streamable_http("${TEST_MCP_URL}")
        self.assertEqual(client.url, "${TEST_MCP_URL}")
        self.assertEqual(client.transport, "streamable-http")

    def test_mcp_constructors_build_adk_connections(self):
        modules = _fake_adk_modules()
        with patch.dict(os.environ, {"TEST_MCP_URL": "https://mcp.example/mcp"}):
            with patch.dict(sys.modules, modules):
                remote = MCPClient.streamable_http(
                    "${TEST_MCP_URL}",
                    headers={"Authorization": "Bearer ${TEST_TOKEN}"},
                    tools=["search"],
                    prefix="remote",
                    timeout_seconds=12,
                )
                with patch.dict(os.environ, {"TEST_TOKEN": "secret"}):
                    remote_toolset = remote.to_adk_toolset()
                    sse_toolset = MCPClient.sse(
                        "${TEST_MCP_URL}/sse",
                        headers={"Authorization": "Bearer ${TEST_TOKEN}"},
                        timeout_seconds=7,
                        sse_read_timeout_seconds=420,
                    ).to_adk_toolset()
                with patch.dict(os.environ, {"TEST_TOKEN": "secret"}):
                    stdio_toolset = MCPClient.stdio(
                        "uvx", "server", env={"API_TOKEN": "${TEST_TOKEN}"}
                    ).to_adk_toolset()

        remote_connection = remote_toolset.kwargs["connection_params"]
        self.assertEqual(remote_connection.kwargs["url"], "https://mcp.example/mcp")
        self.assertEqual(
            remote_connection.kwargs["headers"], {"Authorization": "Bearer secret"}
        )
        self.assertEqual(remote_toolset.kwargs["tool_filter"], ["search"])
        self.assertEqual(remote_toolset.kwargs["tool_name_prefix"], "remote")
        sse_connection = sse_toolset.kwargs["connection_params"]
        self.assertEqual(sse_connection.kwargs["url"], "https://mcp.example/mcp/sse")
        self.assertEqual(sse_connection.kwargs["timeout"], 7)
        self.assertEqual(sse_connection.kwargs["sse_read_timeout"], 420)
        stdio_connection = stdio_toolset.kwargs["connection_params"]
        self.assertEqual(stdio_connection.kwargs["timeout"], 30)
        self.assertEqual(
            stdio_connection.kwargs["server_params"].kwargs,
            {"command": "uvx", "args": ["server"], "env": {"API_TOKEN": "secret"}},
        )

    def test_mcp_supports_legacy_adk_connection_exports(self):
        modules = _fake_adk_modules(public_mcp_exports=False)
        with patch.dict(sys.modules, modules):
            toolset = MCPClient.sse("https://mcp.example/sse").to_adk_toolset()
        self.assertEqual(
            toolset.kwargs["connection_params"].kwargs["url"],
            "https://mcp.example/sse",
        )

    def test_mcp_builds_langgraph_adapter_connections(self):
        remote = MCPClient.streamable_http(
            "https://mcp.example/mcp", timeout_seconds=12
        ).to_langgraph_connection()
        sse = MCPClient.sse(
            "https://mcp.example/sse",
            headers={"Authorization": "Bearer test"},
            timeout_seconds=6,
            sse_read_timeout_seconds=480,
        ).to_langgraph_connection()
        stdio = MCPClient.stdio(
            "uvx", "server", timeout_seconds=9
        ).to_langgraph_connection()

        self.assertEqual(remote["transport"], "streamable_http")
        self.assertEqual(remote["timeout"], 12)
        self.assertEqual(remote["sse_read_timeout"], 300)
        self.assertEqual(
            remote["session_kwargs"]["read_timeout_seconds"].total_seconds(),
            12,
        )
        self.assertEqual(sse["transport"], "sse")
        self.assertEqual(sse["url"], "https://mcp.example/sse")
        self.assertEqual(sse["headers"], {"Authorization": "Bearer test"})
        self.assertEqual(sse["timeout"], 6)
        self.assertEqual(sse["sse_read_timeout"], 480)
        self.assertEqual(stdio["transport"], "stdio")
        self.assertEqual(
            stdio["session_kwargs"]["read_timeout_seconds"].total_seconds(),
            9,
        )

    def test_mcp_validation_rejects_common_shape_errors(self):
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            MCPClient.sse("https://mcp.example/sse", timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            MCPClient.sse(
                "https://mcp.example/sse", sse_read_timeout_seconds=0
            )
        with self.assertRaisesRegex(TypeError, "tools must be a sequence"):
            MCPClient.sse("https://mcp.example/sse", tools="search")
        with self.assertRaisesRegex(TypeError, "args must be a sequence"):
            MCPClient("stdio", command="uvx", args="server")

    def test_orchestrator_plan_is_stable_protocol(self):
        orchestrator = Orchestrator(["agents"], parallelism=2, labels={"team": "test"})
        plan = json.loads(orchestrator.to_json(project_root="/tmp/project"))
        self.assertEqual(plan["apiVersion"], "harnest.dev/v1alpha1")
        self.assertEqual(plan["kind"], "DeploymentPlan")
        self.assertEqual(plan["sources"][0]["root"], "agents")
        self.assertEqual(plan["parallelism"], 2)

    def test_orchestrator_rejects_bare_string_sequences(self):
        with self.assertRaisesRegex(TypeError, "sources must be a sequence"):
            Orchestrator("agents")
        with self.assertRaisesRegex(TypeError, "include must be a sequence"):
            AgentSource("agents", include="*.py")

    def test_bundle_agent_requires_and_resolves_instructions_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            definition = Agent(name="root", model="gemini-test")

            with self.assertRaisesRegex(BundleConventionError, "instructions.*missing"):
                bundle_agent(anchor, definition)

            self._write(root / "instructions.md", "  Loaded from disk.\n")
            with patch.dict(sys.modules, _fake_adk_modules()):
                built = bundle_agent(anchor, definition)
                overridden = bundle_agent(
                    anchor,
                    Agent(
                        name="root",
                        model="gemini-test",
                        instruction="Explicit override.",
                    ),
                )

        self.assertEqual(built.kwargs["instruction"], "Loaded from disk.")
        self.assertEqual(overridden.kwargs["instruction"], "Explicit override.")

        with self.assertRaisesRegex(ValueError, "instruction is unresolved"):
            definition.build()

    def test_compile_agent_uses_explicit_compiler_namespace_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from harnest.model import LiteLLMModel\n\n"
                "root_agent = Agent(\n"
                "    name='root',\n"
                "    model=LiteLLMModel('openai/gpt-4.1-mini', temperature=0),\n"
                "    description='Explicitly imported root.',\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Delegate when useful.\n")
            self._write(
                root / "tools" / "lookup.py",
                "from harnest.tool import tool\n\n"
                "@tool\n"
                "def lookup(query: str) -> str:\n"
                "    \"\"\"Look up a query.\"\"\"\n"
                "    return query\n",
            )
            self._write(
                root / "subagents" / "reviewer.py",
                "from harnest.agent import Agent\n\n"
                "reviewer = Agent(\n"
                "    name='reviewer',\n"
                "    model='gemini-test',\n"
                "    instruction='Review the result.',\n"
                ")\n",
            )
            self._write(
                root / "mcp" / "_README.md",
                "Add a client() factory when MCP is required.\n",
            )

            with patch.dict(sys.modules, _fake_adk_modules()):
                built = compile_agent(root)

        self.assertEqual(built.kwargs["name"], "root")
        self.assertEqual(built.kwargs["instruction"], "Delegate when useful.")
        self.assertEqual(built.kwargs["model"].kwargs["model"], "openai/gpt-4.1-mini")
        self.assertEqual(built.kwargs["model"].kwargs["temperature"], 0)
        self.assertEqual(built.kwargs["tools"][0].__name__, "lookup")
        self.assertEqual(built.kwargs["sub_agents"][0].kwargs["name"], "reviewer")

    def test_compile_agent_rejects_implicit_globals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "agent.py",
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")

            with patch.dict(sys.modules, _fake_adk_modules()):
                with self.assertRaisesRegex(
                    BundleImportError,
                    "NameError: name 'Agent' is not defined",
                ):
                    compile_agent(root)

    def test_compile_agent_skips_empty_optional_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            for relative in (
                "tools",
                "subagents",
                "mcp",
                "extensions",
                "plugins",
                "sandbox",
                "skills",
                "evals",
            ):
                (root / relative).mkdir(exist_ok=True)

            with patch.dict(sys.modules, _fake_adk_modules()):
                built = compile_agent(root)
                suite = discover_evals(root / "agent.py")

            self._write(root / "skills" / "_README.md", "Optional skills.\n")
            self._write(root / "evals" / "_README.md", "Optional evals.\n")
            with patch.dict(sys.modules, _fake_adk_modules()):
                ignored_only = compile_agent(root)

            self._write(root / "evals" / "test_config.json", "{}\n")
            with self.assertRaisesRegex(
                BundleConventionError,
                "at least one .*evalset.json",
            ):
                discover_evals(root / "agent.py")

        self.assertEqual(built.kwargs["tools"], [])
        self.assertEqual(built.kwargs["sub_agents"], [])
        self.assertEqual(ignored_only.kwargs["tools"], [])
        self.assertEqual(suite.eval_sets, ())
        self.assertIsNone(suite.config)

    def test_compile_agent_rejects_legacy_mcp_servers_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            (root / "mcp_servers").mkdir()

            with self.assertRaisesRegex(
                BundleConventionError,
                "unsupported legacy MCP directory.*use mcp",
            ):
                compile_agent(root)

    def test_compile_artifact_is_deterministic_and_importable(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            output = workspace / "compiled"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(root / "__pycache__" / "ignored.pyc", "ignored")
            self._write(root / ".adk" / "eval_history" / "run.json", "{}")
            self._write(root / ".env", "TOKEN=local-secret\n")
            environment_bin = root / ".harnest" / "environments" / "test" / "bin"
            environment_bin.mkdir(parents=True)
            os.symlink(root / "agent.py", environment_bin / "python")
            # Deployment references remain authored bytes in both copies; the
            # launcher resolves them only after the artifact reaches its host.
            authored_server = DEFAULT_SERVER_YAML.replace("1MiB", "${MAX_BYTES}")
            self._write(root / "server.yaml", authored_server)

            with patch.dict(sys.modules, _fake_adk_modules()):
                first = compile_artifact(root, output)
                second = compile_artifact(root, output)
                spec = importlib.util.spec_from_file_location(
                    "_compiled_agent_test",
                    output / "agent.py",
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            persisted = json.loads(
                (output / "harnest-manifest.json").read_text(encoding="utf-8")
            )
            launcher_is_executable = bool(
                (output / "harnest-agent").stat().st_mode & 0o111
            )
            compiled_server = (output / "server.yaml").read_text(encoding="utf-8")
            copied_server = (output / "source" / "server.yaml").read_text(
                encoding="utf-8"
            )

        self.assertEqual(first, second)
        self.assertEqual(persisted, first)
        self.assertEqual(first["entrypoint"], "agent:root_agent")
        self.assertEqual(first["sourceEntrypoint"], "agent:root_agent")
        self.assertEqual(first["interfaces"], {"cli": False})
        self.assertTrue(first["digest"].startswith("sha256:"))
        self.assertEqual(module.root_agent.kwargs["name"], "root")
        self.assertFalse(
            any(record["path"].endswith("ignored.pyc") for record in first["files"])
        )
        self.assertFalse(
            any("/.adk/" in f"/{record['path']}/" for record in first["files"])
        )
        self.assertFalse(
            any("/.harnest/" in f"/{record['path']}/" for record in first["files"])
        )
        self.assertFalse(
            any(record["path"].endswith("/.env") for record in first["files"])
        )
        self.assertTrue(launcher_is_executable)
        self.assertEqual(compiled_server, authored_server)
        self.assertEqual(copied_server, authored_server)
        self.assertNotIn("server.yaml", [record["path"] for record in first["files"]])
        self.assertIn(
            "source/server.yaml",
            [record["path"] for record in first["files"]],
        )
        self.assertIn(
            "harnest-agent",
            [record["path"] for record in first["files"]],
        )

    def test_compile_artifact_materializes_inline_server_settings(self):
        """Carry authored overrides into runtime policy and hash the single source."""
        from harnest.server_config import load_server_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            output = Path(directory) / "compiled"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            authored = "server:\n  http:\n    port: ${PORT}\n  playground:\n    enabled: false\n"
            self._write(root / "config.yaml", authored)
            with patch.dict(sys.modules, _fake_adk_modules()):
                manifest = compile_artifact(root, output)
                repeated = compile_artifact(root, output)
            server = load_server_config(output / "server.yaml", environment={"PORT": "9094"})
            self.assertEqual(server.http.port, 9094)
            self.assertEqual(server.http.max_concurrent_requests, 8)
            self.assertFalse(server.playground.enabled)
            self.assertEqual((output / "source/config.yaml").read_text(), authored)
            self.assertFalse((output / "source/server.yaml").exists())
            paths = {record["path"] for record in manifest["files"]}
            self.assertIn("source/config.yaml", paths)
            self.assertNotIn("server.yaml", paths)
            self.assertEqual(manifest, repeated)

    def test_compile_artifact_binds_explicit_cli_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")

            with patch.dict(sys.modules, _fake_adk_modules()):
                disabled = compile_artifact(root, workspace / "disabled")
                enabled = compile_artifact(
                    root,
                    workspace / "enabled",
                    cli_enabled=True,
                )

        self.assertEqual(disabled["interfaces"], {"cli": False})
        self.assertEqual(enabled["interfaces"], {"cli": True})
        self.assertNotEqual(disabled["digest"], enabled["digest"])

    def test_managed_adk_fastapi_hides_native_routes_and_keeps_openapi(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            output = workspace / "compiled"
            self._write(
                root / "agent.py",
                _deterministic_adk_source(advanced=False),
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "agent-card.yaml",
                json.dumps({"name": "Root", "description": "Test agent"}),
            )
            compile_artifact(root, output)
            app = create_fastapi_app(output, bind_host="testserver")

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                playground_response = client.get("/")
                schema_response = client.get("/openapi.json")
                docs_response = client.get("/docs")
                redoc_response = client.get("/redoc")
                agent_response = client.get("/agent")
                native_response = client.post("/run", json={})
                native_health = client.get("/health")

            self.assertEqual(playground_response.status_code, 200)
            self.assertIn("Harnest Playground", playground_response.text)
            self.assertEqual(schema_response.status_code, 200)
            self.assertEqual(docs_response.status_code, 200)
            self.assertEqual(redoc_response.status_code, 200)
            schema_paths = schema_response.json()["paths"]
            self.assertIn("/responses", schema_paths)
            self.assertNotIn("/", schema_paths)
            self.assertFalse(
                {"/health", "/version", "/list-apps", "/run", "/run_sse"}
                & set(schema_paths)
            )
            self.assertFalse(any(path.startswith("/apps/") for path in schema_paths))
            self.assertEqual(agent_response.json()["mode"], "managed")
            self.assertNotIn("adkRun", agent_response.json()["endpoints"])
            self.assertEqual(native_response.status_code, 404)
            self.assertEqual(native_health.status_code, 404)

    def test_advanced_adk_fastapi_reuses_official_route_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            output = workspace / "compiled"
            self._write(
                root / "agent.py",
                _deterministic_adk_source(advanced=True),
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "agent-card.yaml",
                json.dumps({"name": "Root", "description": "Test agent"}),
            )
            compile_artifact(root, output, mode="advanced")
            app = create_fastapi_app(
                output,
                bind_host="testserver",
                max_request_bytes=1024,
            )
            routes = {
                (method, route.path)
                for route in app.routes
                for method in (getattr(route, "methods", None) or {"WEBSOCKET"})
            }
            expected = {
                ("GET", "/"),
                ("GET", "/_harnest/playground.css"),
                ("GET", "/_harnest/playground.js"),
                ("GET", "/docs"),
                ("GET", "/health"),
                ("GET", "/version"),
                ("GET", "/list-apps"),
                ("POST", "/apps/{app_name}/users/{user_id}/sessions"),
                ("GET", "/apps/{app_name}/users/{user_id}/sessions"),
                (
                    "GET",
                    "/apps/{app_name}/users/{user_id}/sessions/{session_id}",
                ),
                (
                    "DELETE",
                    "/apps/{app_name}/users/{user_id}/sessions/{session_id}",
                ),
                (
                    "PATCH",
                    "/apps/{app_name}/users/{user_id}/sessions/{session_id}",
                ),
                ("POST", "/apps/{app_name}/users/{user_id}/sessions/{session_id}/artifacts"),
                ("PATCH", "/apps/{app_name}/users/{user_id}/memory"),
                ("POST", "/run"),
                ("POST", "/run_sse"),
                ("WEBSOCKET", "/run_live"),
                ("GET", "/agent"),
                ("POST", "/sessions"),
                ("GET", "/sessions"),
                ("GET", "/sessions/{session_id}"),
                ("PATCH", "/sessions/{session_id}"),
                ("DELETE", "/sessions/{session_id}"),
                ("POST", "/responses"),
                ("WEBSOCKET", "/live"),
                ("GET", "/healthz"),
                ("GET", "/.well-known/agent-card.json"),
            }
            self.assertTrue(expected <= routes, expected - routes)
            self.assertNotIn(("POST", "/v1/messages"), routes)

            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                playground_response = client.get("/")
                self.assertEqual(playground_response.status_code, 200)
                self.assertIn("Harnest Playground", playground_response.text)
                oversized_native = client.post(
                    "/run",
                    content=b"x" * 1025,
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(oversized_native.status_code, 413)
                response = client.post(
                    "/apps/root/users/test-user/sessions",
                    json={"sessionId": "session-1", "state": {"ready": True}},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["id"], "session-1")
                run_request = {
                    "appName": "root",
                    "userId": "test-user",
                    "sessionId": "session-1",
                    "newMessage": {
                        "role": "user",
                        "parts": [{"text": "hello"}],
                    },
                    "streaming": False,
                }
                run_response = client.post("/run", json=run_request)
                self.assertEqual(run_response.status_code, 200, run_response.text)
                self.assertIn("official response", run_response.text)

                stream_session = client.post(
                    "/apps/root/users/test-user/sessions",
                    json={"sessionId": "session-2", "state": {}},
                )
                self.assertEqual(stream_session.status_code, 200)
                run_request["sessionId"] = "session-2"
                run_request["streaming"] = True
                stream_response = client.post("/run_sse", json=run_request)
                self.assertEqual(
                    stream_response.status_code,
                    200,
                    stream_response.text,
                )
                self.assertIn("data:", stream_response.text)
                self.assertIn("official response", stream_response.text)

                agent_response = client.get("/agent")
                self.assertEqual(agent_response.status_code, 200)
                self.assertEqual(agent_response.json()["id"], "root")
                self.assertEqual(agent_response.json()["mode"], "advanced")
                self.assertEqual(
                    agent_response.json()["endpoints"]["adkRun"], "/run"
                )
                openapi_paths = client.get("/openapi.json").json()["paths"]
                self.assertIn("/run", openapi_paths)
                self.assertIn("/responses", openapi_paths)
                self.assertNotIn("/", openapi_paths)

                neutral_session = client.post(
                    "/sessions",
                    json={"id": "neutral-session", "state": {"ready": True}},
                )
                self.assertEqual(
                    neutral_session.status_code, 201, neutral_session.text
                )
                neutral_record = neutral_session.json()
                self.assertEqual(neutral_record["id"], "neutral-session")
                self.assertEqual(neutral_record["state"], {"ready": True})
                self.assertIsNone(neutral_record["createdAt"])
                self.assertIsNotNone(neutral_record["updatedAt"])
                self.assertIn("adk", neutral_record["metadata"])
                native_session = client.get(
                    "/apps/root/users/_harnest_neutral/sessions/neutral-session"
                )
                self.assertEqual(native_session.status_code, 200)
                self.assertEqual(native_session.json()["state"], {"ready": True})

                neutral_run = client.post(
                    "/responses",
                    json={
                        "input": "hello",
                        "sessionId": "neutral-session",
                        "metadata": {"source": "test"},
                    },
                )
                self.assertEqual(neutral_run.status_code, 200, neutral_run.text)
                neutral_body = neutral_run.json()
                self.assertEqual(neutral_body["status"], "completed")
                self.assertEqual(neutral_body["outputText"], "official response")
                neutral_message = _event_of_type(
                    neutral_body["output"], "message"
                )
                self.assertEqual(
                    neutral_message["content"][0],
                    {"type": "output_text", "text": "official response"},
                )

                neutral_stream_session = client.post(
                    "/sessions", json={"id": "neutral-stream"}
                )
                self.assertEqual(neutral_stream_session.status_code, 201)
                neutral_stream = client.post(
                    "/responses",
                    json={
                        "input": "hello",
                        "sessionId": "neutral-stream",
                        "stream": True,
                    },
                )
                self.assertEqual(neutral_stream.status_code, 200)
                self.assertIn("event: response.created", neutral_stream.text)
                self.assertIn("event: response.agent_activity", neutral_stream.text)
                self.assertIn("event: response.text.delta", neutral_stream.text)
                self.assertIn("event: response.completed", neutral_stream.text)
                self.assertNotIn('"modelVersion"', neutral_stream.text)

                neutral_live_session = client.post(
                    "/sessions", json={"id": "neutral-live"}
                )
                self.assertEqual(neutral_live_session.status_code, 201)
                with client.websocket_connect("/live") as websocket:
                    websocket.send_json(
                        {"type": "connect", "sessionId": "neutral-live"}
                    )
                    self.assertEqual(
                        websocket.receive_json(),
                        {
                            "type": "session.connected",
                            "sessionId": "neutral-live",
                        },
                    )
                    websocket.send_json(
                        {
                            "type": "response.create",
                            "requestId": "request-1",
                            "input": "hello",
                        }
                    )
                    live_events = []
                    while True:
                        live_event = websocket.receive_json()
                        live_events.append(live_event)
                        if live_event["type"] == "response.completed":
                            break
                    self.assertEqual(live_events[0]["type"], "response.created")
                    _event_of_type(live_events, "response.agent_activity")
                    self.assertTrue(
                        any(
                            item["type"] == "response.text.delta"
                            and item["delta"] == "official response"
                            for item in live_events
                        )
                    )
                    self.assertEqual(
                        live_events[-1]["outputText"], "official response"
                    )
                    websocket.send_json({"type": "session.close"})

    def test_advanced_adk_custom_route_invokes_the_wrapped_root(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            output = workspace / "compiled"
            self._write(root / "agent.py", _deterministic_adk_source(advanced=True))
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "agent-card.yaml",
                json.dumps({"name": "Root", "description": "Test agent"}),
            )
            self._write(
                root / "extensions" / "http.py",
                "from fastapi import APIRouter, Request\n"
                "from harnest import lifecycle\n\n"
                "@lifecycle.http_routes\n"
                "def routes(agent):\n"
                "    router = APIRouter()\n"
                "    @router.post('/custom/adk')\n"
                "    async def invoke(request: Request):\n"
                "        response = await agent.invoke(\n"
                "            connection=request, input='hello'\n"
                "        )\n"
                "        return response.as_dict()\n"
                "    return router\n",
            )
            compile_artifact(root, output, mode="advanced")

            from fastapi.testclient import TestClient

            with TestClient(
                create_fastapi_app(output, bind_host="testserver")
            ) as client:
                response = client.post("/custom/adk")
                schema = client.get("/openapi.json")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outputText"], "official response")
            self.assertEqual(schema.status_code, 200, schema.text)
            self.assertIn("/custom/adk", schema.json()["paths"])

    def test_advanced_native_and_neutral_transports_use_discovered_auth_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            output = workspace / "compiled"
            self._write(root / "agent.py", _deterministic_adk_source(advanced=True))
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "agent-card.yaml",
                json.dumps({"name": "Root", "description": "Test agent"}),
            )
            self._write(
                root / "extensions" / "gateway.py",
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.runtime_auth import AuthPrincipal, AuthenticationError\n"
                "@lifecycle.authenticate\n"
                "def authenticate(connection, principal):\n"
                "    user = connection.headers.get('x-user')\n"
                "    if not user: raise AuthenticationError()\n"
                "    return AuthPrincipal(user)\n",
            )
            compile_artifact(root, output, mode="advanced")
            app = create_fastapi_app(output, bind_host="testserver")

            from fastapi.testclient import TestClient
            from starlette.websockets import WebSocketDisconnect

            with TestClient(app) as client:
                self.assertEqual(client.post("/run", json={}).status_code, 401)
                self.assertEqual(
                    client.post(
                        "/responses", json={"input": "hello", "stream": True}
                    ).status_code,
                    401,
                )
                with self.assertRaises(WebSocketDisconnect) as rejected:
                    with client.websocket_connect("/live"):
                        pass
                self.assertEqual(rejected.exception.code, 4401)
                authorized = client.post(
                    "/sessions", headers={"x-user": "alice"}, json={"id": "one"}
                )
                self.assertEqual(authorized.status_code, 201)

    def test_compile_cli_emits_manifest_for_explicit_authoring_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "authored"
            output = workspace / "compiled"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            stdout = StringIO()
            with patch.dict(sys.modules, _fake_adk_modules()):
                with redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "compile",
                            str(root),
                            "--output",
                            str(output),
                            "--enable-cli",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        manifest = json.loads(stdout.getvalue())
        self.assertEqual(manifest["name"], "root")
        self.assertEqual(manifest["interfaces"], {"cli": True})

    def test_test_cli_delegates_eval_result_output_and_quiet_mode(self):
        with patch("harnest.cli.run_agent_tests", return_value=0) as runner:
            default_status = cli_main(["test", ".", "--evals"])
            quiet_status = cli_main(
                [
                    "test",
                    ".",
                    "--evals",
                    "--no-output",
                    "--eval-output",
                    "results/eval.json",
                ]
            )

        self.assertEqual((default_status, quiet_status), (0, 0))
        self.assertFalse(runner.call_args_list[0].kwargs["no_output"])
        self.assertIsNone(runner.call_args_list[0].kwargs["eval_output"])
        self.assertTrue(runner.call_args_list[1].kwargs["no_output"])
        self.assertEqual(
            runner.call_args_list[1].kwargs["eval_output"],
            Path("results/eval.json"),
        )

    def test_authored_unit_test_runner_injects_agent_and_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "extensions" / "output.py",
                "from harnest.lifecycle import lifecycle\n"
                "from harnest.output import OutputPolicy\n\n"
                "@lifecycle.output_policy\n"
                "def output_policy():\n"
                "    return OutputPolicy(subagent_messages='include')\n",
            )
            self._write(
                root / "tools" / "double.py",
                "from harnest.tool import tool\n\n"
                "@tool\n"
                "def double(value: int) -> int:\n"
                "    \"\"\"Double a number.\"\"\"\n"
                "    return value * 2\n",
            )
            self._write(
                root / "tests" / "unit" / "test_double.py",
                "def test_injected_fixtures(agent, tools):\n"
                "    assert agent.name == 'root'\n"
                "    assert tools['double'](4) == 8\n"
                "    assert type(tools).__name__ == 'mappingproxy'\n",
            )

            exit_code = run_agent_tests(root)

        self.assertEqual(exit_code, 0)

    def test_authored_test_runner_requires_convention_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")

            with self.assertRaisesRegex(AgentTestError, "tests/unit"):
                run_agent_tests(root)

    def test_authored_test_runner_accepts_placeholder_only_test_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(root / "tests" / "unit" / "_README.md", "Add tests.\n")
            self._write(root / "tests" / "smoke" / "_README.md", "Add smoke.\n")
            output = StringIO()

            with redirect_stdout(output):
                exit_code = run_agent_tests(root)

        self.assertEqual(exit_code, 0)
        self.assertIn("no authored Python tests", output.getvalue())

    def test_authored_test_runner_runs_validated_evals_after_unit_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "tests" / "unit" / "test_agent.py",
                "def test_agent_compiles(agent):\n"
                "    assert agent.name == 'root'\n",
            )
            self._write(
                root / "evals" / "quality.evalset.json",
                json.dumps({"eval_set_id": "quality", "eval_cases": []}),
            )
            self._write(
                root / "evals" / "test_config.json",
                json.dumps({"criteria": {}}),
            )

            with patch(
                "harnest.testing._run_adk_evals", return_value=0
            ) as eval_runner:
                exit_code = run_agent_tests(root, include_evals=True)

        self.assertEqual(exit_code, 0)
        eval_runner.assert_called_once()
        suite = eval_runner.call_args.args[1]
        self.assertEqual(
            [path.name for path in suite.eval_sets],
            ["quality.evalset.json"],
        )
        self.assertEqual(suite.config.name, "test_config.json")
        self.assertEqual(
            eval_runner.call_args.kwargs,
            {
                "trajectory": "business",
                "print_results": True,
                "result_output": None,
            },
        )

    def test_langgraph_test_runner_uses_shared_eval_suite_after_unit_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            self._write(
                root / "agent.py",
                "from harnest.graph import START, Edge, Event, Graph\n\n"
                "def respond(value):\n"
                "    return Event(message='ok')\n\n"
                "root_agent = Graph(\n"
                "    name='root', nodes={'respond': respond},\n"
                "    edges=(Edge(START, 'respond'),),\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(root / "tests" / "unit" / "_README.md", "Optional.\n")
            self._write(
                root / "evals" / "quality.evalset.json",
                json.dumps({"eval_set_id": "quality", "eval_cases": []}),
            )

            with patch(
                "harnest.testing._run_langgraph_evals", return_value=0
            ) as eval_runner:
                exit_code = run_agent_tests(
                    root, include_evals=True, framework="langgraph"
                )

        self.assertEqual(exit_code, 0)
        eval_runner.assert_called_once()
        self.assertEqual(eval_runner.call_args.args[0].framework, "langgraph")
        self.assertEqual(
            [path.name for path in eval_runner.call_args.args[1].eval_sets],
            ["quality.evalset.json"],
        )
        self.assertEqual(
            eval_runner.call_args.kwargs,
            {
                "trajectory": "business",
                "print_results": True,
                "result_output": None,
            },
        )

    def test_authored_test_runner_can_suppress_all_test_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            result_output = Path(directory) / "results" / "eval.json"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n\n"
                "root_agent = Agent(name='root', model='gemini-test')\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "tests" / "unit" / "test_no_output.py",
                "def test_agent_compiles(agent):\n"
                "    assert agent.name == 'root'\n",
            )
            self._write(
                root / "evals" / "quality.evalset.json",
                json.dumps({"eval_set_id": "quality", "eval_cases": []}),
            )

            with patch(
                "harnest.testing._run_adk_evals", return_value=0
            ) as eval_runner:
                output = StringIO()
                errors = StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    exit_code = run_agent_tests(
                        root,
                        include_evals=True,
                        no_output=True,
                        eval_output=result_output,
                    )

        self.assertEqual(exit_code, 0)
        self.assertFalse(eval_runner.call_args.kwargs["print_results"])
        self.assertEqual(
            eval_runner.call_args.kwargs["result_output"], result_output
        )
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_authored_test_runner_rejects_eval_output_without_evals(self):
        with self.assertRaisesRegex(
            AgentTestError, "--eval-output requires --evals"
        ):
            run_agent_tests(".", eval_output=Path("eval-result.json"))

    def test_named_eval_trajectories_override_only_tool_matching_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "test_config.json"
            self._write(
                config,
                json.dumps(
                    {
                        "criteria": {
                            "tool_trajectory_avg_score": {
                                "threshold": 0.75,
                                "matchType": "EXACT",
                            },
                            "response_match_score": 0.6,
                        }
                    }
                ),
            )
            suite = EvalSuite((), config)

            business = _eval_config(suite, "business")
            strict = _eval_config(suite, "strict")

        business_criterion = business.criteria["tool_trajectory_avg_score"]
        strict_criterion = strict.criteria["tool_trajectory_avg_score"]
        self.assertEqual(business_criterion.match_type.name, "IN_ORDER")
        self.assertEqual(strict_criterion.match_type.name, "EXACT")
        self.assertEqual(business_criterion.threshold, 0.75)
        self.assertEqual(business.criteria["response_match_score"], 0.6)

    def test_eval_config_preserves_every_adk_metric_and_custom_metric(self):
        from google.adk.evaluation.eval_metrics import PrebuiltMetrics

        # The test follows ADK's installed registry rather than creating a
        # Harnest allowlist that would fall behind a compatible ADK release.
        metric_names = {metric.value for metric in PrebuiltMetrics}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "test_config.json"
            self._write(
                config,
                json.dumps(
                    {
                        "criteria": {
                            **{name: 0.5 for name in metric_names},
                            "custom_quality": 0.75,
                        },
                        "customMetrics": {
                            "custom_quality": {
                                "codeConfig": {
                                    "name": "harnest.lib.metrics.custom_quality"
                                }
                            }
                        },
                    }
                ),
            )

            loaded = _eval_config(EvalSuite((), config), "business")

        self.assertEqual(set(loaded.criteria), metric_names | {"custom_quality"})
        self.assertEqual(
            loaded.custom_metrics["custom_quality"].code_config.name,
            "harnest.lib.metrics.custom_quality",
        )

    def test_eval_config_uses_canonical_model_for_implicit_judges_and_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "test_config.json"
            self._write(
                config,
                json.dumps(
                    {
                        "criteria": {
                            "final_response_match_v2": {
                                "threshold": 0.8,
                                "judgeModelOptions": {"numSamples": 1},
                            }
                        },
                        "userSimulatorConfig": {
                            "type": "llm_backed",
                            "maxAllowedInvocations": 2,
                        },
                    }
                ),
            )
            with patch.dict(
                os.environ, {"OPENAI_MODEL": "local-judge"}, clear=False
            ):
                loaded = _eval_config(EvalSuite((), config), "business")

        criterion = loaded.criteria["final_response_match_v2"].model_dump(
            by_alias=True
        )
        self.assertEqual(
            criterion["judgeModelOptions"]["judgeModel"],
            "openai/local-judge",
        )
        self.assertEqual(loaded.user_simulator_config.model, "openai/local-judge")

    def test_eval_config_preserves_explicit_non_openai_model_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "test_config.json"
            self._write(
                config,
                json.dumps(
                    {
                        "criteria": {
                            "final_response_match_v2": {
                                "threshold": 0.8,
                                "judgeModelOptions": {
                                    "judgeModel": "gemini-2.5-flash"
                                },
                            }
                        },
                        "userSimulatorConfig": {
                            "type": "llm_backed",
                            "model": "gemini-2.5-flash",
                        },
                    }
                ),
            )
            with patch.dict(
                os.environ, {"OPENAI_MODEL": "ollama_chat/unused"}, clear=False
            ):
                loaded = _eval_config(EvalSuite((), config), "business")

        criterion = loaded.criteria["final_response_match_v2"].model_dump(
            by_alias=True
        )
        self.assertEqual(
            criterion["judgeModelOptions"]["judgeModel"],
            "gemini-2.5-flash",
        )
        self.assertEqual(loaded.user_simulator_config.model, "gemini-2.5-flash")

    def test_eval_config_defaults_an_omitted_scenario_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            eval_set = Path(directory) / "conversation.evalset.json"
            self._write(
                eval_set,
                json.dumps(
                    {
                        "evalCases": [
                            {"conversationScenario": {"startingPrompt": "Hello"}}
                        ]
                    }
                ),
            )
            with patch.dict(
                os.environ, {"OPENAI_MODEL": "shared-simulator"}, clear=False
            ):
                loaded = _eval_config(EvalSuite((eval_set,)), "business")

        self.assertEqual(
            loaded.user_simulator_config.model, "openai/shared-simulator"
        )

    def test_authored_test_runner_rejects_unknown_eval_trajectory(self):
        with self.assertRaisesRegex(AgentTestError, "business or strict"):
            run_agent_tests(".", eval_trajectory="approximate")

    def test_eval_result_projection_retains_rubric_session_and_invocations(self):
        from google.adk.evaluation.eval_case import Invocation
        from google.adk.evaluation.eval_metrics import (
            EvalMetricResult,
            EvalMetricResultDetails,
            EvalMetricResultPerInvocation,
            EvalStatus,
        )
        from google.adk.evaluation.eval_result import EvalCaseResult
        from google.adk.evaluation.eval_rubrics import RubricScore
        from google.adk.sessions import Session
        from google.genai import types as genai_types

        actual = Invocation(
            invocation_id="actual-1",
            user_content=genai_types.Content(
                role="user", parts=[genai_types.Part(text="Question")]
            ),
            final_response=genai_types.Content(
                role="model", parts=[genai_types.Part(text="Answer")]
            ),
        )
        expected = Invocation(
            invocation_id="expected-1",
            user_content=actual.user_content,
            final_response=actual.final_response,
        )
        metric = EvalMetricResult(
            metric_name="rubric_based_final_response_quality_v1",
            threshold=1.0,
            score=1.0,
            eval_status=EvalStatus.PASSED,
            details=EvalMetricResultDetails(
                rubric_scores=[
                    RubricScore(
                        rubric_id="grounded-answer",
                        score=1.0,
                        rationale="The response cites the supplied evidence.",
                    )
                ]
            ),
        )
        result = EvalCaseResult(
            eval_set_id="quality",
            eval_id="grounded",
            final_eval_status=EvalStatus.PASSED,
            overall_eval_metric_results=[metric],
            eval_metric_result_per_invocation=[
                EvalMetricResultPerInvocation(
                    actual_invocation=actual,
                    expected_invocation=expected,
                    eval_metric_results=[metric],
                )
            ],
            session_id="session-1",
            session_details=Session(
                id="session-1",
                app_name="root",
                user_id="eval-user",
                state={"trace": "retained"},
            ),
            user_id="eval-user",
        )

        payload = _eval_case_result_payload(result)

        # These nested fields are the diagnostic contract that CSV summaries
        # cannot preserve, so assert their values rather than mere presence.
        self.assertEqual(payload["finalEvalStatus"], "passed")
        self.assertEqual(
            payload["overallEvalMetricResults"][0]["details"]["rubricScores"][0],
            {
                "rationale": "The response cites the supplied evidence.",
                "rubricId": "grounded-answer",
                "score": 1.0,
            },
        )
        self.assertEqual(
            payload["evalMetricResultPerInvocation"][0]["actualInvocation"][
                "invocationId"
            ],
            "actual-1",
        )
        self.assertEqual(
            payload["evalMetricResultPerInvocation"][0]["expectedInvocation"][
                "invocationId"
            ],
            "expected-1",
        )
        self.assertIn("evalSetFile", payload)
        self.assertIn("evalMetricResults", payload)
        self.assertEqual(payload["sessionDetails"]["state"]["trace"], "retained")
        self.assertEqual(payload["sessionId"], "session-1")
        self.assertEqual(payload["userId"], "eval-user")

    def test_eval_set_runner_collects_later_suites_after_scored_failure(self):
        calls = []

        class EvalSet:
            @classmethod
            def model_validate_json(cls, payload):
                """Expose only the suite identity needed by the evaluator fake."""

                del cls
                return types.SimpleNamespace(
                    eval_set_id=json.loads(payload)["eval_set_id"]
                )

        class Evaluator:
            @classmethod
            async def evaluate_eval_set(cls, *, eval_set, **kwargs):
                """Record every suite while making the first quality gate fail."""

                del cls, kwargs
                calls.append(eval_set.eval_set_id)
                # A scored failure is recoverable at the suite boundary, so a
                # later independent suite must still contribute to the result.
                if eval_set.eval_set_id == "first":
                    raise AssertionError("first suite failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.evalset.json"
            second = root / "second.evalset.json"
            self._write(first, json.dumps({"eval_set_id": "first"}))
            self._write(second, json.dumps({"eval_set_id": "second"}))
            suite = EvalSuite((first, second), root / "test_config.json")

            with self.assertRaisesRegex(AssertionError, "first suite failed"):
                asyncio.run(
                    _evaluate_eval_sets(
                        Evaluator,
                        EvalSet,
                        module_name="compiled.agent",
                        suite=suite,
                        config=object(),
                        trajectory="business",
                        print_results=False,
                    )
                )

        self.assertEqual(calls, ["first", "second"])

    def test_eval_set_runner_retains_empty_authored_suite(self):
        class EvalSet:
            @classmethod
            def model_validate_json(cls, payload):
                """Expose the empty authored suite identity to the runner."""

                del cls
                return types.SimpleNamespace(
                    eval_set_id=json.loads(payload)["eval_set_id"]
                )

        class Evaluator:
            @classmethod
            async def evaluate_eval_set(cls, **kwargs):
                """Mirror ADK's lack of a manager callback for an empty suite."""

                del cls, kwargs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            eval_set = root / "empty.evalset.json"
            self._write(eval_set, json.dumps({"eval_set_id": "empty"}))
            suite = EvalSuite((eval_set,), root / "test_config.json")
            collector = _EvalResultCollector(
                framework="adk", trajectory="business"
            )

            with redirect_stdout(StringIO()):
                asyncio.run(
                    _evaluate_eval_sets(
                        Evaluator,
                        EvalSet,
                        module_name="compiled.agent",
                        suite=suite,
                        config=object(),
                        trajectory="business",
                        print_results=False,
                        result_collector=collector,
                    )
                )
            result = collector.payload(status="passed")

        self.assertEqual(result["status"], "not_evaluated")
        self.assertEqual(result["summary"]["suiteCount"], 1)
        self.assertEqual(result["summary"]["caseCount"], 0)
        self.assertEqual(len(result["evalSetResults"]), 1)
        self.assertEqual(result["evalSetResults"][0]["evalSetId"], "empty")
        self.assertEqual(
            result["evalSetResults"][0]["status"], "not_evaluated"
        )
        self.assertEqual(result["evalSetResults"][0]["evalCaseResults"], [])

    def test_adk_eval_error_result_captures_infrastructure_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "eval-result.json"
            suite = EvalSuite((), root / "test_config.json")
            with patch(
                "harnest.testing._eval_dependencies",
                return_value=(object(), object()),
            ), patch(
                "harnest.testing._eval_config", return_value=object()
            ), patch(
                "harnest.testing._adk_eval_output_filter"
            ), patch(
                "harnest.testing._evaluate_eval_sets",
                side_effect=RuntimeError(
                    "provider unavailable with synthetic-api-key"
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                    _run_adk_evals(
                        root,
                        suite,
                        print_results=False,
                        result_output=output,
                    )
            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            {
                "message": "evaluation infrastructure failed",
                "type": "RuntimeError",
            },
        )
        self.assertNotIn("synthetic-api-key", json.dumps(result))

    def test_adk_scored_failure_artifact_retains_case_metric_details(self):
        case_payload = {
            "evalSetId": "quality",
            "evalId": "failed-case",
            "finalEvalStatus": "failed",
            "overallEvalMetricResults": [
                {
                    "metricName": "quality_metric",
                    "evalStatus": "failed",
                    "details": {
                        "rubricScores": [
                            {
                                "rubricId": "grounding",
                                "score": 0.0,
                                "rationale": "The answer lacks evidence.",
                            }
                        ]
                    },
                }
            ],
            "evalMetricResultPerInvocation": [],
            "sessionId": "session-1",
            "sessionDetails": None,
            "userId": "eval-user",
        }

        async def evaluate_with_scored_failure(*args, result_collector, **kwargs):
            """Persist ADK's scored case before raising its quality-gate error."""

            del args, kwargs
            result_collector.save_eval_set_result(
                "harnest_eval",
                "quality",
                [types.SimpleNamespace(eval_id="failed-case")],
            )
            raise AssertionError("quality metric failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "eval-result.json"
            suite = EvalSuite((), root / "test_config.json")
            with patch(
                "harnest.testing._eval_dependencies",
                return_value=(object(), object()),
            ), patch(
                "harnest.testing._eval_config", return_value=object()
            ), patch(
                "harnest.testing._adk_eval_output_filter"
            ), patch(
                "harnest.testing._eval_case_result_payload",
                return_value=case_payload,
            ), patch(
                "harnest.testing._evaluate_eval_sets",
                side_effect=evaluate_with_scored_failure,
            ):
                with redirect_stderr(StringIO()):
                    status = _run_adk_evals(
                        root,
                        suite,
                        print_results=False,
                        result_output=output,
                    )
            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "failed")
        suite_result = result["evalSetResults"][0]
        self.assertEqual(suite_result["status"], "failed")
        failed_case = suite_result["evalCaseResults"][0]
        self.assertEqual(failed_case["evalId"], "failed-case")
        rubric = failed_case["overallEvalMetricResults"][0]["details"][
            "rubricScores"
        ][0]
        self.assertEqual(rubric["rationale"], "The answer lacks evidence.")

    def test_langgraph_eval_publishes_structured_result(self):
        application = types.SimpleNamespace(target=None)

        @asynccontextmanager
        async def eval_module(received_application):
            """Supply a stable adapter module without importing LangGraph."""

            self.assertIs(received_application, application)
            yield "compiled.langgraph_eval"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "langgraph-result.json"
            suite = EvalSuite((), root / "test_config.json")
            config = types.SimpleNamespace(live_model_config=None)
            with patch(
                "harnest.testing._eval_dependencies",
                return_value=(object(), object()),
            ), patch(
                "harnest.testing._eval_config", return_value=config
            ), patch(
                "harnest.eval_langgraph.langgraph_eval_agent_module", eval_module
            ), patch("harnest.testing._evaluate_eval_sets"):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    status = _run_langgraph_evals(
                        application, suite, result_output=output
                    )
            result = json.loads(output.read_text(encoding="utf-8"))
            printed = json.loads(stdout.getvalue())

        self.assertEqual(status, 0)
        self.assertEqual(result, printed)
        self.assertEqual(result["framework"], "langgraph")
        self.assertEqual(result["status"], "not_evaluated")
        self.assertEqual(result["summary"]["suiteCount"], 0)

    def test_langgraph_eval_rejects_adk_live_model_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "test_config.json"
            self._write(config, json.dumps({"liveModelConfig": {}}))

            with self.assertRaisesRegex(AgentTestError, "liveModelConfig"):
                _run_langgraph_evals(object(), EvalSuite((), config))

    def test_adk_eval_filter_scores_only_customer_facing_parts(self):
        """Filter an isolated native App without mutating its authored plugins."""
        from google.adk.agents import LlmAgent
        from google.adk.apps import App
        from google.adk.plugins import BasePlugin

        authored_plugin = BasePlugin(name="authored")
        app = App(name="compiled", root_agent=LlmAgent(name="root", model="gemini-test"),
                  plugins=[authored_plugin])
        package = types.ModuleType("compiled")
        package.__path__ = []
        module = types.ModuleType("compiled.agent")
        module.app = app
        event = types.SimpleNamespace(
            content=types.SimpleNamespace(
                parts=[
                    types.SimpleNamespace(text="hidden reasoning", thought=True),
                    types.SimpleNamespace(text="visible answer", thought=False),
                    types.SimpleNamespace(text=None, thought=False, function_call=object()),
                ]
            )
        )

        # Install only the requested module instead of replacing the process-wide
        # importlib function used by lazily imported ADK dependencies.
        with patch.dict(
            sys.modules,
            {"compiled": package, "compiled.agent": module},
        ), _adk_eval_output_filter("compiled.agent"):
            self.assertIsNot(module.app, app)
            eval_plugin = module.app.plugins[0]
            self.assertIn(authored_plugin.name, [item.name for item in module.app.plugins])
            self.assertEqual(app.plugins, [authored_plugin])
            asyncio.run(
                eval_plugin.on_event_callback(
                    invocation_context=object(), event=event
                )
            )
            evaluated_text = "\n".join(
                part.text for part in event.content.parts if part.text
            )

        self.assertEqual(evaluated_text, "visible answer")
        self.assertEqual(len(event.content.parts), 2)
        self.assertEqual(app.plugins, [authored_plugin])

    def test_official_adk_eval_scores_visible_answer_without_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            artifact = Path(directory) / "compiled"
            result_output = Path(directory) / "results" / "eval-result.json"
            result_output.parent.mkdir(parents=True)
            result_output.write_text("stale result\n", encoding="utf-8")
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from google.adk.models import BaseLlm, LlmResponse\n"
                "from google.genai import types\n\n"
                "class ThoughtfulLlm(BaseLlm):\n"
                "    async def generate_content_async(self, llm_request, stream=False):\n"
                "        yield LlmResponse(content=types.Content(role='model', parts=[\n"
                "            types.Part(text='hidden reasoning', thought=True),\n"
                "            types.Part(text='visible answer', thought=False),\n"
                "        ]))\n\n"
                "root_agent = Agent(name='root', model=ThoughtfulLlm(model='test'))\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(
                root / "evals" / "visible.evalset.json",
                json.dumps(
                    {
                        "eval_set_id": "visible",
                        "eval_cases": [
                            {
                                "evalId": "visible-only",
                                "conversation": [
                                    {
                                        "userContent": {
                                            "role": "user",
                                            "parts": [{"text": "answer"}],
                                        },
                                        "finalResponse": {
                                            "role": "model",
                                            "parts": [{"text": "visible answer"}],
                                        },
                                    }
                                ],
                                "sessionInput": {
                                    "appName": "root",
                                    "userId": "eval-user",
                                    "state": {},
                                },
                            }
                        ],
                    }
                ),
            )
            self._write(
                root / "evals" / "test_config.json",
                json.dumps({"criteria": {"response_match_score": 1.0}}),
            )
            compile_artifact(root, artifact)
            suite = discover_evals(artifact / "source" / "agent.py")

            output = StringIO()
            with redirect_stdout(output):
                status = _run_adk_evals(
                    artifact, suite, result_output=result_output
                )
            result = json.loads(result_output.read_text(encoding="utf-8"))
            temporary_results = list(
                result_output.parent.glob(f".{result_output.name}.*.tmp")
            )

            # The eval dependency already provides JSON Schema validation; use
            # it here without adding a runtime dependency to Harnest itself.
            from jsonschema import Draft202012Validator, FormatChecker

            schema_path = (
                Path(__file__).parents[2]
                / "schemas"
                / "eval-run-result.schema.json"
            )
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).validate(result)

        self.assertEqual(status, 0)
        self.assertEqual(temporary_results, [])
        self.assertIn('"kind": "EvalRunResult"', output.getvalue())
        self.assertEqual(result["apiVersion"], "harnest.dev/v1alpha1")
        self.assertEqual(result["kind"], "EvalRunResult")
        self.assertEqual(result["framework"], "adk")
        self.assertEqual(result["trajectory"], "business")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["summary"],
            {
                "caseCount": 1,
                "failedCases": 0,
                "notEvaluatedCases": 0,
                "passedCases": 1,
                "suiteCount": 1,
            },
        )
        case = result["evalSetResults"][0]["evalCaseResults"][0]
        self.assertTrue(case["sessionId"].startswith("___eval___session___"))
        self.assertEqual(case["userId"], "eval-user")
        self.assertIn("sessionDetails", case)
        invocation = case["evalMetricResultPerInvocation"][0]
        actual = invocation["actualInvocation"]
        expected = invocation["expectedInvocation"]
        self.assertEqual(
            actual["finalResponse"]["parts"][0]["text"], "visible answer"
        )
        self.assertEqual(
            expected["finalResponse"]["parts"][0]["text"], "visible answer"
        )
        metric = case["overallEvalMetricResults"][0]
        self.assertEqual(metric["metricName"], "response_match_score")
        self.assertEqual(metric["evalStatus"], "passed")
        self.assertIn("rubricScores", metric["details"])

    def test_official_adk_eval_preserves_multi_turn_history_and_session_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            artifact = Path(directory) / "compiled"
            result_output = Path(directory) / "multi-turn-result.json"
            self._write(
                root / "agent.py",
                "from harnest.agent import Agent\n"
                "from google.adk.models import BaseLlm, LlmResponse\n"
                "from google.genai import types\n\n"
                "class HistoryRequiredLlm(BaseLlm):\n"
                "    \"\"\"Answer the follow-up only from prior user history.\"\"\"\n\n"
                "    async def generate_content_async(self, llm_request, stream=False):\n"
                "        \"\"\"Require the first user turn to resolve the second.\"\"\"\n"
                "        user_turns = [\n"
                "            part.text\n"
                "            for content in llm_request.contents\n"
                "            if content.role == 'user'\n"
                "            for part in (content.parts or [])\n"
                "            if part.text\n"
                "        ]\n"
                "        current = user_turns[-1]\n"
                "        if current.startswith('Remember:'):\n"
                "            answer = 'I will remember the destination.'\n"
                "        else:\n"
                "            # The follow-up omits the city, so only session history\n"
                "            # can supply the entity needed for the correct answer.\n"
                "            remembered = any(\n"
                "                'destination is Paris' in turn\n"
                "                for turn in user_turns[:-1]\n"
                "            )\n"
                "            answer = (\n"
                "                'Your destination is Paris.'\n"
                "                if remembered\n"
                "                else 'I no longer have the destination.'\n"
                "            )\n"
                "        yield LlmResponse(content=types.Content(\n"
                "            role='model', parts=[types.Part(text=answer)]\n"
                "        ))\n\n"
                "root_agent = Agent(\n"
                "    name='root', model=HistoryRequiredLlm(model='history-test')\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Remember conversation context.\n")
            self._write(
                root / "evals" / "history.evalset.json",
                json.dumps(
                    {
                        "eval_set_id": "history",
                        "eval_cases": [
                            {
                                "evalId": "destination-memory",
                                "conversation": [
                                    {
                                        "userContent": {
                                            "role": "user",
                                            "parts": [
                                                {
                                                    "text": (
                                                        "Remember: my destination "
                                                        "is Paris."
                                                    )
                                                }
                                            ],
                                        },
                                        "finalResponse": {
                                            "role": "model",
                                            "parts": [
                                                {
                                                    "text": (
                                                        "I will remember the "
                                                        "destination."
                                                    )
                                                }
                                            ],
                                        },
                                    },
                                    {
                                        "userContent": {
                                            "role": "user",
                                            "parts": [
                                                {
                                                    "text": (
                                                        "Which destination did "
                                                        "I name?"
                                                    )
                                                }
                                            ],
                                        },
                                        "finalResponse": {
                                            "role": "model",
                                            "parts": [
                                                {
                                                    "text": (
                                                        "Your destination is Paris."
                                                    )
                                                }
                                            ],
                                        },
                                    },
                                ],
                                # ADK retrieves result session details through
                                # the evaluator app owner, so the authored input
                                # uses that same owner for this diagnostic test.
                                "sessionInput": {
                                    "appName": "harnest_eval",
                                    "userId": "history-user",
                                    "state": {"testMarker": "multi-turn"},
                                },
                            }
                        ],
                    }
                ),
            )
            self._write(
                root / "evals" / "test_config.json",
                json.dumps({"criteria": {"response_match_score": 1.0}}),
            )
            compile_artifact(root, artifact)
            suite = discover_evals(artifact / "source" / "agent.py")

            status = _run_adk_evals(
                artifact,
                suite,
                print_results=False,
                result_output=result_output,
            )
            result = json.loads(result_output.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(result["status"], "passed")
        case = result["evalSetResults"][0]["evalCaseResults"][0]
        self.assertEqual(case["finalEvalStatus"], "passed")
        invocations = case["evalMetricResultPerInvocation"]
        self.assertEqual(len(invocations), 2)
        actual_responses = [
            invocation["actualInvocation"]["finalResponse"]["parts"][0]["text"]
            for invocation in invocations
        ]
        self.assertEqual(
            actual_responses,
            [
                "I will remember the destination.",
                "Your destination is Paris.",
            ],
        )
        follow_up = invocations[1]["actualInvocation"]["userContent"]["parts"][
            0
        ]["text"]
        self.assertNotIn("Paris", follow_up)
        self.assertTrue(
            all(
                invocation["evalMetricResults"][0]["evalStatus"] == "passed"
                for invocation in invocations
            )
        )
        session = case["sessionDetails"]
        self.assertEqual(session["id"], case["sessionId"])
        self.assertEqual(session["appName"], "harnest_eval")
        self.assertEqual(session["userId"], "history-user")
        self.assertEqual(session["state"]["testMarker"], "multi-turn")
        event_texts = [
            part["text"]
            for event in session["events"]
            if event.get("content")
            for part in event["content"].get("parts", [])
            if part.get("text")
        ]
        self.assertIn("Remember: my destination is Paris.", event_texts)
        self.assertIn("Which destination did I name?", event_texts)
        self.assertIn("Your destination is Paris.", event_texts)

    def test_langgraph_eval_executes_authored_custom_metric(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authored"
            self._write(
                root / "agent.py",
                "from harnest.graph import START, Edge, Event, Graph\n\n"
                "def respond(value):\n"
                "    return Event(message='official response')\n\n"
                "root_agent = Graph(\n"
                "    name='root', nodes={'respond': respond},\n"
                "    edges=(Edge(START, 'respond'),),\n"
                ")\n",
            )
            self._write(root / "instructions.md", "Answer clearly.\n")
            self._write(root / "tests" / "unit" / "_README.md", "Optional.\n")
            self._write(
                root / "lib" / "eval_metrics.py",
                "from google.adk.evaluation.eval_metrics import EvalStatus\n"
                "from google.adk.evaluation.evaluator import (\n"
                "    EvaluationResult, PerInvocationResult,\n"
                ")\n\n"
                "def response_present(metric, actual, expected, scenario):\n"
                "    del metric, scenario\n"
                "    details = [\n"
                "        PerInvocationResult(\n"
                "            actual_invocation=item,\n"
                "            expected_invocation=(\n"
                "                expected[index] if expected else None\n"
                "            ),\n"
                "            score=1.0, eval_status=EvalStatus.PASSED,\n"
                "        )\n"
                "        for index, item in enumerate(actual)\n"
                "    ]\n"
                "    return EvaluationResult(\n"
                "        overall_score=1.0,\n"
                "        overall_eval_status=EvalStatus.PASSED,\n"
                "        per_invocation_results=details,\n"
                "    )\n",
            )
            self._write(
                root / "evals" / "portable.evalset.json",
                json.dumps(
                    {
                        "eval_set_id": "portable",
                        "eval_cases": [
                            {
                                "evalId": "langgraph-custom-metric",
                                "conversation": [
                                    {
                                        "userContent": {
                                            "role": "user",
                                            "parts": [{"text": "answer"}],
                                        },
                                        "finalResponse": {
                                            "role": "model",
                                            "parts": [
                                                {"text": "official response"}
                                            ],
                                        },
                                    },
                                    {
                                        "userContent": {
                                            "role": "user",
                                            "parts": [{"text": "answer again"}],
                                        },
                                        "finalResponse": {
                                            "role": "model",
                                            "parts": [
                                                {"text": "official response"}
                                            ],
                                        },
                                    },
                                ],
                                "sessionInput": {
                                    "appName": "root",
                                    "userId": "eval-user",
                                    "state": {},
                                },
                            }
                        ],
                    }
                ),
            )
            self._write(
                root / "evals" / "test_config.json",
                json.dumps(
                    {
                        "criteria": {"custom_quality": 1.0},
                        "customMetrics": {
                            "custom_quality": {
                                "codeConfig": {
                                    "name": (
                                        "harnest.lib.eval_metrics.response_present"
                                    )
                                }
                            }
                        },
                    }
                ),
            )

            status = run_agent_tests(
                root, include_evals=True, framework="langgraph"
            )

        self.assertEqual(status, 0)

    def test_orchestrator_loader_injects_import_free_prelude(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.py"
            self._write(
                path,
                "orchestrator = define_orchestrator(\n"
                "    agents=[AgentSource.directory('agents')],\n"
                "    parallelism=2,\n"
                ")\n",
            )
            orchestrator = load_orchestrator(path)

        self.assertEqual(orchestrator.parallelism, 2)
        self.assertEqual(orchestrator.sources[0].root, "agents")

    def test_bundle_agent_discovers_sorted_sibling_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            self._write(
                root / "tools" / "zeta.py",
                "from harnest import tool\n"
                "@tool\n"
                "def zeta(value: str) -> str:\n"
                "    \"\"\"Return a value.\"\"\"\n"
                "    return value\n",
            )
            self._write(
                root / "tools" / "alpha.py",
                "from harnest import tool\n"
                "@tool\n"
                "def alpha(value: str) -> str:\n"
                "    \"\"\"Return a value.\"\"\"\n"
                "    return value\n",
            )
            self._write(
                root / "subagents" / "reviewer.py",
                "from harnest import Agent\n"
                "reviewer = Agent(name='reviewer', model='gemini-test', "
                "instruction='Review.')\n",
            )
            self._write(
                root / "mcp" / "knowledge.py",
                "from harnest.mcp import MCPClient\n"
                "def client():\n"
                "    return MCPClient.streamable_http('https://mcp.example/mcp')\n",
            )
            self._write(
                root / "mcp" / "_optional.py",
                "# Private helpers are not discovered.\n",
            )

            modules = _fake_adk_modules()
            with patch.dict(sys.modules, modules):
                built = bundle_agent(
                    anchor,
                    Agent(
                        name="root",
                        model="gemini-test",
                        instruction="Delegate.",
                    ),
                )

        self.assertEqual(
            [function.__name__ for function in built.kwargs["tools"][:-1]],
            ["alpha", "zeta"],
        )
        self.assertEqual(
            built.kwargs["sub_agents"][0].kwargs["name"],
            "reviewer",
        )
        self.assertEqual(
            built.kwargs["tools"][-1].kwargs["connection_params"].kwargs["url"],
            "https://mcp.example/mcp",
        )

    def test_bundle_agent_composes_nested_subagent_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            nested = root / "subagents" / "researcher"
            self._write(nested / "instructions.md", "Nested instructions.\n")
            self._write(
                nested / "agent.py",
                "from harnest import Agent\n"
                "researcher = Agent(name='researcher', model='gemini-test')\n",
            )
            self._write(
                nested / "tools" / "lookup.py",
                "from harnest import tool\n"
                "@tool\n"
                "def lookup(query: str) -> str:\n"
                "    \"\"\"Look up a query.\"\"\"\n"
                "    return query\n",
            )
            self._write(
                nested / "subagents" / "critic.py",
                "from harnest import Agent\n"
                "critic = Agent(name='critic', model='gemini-test', "
                "instruction='Critique.')\n",
            )

            with patch.dict(sys.modules, _fake_adk_modules()):
                built = bundle_agent(
                    anchor,
                    Agent(name="root", model="gemini-test", instruction="Delegate."),
                )

        researcher = built.kwargs["sub_agents"][0]
        self.assertEqual(researcher.kwargs["name"], "researcher")
        self.assertEqual(researcher.kwargs["instruction"], "Nested instructions.")
        self.assertEqual(researcher.kwargs["tools"][0].__name__, "lookup")
        self.assertEqual(researcher.kwargs["sub_agents"][0].kwargs["name"], "critic")

    def test_bundle_agent_discovers_skills_as_shared_on_demand_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Use relevant skills.\n")
            for name in ("zeta", "alpha"):
                self._write(
                    root / "skills" / name / "SKILL.md",
                    "---\n"
                    f"name: {name}\n"
                    f"description: The {name} skill.\n"
                    "---\n"
                    f"Follow the {name} process.\n",
                )

            with patch.dict(sys.modules, _fake_adk_modules()):
                built = bundle_agent(
                    anchor,
                    Agent(name="root", model="gemini-test"),
                )

        self.assertEqual(built.kwargs["instruction"], "Use relevant skills.")
        self.assertEqual(
            [tool.__name__ for tool in built.kwargs["tools"]],
            ["list_skills", "load_skill", "load_skill_resource"],
        )

    def test_bundle_agent_rejects_invalid_and_conflicting_skills(self):
        @tool
        def load_skill(name: str) -> str:
            """Load an application-specific skill."""

            return name

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Use relevant skills.\n")
            skill = root / "skills" / "research"
            skill.mkdir(parents=True)

            with self.assertRaisesRegex(BundleSkillError, "uppercase SKILL.md"):
                bundle_agent(anchor, Agent(name="root", model="gemini-test"))

            self._write(
                skill / "SKILL.md",
                "---\nname: research\ndescription: Research.\n---\nResearch.\n",
            )
            with patch.dict(sys.modules, _fake_adk_modules()):
                with self.assertRaisesRegex(BundleDuplicateError, "skill tools"):
                    bundle_agent(
                        anchor,
                        Agent(
                            name="root",
                            model="gemini-test",
                            tools=[load_skill],
                        ),
                    )

    def test_bundle_agent_rejects_symlinks_inside_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Use relevant skills.\n")
            skill = root / "skills" / "research"
            self._write(
                skill / "SKILL.md",
                "---\nname: research\ndescription: Research.\n---\nResearch.\n",
            )
            outside = root / "outside.md"
            self._write(outside, "outside\n")
            references = skill / "references"
            references.mkdir()
            (references / "outside.md").symlink_to(outside)

            with self.assertRaisesRegex(BundleSkillError, "cannot be a symlink"):
                bundle_agent(anchor, Agent(name="root", model="gemini-test"))

    def test_discover_evals_validates_sorted_test_only_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Answer clearly.\n")
            for name in ("zeta", "alpha"):
                self._write(
                    root / "evals" / f"{name}.evalset.json",
                    json.dumps(
                        {
                            "eval_set_id": name,
                            "eval_cases": [{"eval_id": f"{name}-case"}],
                        }
                    ),
                )
            self._write(
                root / "evals" / "test_config.json",
                json.dumps({"criteria": {}}),
            )

            with patch.dict(sys.modules, _fake_adk_modules()):
                suite = discover_evals(anchor)
                built = bundle_agent(
                    anchor,
                    Agent(name="root", model="gemini-test"),
                )

        self.assertEqual(
            [path.name for path in suite.eval_sets],
            ["alpha.evalset.json", "zeta.evalset.json"],
        )
        self.assertEqual(suite.config.name, "test_config.json")
        self.assertEqual(built.kwargs["tools"], [])
        self.assertEqual(built.kwargs["instruction"], "Answer clearly.")

    def test_discover_evals_rejects_identity_duplicates_and_json_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            eval_path = root / "evals" / "quality.evalset.json"
            self._write(
                eval_path,
                json.dumps({"eval_set_id": "different", "eval_cases": []}),
            )
            modules = _fake_adk_modules()
            with patch.dict(sys.modules, modules):
                with self.assertRaisesRegex(BundleEvalError, "must match filename"):
                    discover_evals(anchor)

            self._write(
                eval_path,
                json.dumps(
                    {
                        "eval_set_id": "quality",
                        "eval_cases": [
                            {"eval_id": "same"},
                            {"eval_id": "same"},
                        ],
                    }
                ),
            )
            with patch.dict(sys.modules, modules):
                with self.assertRaisesRegex(BundleEvalError, "duplicate eval_id"):
                    discover_evals(anchor)

            self._write(
                eval_path,
                '{"eval_set_id":"quality","eval_set_id":"again",'
                '"eval_cases":[]}',
            )
            with patch.dict(sys.modules, modules):
                with self.assertRaisesRegex(BundleEvalError, "duplicate JSON key"):
                    discover_evals(anchor)

    def test_bundle_agent_reports_export_and_import_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            resource = root / "tools" / "search.py"
            self._write(resource, "different_name = object()\n")
            definition = Agent(name="root", model="gemini-test", instruction="Help.")

            with self.assertRaisesRegex(BundleExportError, "must export 'search'"):
                bundle_agent(anchor, definition)

            self._write(
                resource,
                "from harnest import tool\n"
                "@tool\n"
                "def search() -> str:\n"
                "    \"\"\"Search.\"\"\"\n"
                "    return 'result'\n"
                "@tool\n"
                "def extra() -> str:\n"
                "    \"\"\"An accidental second export.\"\"\"\n"
                "    return 'extra'\n",
            )
            with self.assertRaisesRegex(BundleExportError, "additional tool"):
                bundle_agent(anchor, definition)

            self._write(resource, "raise RuntimeError('broken resource')\n")
            with self.assertRaisesRegex(
                BundleImportError,
                "RuntimeError: broken resource",
            ):
                bundle_agent(anchor, definition)

    def test_bundle_agent_reports_duplicate_resources(self):
        @tool
        def search(query: str) -> str:
            """Search explicitly."""

            return query

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            self._write(
                root / "tools" / "search.py",
                "from harnest import tool\n"
                "@tool\n"
                "def search(query: str) -> str:\n"
                "    \"\"\"Search automatically.\"\"\"\n"
                "    return query\n",
            )
            definition = Agent(
                name="root",
                model="gemini-test",
                instruction="Help.",
                tools=[search],
            )
            with self.assertRaisesRegex(BundleDuplicateError, "duplicate tool 'search'"):
                bundle_agent(anchor, definition)

    def test_bundle_agent_enforces_anchor_and_nested_conventions(self):
        definition = Agent(name="root", model="gemini-test", instruction="Help.")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong_anchor = root / "main.py"
            self._write(wrong_anchor, "# wrong anchor\n")
            with self.assertRaisesRegex(BundleConventionError, "named agent.py"):
                bundle_agent(wrong_anchor, definition)

            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            (root / "subagents" / "researcher").mkdir(parents=True)
            with self.assertRaisesRegex(BundleConventionError, "must contain agent.py"):
                bundle_agent(anchor, definition)

    def test_bundle_agent_rejects_symlinked_resources_and_nested_anchors(self):
        definition = Agent(name="root", model="gemini-test", instruction="Help.")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            outside_tool = root / "outside.py"
            self._write(
                outside_tool,
                "from harnest import tool\n"
                "@tool\n"
                "def escaped() -> str:\n"
                "    \"\"\"Must not load.\"\"\"\n"
                "    return 'escaped'\n",
            )
            tools = root / "tools"
            tools.mkdir()
            (tools / "escaped.py").symlink_to(outside_tool)

            with self.assertRaisesRegex(BundleConventionError, "cannot be a symlink"):
                bundle_agent(anchor, definition)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            outside_agent = root / "outside.py"
            self._write(
                outside_agent,
                "from harnest import Agent\n"
                "researcher = Agent(name='researcher', model='gemini-test', "
                "instruction='Research.')\n",
            )
            nested = root / "subagents" / "researcher"
            nested.mkdir(parents=True)
            (nested / "agent.py").symlink_to(outside_agent)

            with self.assertRaisesRegex(BundleConventionError, "cannot be a symlink"):
                bundle_agent(anchor, definition)

    def test_bundle_agent_requires_filesystem_and_agent_names_to_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "agent.py"
            self._write(anchor, "# bundle anchor\n")
            self._write(root / "instructions.md", "Bundled instructions.\n")
            self._write(
                root / "subagents" / "researcher.py",
                "from harnest import Agent\n"
                "researcher = Agent(name='different', model='gemini-test', "
                "instruction='Research.')\n",
            )
            definition = Agent(name="root", model="gemini-test", instruction="Help.")

            with self.assertRaisesRegex(
                BundleExportError,
                "must have name 'researcher'",
            ):
                bundle_agent(anchor, definition)


if __name__ == "__main__":
    unittest.main()

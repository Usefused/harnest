"""Transport-binding metadata and borrowed lifecycle ownership regressions."""

import copy
import os
import types
import unittest
from unittest.mock import patch

from harnest.model import LiteLLMModel
from harnest.model_lifecycle import (
    LiteLLMLifecycle,
    close_litellm_lifecycles,
    propagate_litellm_lifecycles,
)
from harnest.model_transport import (
    ModelTransportBinding,
    model_transport_bindings,
)


class _RecordingLifecycle(LiteLLMLifecycle):
    """Count ownership operations without constructing a network transport."""

    def __init__(self):
        """Prepare a single sentinel transport shared by every borrower."""

        self.transport = object()
        self.created = 0
        self.closed = 0
        self.requests = []

    async def create_transport(self, context):
        """Return the owner's transport only when its controller initializes."""

        self.created += 1
        return self.transport

    async def before_request(self, request, context):
        """Record model routing and transport identity without credential data."""

        self.requests.append(
            (request["model"], context.framework, request["client"] is self.transport)
        )
        return request

    async def close(self, context):
        """Count only cleanup performed by the original resource owner."""

        self.closed += 1


class ModelTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """Keep optional LiteLLM imports fully offline in isolated test runs."""

        environment = patch.dict(
            os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_explicit_transport_arguments_are_private_and_borrowed_unchanged(self):
        """Native clients and private options survive reconstruction by identity."""

        native_client = object()
        headers = {"X-Synthetic-Private": "synthetic-header-value"}
        owner = LiteLLMModel(
            "openai/agent-model",
            client=native_client,
            api_key="synthetic-private-key",
            api_base="https://transport.invalid/v1",
            extra_headers=headers,
            temperature=0,
        ).build()
        binding = model_transport_bindings(owner)[0]
        borrowed = binding.build_eval_model("openai/judge-model")

        self.assertEqual(binding.model, "openai/agent-model")
        self.assertNotIn("synthetic-private-key", repr(binding))
        self.assertNotIn("synthetic-header-value", repr(binding))
        self.assertEqual(borrowed.model, "openai/judge-model")
        self.assertIs(borrowed._additional_args["client"], native_client)
        self.assertIs(borrowed._additional_args["extra_headers"], headers)
        self.assertNotIn("temperature", borrowed._additional_args)
        self.assertEqual(
            borrowed._additional_args["api_base"], "https://transport.invalid/v1"
        )
        self.assertEqual(model_transport_bindings(borrowed), ())
        self.assertFalse(hasattr(borrowed, "__harnest_litellm_resources__"))

    def test_only_explicit_transport_options_create_bindings(self):
        """Generation-only options do not masquerade as owned transport choices."""

        plain = LiteLLMModel("openai/agent-model", temperature=0).build()
        self.assertEqual(model_transport_bindings(plain), ())
        for key in (
            "client", "api_base", "api_key", "api_version", "organization",
            "extra_headers", "default_headers", "http_client",
        ):
            with self.subTest(option=key):
                adapter = LiteLLMModel(
                    "openai/agent-model", **{key: object()}
                ).build()
                self.assertEqual(len(model_transport_bindings(adapter)), 1)

    async def test_adk_eval_borrows_existing_controller_without_cleanup_ownership(self):
        """Agent and judge calls initialize one ADK transport and close it once."""

        lifecycle = _RecordingLifecycle()

        async def complete(_client, **kwargs):
            """Return offline data after the lifecycle has injected its client."""

            return {"same_transport": kwargs["client"] is lifecycle.transport}

        with patch(
            "google.adk.models.lite_llm.LiteLLMClient.acompletion", new=complete
        ):
            owner = LiteLLMModel(
                "openai/agent-model", lifecycle=lifecycle
            ).build()
            borrowed = model_transport_bindings(owner)[0].build_eval_model(
                "openai/judge-model"
            )
            self.assertIs(borrowed.llm_client, owner.llm_client)
            await owner.llm_client.acompletion(
                model=owner.model, messages=[], tools=[]
            )
            response = await borrowed.llm_client.acompletion(
                model=borrowed.model, messages=[], tools=[]
            )
            await close_litellm_lifecycles(borrowed)
            self.assertEqual(lifecycle.closed, 0)
            await close_litellm_lifecycles(owner)

        self.assertTrue(response["same_transport"])
        self.assertEqual(lifecycle.created, 1)
        self.assertEqual(lifecycle.closed, 1)
        self.assertEqual(
            lifecycle.requests,
            [("openai/agent-model", "adk", True), ("openai/judge-model", "adk", True)],
        )

    async def test_langgraph_eval_bridge_reuses_controller_and_original_owner(self):
        """An ADK judge borrows the LangGraph controller rather than cloning it."""

        lifecycle = _RecordingLifecycle()

        async def complete(**kwargs):
            """Observe the sentinel transport after the shared lifecycle hook."""

            return {"same_transport": kwargs["client"] is lifecycle.transport}

        with patch("litellm.acompletion", new=complete):
            owner = LiteLLMModel(
                "openai/agent-model", lifecycle=lifecycle
            ).build_langgraph()
            borrowed = model_transport_bindings(owner)[0].build_eval_model(
                "openai/simulator-model"
            )
            await owner.client.acompletion(model=owner.model, messages=[], tools=[])
            response = await borrowed.llm_client.acompletion(
                model=borrowed.model, messages=[], tools=[]
            )
            await close_litellm_lifecycles(borrowed)
            self.assertEqual(lifecycle.closed, 0)
            await close_litellm_lifecycles(owner)

        self.assertTrue(response["same_transport"])
        self.assertEqual(lifecycle.created, 1)
        self.assertEqual(lifecycle.closed, 1)
        self.assertEqual(
            lifecycle.requests,
            [
                ("openai/agent-model", "langgraph", True),
                ("openai/simulator-model", "langgraph", True),
            ],
        )

    async def test_propagation_deduplicates_bindings_and_cleanup_resources(self):
        """Repeated graph paths preserve one binding and one cleanup owner."""

        lifecycle = _RecordingLifecycle()
        source = LiteLLMModel("openai/agent-model", lifecycle=lifecycle).build()
        target = types.SimpleNamespace()
        propagate_litellm_lifecycles(source, target)
        propagate_litellm_lifecycles(source, target)

        self.assertEqual(len(model_transport_bindings(target)), 1)
        self.assertIs(
            model_transport_bindings(target)[0], model_transport_bindings(source)[0]
        )
        self.assertEqual(len(target.__harnest_litellm_resources__), 1)
        await close_litellm_lifecycles(target)
        await close_litellm_lifecycles(source)
        self.assertEqual(lifecycle.closed, 1)

    def test_binding_snapshots_top_level_arguments_without_copying_clients(self):
        """Later authoring-map edits cannot silently reroute an existing binding."""

        native_client = object()
        arguments = {"client": native_client, "api_base": "https://original.invalid"}
        binding = ModelTransportBinding("openai/agent-model", arguments)
        arguments["api_base"] = "https://changed.invalid"
        borrowed = binding.build_eval_model("openai/judge-model")

        self.assertEqual(borrowed._additional_args["api_base"], "https://original.invalid")
        self.assertIs(borrowed._additional_args["client"], native_client)
        self.assertIs(copy.deepcopy(binding), binding)

    def test_langgraph_explicit_native_client_remains_available_to_eval(self):
        """Preserve a direct provider client despite LangChain's adapter wrapping."""

        native_client = object()
        owner = LiteLLMModel(
            "openai/agent-model", client=native_client
        ).build_langgraph()
        borrowed = model_transport_bindings(owner)[0].build_eval_model(
            "openai/judge-model"
        )

        self.assertIs(owner.model_kwargs["client"], native_client)
        self.assertIs(borrowed._additional_args["client"], native_client)

    def test_authentication_and_tls_options_are_detected_and_preserved(self):
        """Supported credentials, providers, and CA settings survive borrowing."""

        def token_provider():
            """Supply a synthetic token only if a real caller requests one."""

            return "synthetic-token"

        options = {
            "azure_ad_token": "synthetic-token",
            "azure_ad_token_provider": token_provider,
            "ssl_verify": "/synthetic/ca.pem",
            "custom_llm_provider": "openai",
            "headers": {"X-Synthetic": "private-header"},
            "tenant_id": "tenant", "client_id": "identity",
            "client_secret": "synthetic-secret", "azure_username": "user",
            "azure_password": "synthetic-password", "azure_scope": "scope",
            "aws_region_name": "region", "aws_access_key_id": "synthetic-id",
            "aws_secret_access_key": "synthetic-secret",
            "aws_session_token": "synthetic-token", "aws_session_name": "session",
            "aws_profile_name": "profile", "aws_role_name": "role",
            "aws_web_identity_token": "synthetic-token",
            "aws_sts_endpoint": "https://sts.invalid",
            "aws_external_id": "external",
            "aws_bedrock_runtime_endpoint": "https://bedrock.invalid",
            "vertex_credentials": object(), "vertex_project": "project",
            "vertex_location": "location", "vertex_ai_credentials": object(),
            "vertex_ai_project": "project", "vertex_ai_location": "location",
        }
        for key, value in options.items():
            with self.subTest(option=key):
                owner = LiteLLMModel("openai/agent-model", **{key: value}).build()
                binding = model_transport_bindings(owner)[0]
                borrowed = binding.build_eval_model("openai/judge-model")
                self.assertIs(borrowed._additional_args[key], value)
                self.assertNotIn("synthetic", repr(binding))

    def test_adk_explicit_delegate_is_borrowed_without_cleanup_ownership(self):
        """ADK's public client delegate remains the same non-owned instance."""

        from google.adk.models.lite_llm import LiteLLMClient

        client = LiteLLMClient()
        owner = LiteLLMModel("openai/agent-model", llm_client=client).build()
        borrowed = model_transport_bindings(owner)[0].build_eval_model(
            "openai/judge-model"
        )
        self.assertIs(owner.llm_client, client)
        self.assertIs(borrowed.llm_client, client)
        self.assertNotIn("llm_client", borrowed._additional_args)
        self.assertFalse(hasattr(borrowed, "__harnest_litellm_resources__"))

    def test_langgraph_nested_transport_matches_effective_call_options(self):
        """Promoted nested credentials reach both agent and evaluator calls."""

        headers = {"X-Synthetic": "private-header"}
        owner = LiteLLMModel(
            "openai/agent-model",
            model_kwargs={
                "api_base": "https://nested.invalid/v1",
                "api_key": "synthetic-key", "organization": "synthetic-org",
                "extra_headers": headers, "temperature": 0.2,
            },
        ).build_langgraph()
        borrowed = model_transport_bindings(owner)[0].build_eval_model(
            "openai/judge-model"
        )
        for key in ("api_base", "api_key", "organization", "extra_headers"):
            self.assertEqual(owner._client_params[key], borrowed._additional_args[key])
        self.assertEqual(owner._client_params["api_base"], "https://nested.invalid/v1")
        self.assertIs(borrowed._additional_args["extra_headers"], headers)
        self.assertNotIn("temperature", borrowed._additional_args)

    async def test_langgraph_lifecycle_retains_nested_transport_options(self):
        """Nested routing is retained alongside the original lifecycle owner."""

        lifecycle = _RecordingLifecycle()

        async def complete(**kwargs):
            """Inspect the effective request after lifecycle client injection."""

            self.assertIs(kwargs["client"], lifecycle.transport)
            self.assertEqual(kwargs["api_base"], "https://nested.invalid/v1")
            self.assertEqual(kwargs["api_key"], "synthetic-key")
            return {}

        owner = LiteLLMModel(
            "openai/agent-model", lifecycle=lifecycle,
            model_kwargs={
                "api_base": "https://nested.invalid/v1", "api_key": "synthetic-key"
            },
        ).build_langgraph()
        borrowed = model_transport_bindings(owner)[0].build_eval_model(
            "openai/judge-model"
        )
        with patch("litellm.acompletion", new=complete):
            await owner.client.acompletion(**owner._client_params, messages=[])
            await borrowed.llm_client.acompletion(
                model=borrowed.model, messages=[], **borrowed._additional_args
            )
        await close_litellm_lifecycles(borrowed)
        self.assertEqual(lifecycle.closed, 0)
        await close_litellm_lifecycles(owner)
        self.assertEqual((lifecycle.created, lifecycle.closed), (1, 1))

    def test_langgraph_nested_credentials_preserve_top_level_precedence(self):
        """Explicit adapter credentials keep their existing conflict precedence."""

        owner = LiteLLMModel(
            "openai/agent-model", api_base="https://top.invalid/v1",
            model_kwargs={"api_base": "https://nested.invalid/v1"},
        ).build_langgraph()
        borrowed = model_transport_bindings(owner)[0].build_eval_model(
            "openai/judge-model"
        )
        self.assertEqual(owner._client_params["api_base"], "https://top.invalid/v1")
        self.assertEqual(borrowed._additional_args["api_base"], owner.api_base)
        with self.assertRaisesRegex(ValueError, "duplicate LiteLLM model option"):
            LiteLLMModel(
                "openai/agent-model", ssl_verify=False,
                model_kwargs={"ssl_verify": True},
            ).build_langgraph()

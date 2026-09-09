"""Keep implicit model configuration on Ollama without cloud-provider fallback."""

import os
import unittest
from unittest.mock import patch

from harnest.model import LiteLLMModel, OllamaModel
from harnest.model_transport import model_transport_bindings


class OllamaDefaultsTests(unittest.TestCase):
    def test_empty_environment_selects_existing_ollama_default(self):
        """A new agent needs Ollama, not an implicit OpenAI credential."""
        with patch.dict(os.environ, {}, clear=True):
            connector = OllamaModel.from_environment()
        self.assertEqual(connector.litellm_model, "ollama_chat/qwen3.5:cloud")
        self.assertEqual(connector.api_base, "http://localhost:11434")
        self.assertEqual(connector.completion_args, {})

    def test_openai_environment_cannot_redirect_ollama(self):
        """Ambient OpenAI settings must not turn the default into a billed API call."""
        environment = {"OPENAI_MODEL": "openai/unused", "OPENAI_BASE_URL": "https://unused.invalid/v1", "OPENAI_API_KEY": "synthetic-unused"}
        with patch.dict(os.environ, environment, clear=True):
            connector = OllamaModel.from_environment()
        self.assertEqual(connector.litellm_model, "ollama_chat/qwen3.5:cloud")
        self.assertEqual(connector.api_base, "http://localhost:11434")
        self.assertNotIn("api_key", connector.completion_args)

    def test_environment_and_explicit_overrides_remain_provider_scoped(self):
        """Configure a local or remote Ollama without copying unrelated credentials."""
        environment = {"OLLAMA_MODEL": " qwen3:8b ", "OLLAMA_BASE_URL": " http://ollama.test:11434 ", "OLLAMA_API_KEY": "synthetic-ollama-key"}
        with patch.dict(os.environ, environment, clear=True):
            connector = OllamaModel.from_environment(temperature=0)
            overridden = OllamaModel.from_environment(api_base="http://override.test", api_key="explicit-key")
        self.assertEqual(connector.model, "qwen3:8b")
        self.assertEqual(connector.api_base, "http://ollama.test:11434")
        self.assertEqual(connector.completion_args["api_key"], environment["OLLAMA_API_KEY"])
        self.assertNotIn(environment["OLLAMA_API_KEY"], repr(connector))
        self.assertEqual(overridden.api_base, "http://override.test")
        self.assertEqual(overridden.completion_args["api_key"], "explicit-key")

    def test_blank_ollama_values_fail_without_switching_provider(self):
        """Reject invalid explicit settings instead of silently choosing another host."""
        for variable in ("OLLAMA_MODEL", "OLLAMA_BASE_URL"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: " "}, clear=True):
                with self.assertRaises(ValueError):
                    OllamaModel.from_environment()

    def test_both_frameworks_retain_transport_for_evaluation(self):
        """The real adapters carry endpoint metadata without making network calls."""
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://ollama.test:11434"}, clear=True):
            connector = OllamaModel.from_environment()
        for adapter in (connector.build(), connector.build_langgraph()):
            binding, = model_transport_bindings(adapter)
            self.assertEqual(binding.model, "ollama_chat/qwen3.5:cloud")
            self.assertEqual(binding._completion_args["api_base"], "http://ollama.test:11434")

    def test_explicit_openai_factory_keeps_its_contract(self):
        """Changing defaults must not rewrite an authored OpenAI selection."""
        with patch.dict(os.environ, {"OPENAI_MODEL": "chosen-model"}, clear=True):
            self.assertEqual(LiteLLMModel.from_openai_environment().model, "openai/chosen-model")

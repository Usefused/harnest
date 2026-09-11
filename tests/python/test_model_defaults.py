"""Require explicit, vendor-neutral OpenAI-compatible configuration."""

import os
import unittest
from unittest.mock import patch

from harnest.model import LiteLLMModel
from harnest.model_transport import model_transport_bindings


class ModelDefaultsTests(unittest.TestCase):
    def test_missing_configuration_never_selects_a_provider(self):
        for environment, missing in (
            ({}, "OPENAI_MODEL"),
            ({"OPENAI_MODEL": "chosen"}, "OPENAI_BASE_URL"),
            ({"OLLAMA_MODEL": "legacy", "OLLAMA_BASE_URL": "http://legacy"}, "OPENAI_MODEL"),
        ):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, missing):
                    LiteLLMModel.from_openai_environment()

    def test_environment_is_captured_and_explicit_options_win(self):
        environment = {"OPENAI_MODEL": " team/chosen ", "OPENAI_BASE_URL": " https://models.test/v1 ", "OPENAI_API_KEY": "synthetic-key"}
        with patch.dict(os.environ, environment, clear=True):
            connector = LiteLLMModel.from_openai_environment(temperature=0)
            override = LiteLLMModel.from_openai_environment(api_base="http://override.test/v1", api_key="explicit-key")
        self.assertEqual(connector.model, "openai/team/chosen")
        self.assertEqual(connector.completion_args, {"api_base": "https://models.test/v1", "api_key": "synthetic-key", "temperature": 0})
        self.assertNotIn("synthetic-key", repr(connector))
        self.assertEqual(override.completion_args["api_base"], "http://override.test/v1")
        self.assertEqual(override.completion_args["api_key"], "explicit-key")

    def test_invalid_settings_fail_before_build(self):
        for model, endpoint in ((" ", "http://models.test/v1"), ("chosen", " "), ("chosen", "/v1"), ("chosen", "ftp://models.test")):
            with self.subTest(model=model, endpoint=endpoint), patch.dict(os.environ, {"OPENAI_MODEL": model, "OPENAI_BASE_URL": endpoint}, clear=True):
                with self.assertRaises(ValueError):
                    LiteLLMModel.from_openai_environment()

    def test_both_frameworks_retain_transport_for_evaluation(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "chosen", "OPENAI_BASE_URL": "http://models.test/v1"}, clear=True):
            connector = LiteLLMModel.from_openai_environment()
        for adapter in (connector.build(), connector.build_langgraph()):
            binding, = model_transport_bindings(adapter)
            self.assertEqual(binding.model, "openai/chosen")
            self.assertEqual(binding._completion_args["api_base"], "http://models.test/v1")
            self.assertEqual(binding._completion_args["api_key"], "not-required")

    def test_native_connector_is_removed(self):
        import harnest.model

        self.assertFalse(hasattr(harnest.model, "OllamaModel"))

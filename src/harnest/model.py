"""Lazy, framework-aware model connectors for Harnest agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os
from typing import Any, Mapping, TypeAlias
from urllib.parse import urlsplit

from .model_lifecycle import (
    LiteLLMContext,
    LiteLLMLifecycle,
)
from .model_hooks import ModelLifecycleError
from .lifecycle import ModelCallRequest, ModelCallResponse, ModelLifecycleContext, ModelMessage

# A model string, a ModelConnector, or a framework-specific model object. Native
# objects are validated by the framework adapter that lowers the agent.
ModelInput: TypeAlias = Any


def _openai_model_name_from_environment(
    default_model: str | None = None,
) -> str:
    """Resolve Harnest's canonical OpenAI-compatible model environment."""

    configured = os.getenv("OPENAI_MODEL", default_model or "").strip()
    if not configured:
        raise ValueError("OPENAI_MODEL is required; choose a model served by your endpoint")
    # A slash can belong to the server's model ID, not a LiteLLM provider.
    return _litellm_model_name(
        configured if configured.startswith("openai/") else f"openai/{configured}"
    )


def _openai_environment_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Capture explicit endpoint and credentials without a provider fallback."""

    resolved = dict(arguments)
    endpoint = resolved.get("api_base", os.getenv("OPENAI_BASE_URL", ""))
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("OPENAI_BASE_URL is required; set your OpenAI-compatible API URL")
    endpoint = endpoint.strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OPENAI_BASE_URL must be an absolute HTTP(S) URL")
    resolved["api_base"] = endpoint
    # Capture settings now so agent, judge and simulator retain the same
    # authority even if the process environment changes after compilation.
    # Compatible servers without authentication still need an SDK key value.
    resolved["api_key"] = resolved.get("api_key", os.getenv("OPENAI_API_KEY")) or "not-required"
    return resolved


class ModelConnector(ABC):
    """A lazily constructed model that each framework adapter can build.

    Connectors keep optional model-provider imports out of config discovery and
    agent validation. Plain model strings and already-built framework-specific
    model objects do not need a connector.
    """

    @abstractmethod
    def build(self) -> Any:
        """Build the model for the framework selected by Harnest."""


def _with_thinking_mode(
    completion_args: Mapping[str, Any], thinking: bool | None
) -> dict[str, Any]:
    """Translate Harnest's binary mode to LiteLLM's portable reasoning option."""

    arguments = dict(completion_args)
    if thinking is None:
        return arguments
    if not isinstance(thinking, bool):
        raise TypeError("model thinking must be a boolean or None")
    if "reasoning_effort" in arguments:
        raise ValueError("thinking and reasoning_effort cannot be used together")
    # Exact support depends on the endpoint; callers can pass reasoning_effort
    # directly when their provider requires a particular level.
    arguments["reasoning_effort"] = "medium" if thinking else "none"
    return arguments


def _litellm_model_name(model: str) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("LiteLLM provider-qualified model name is required")
    qualified = model.strip()
    provider, separator, provider_model = qualified.partition("/")
    if not separator or not provider.strip() or not provider_model.strip():
        raise ValueError("LiteLLM model must be provider-qualified as 'provider/model'")
    if any(character.isspace() for character in qualified):
        raise ValueError("LiteLLM provider-qualified model cannot contain whitespace")
    return qualified


def _manages_litellm_transport(completion_args: Mapping[str, Any]) -> bool:
    """Report whether Harnest may wrap the adapter's LiteLLM client.

    An explicitly authored ``client`` or ``llm_client`` already owns the
    transport, so the default prompt-cache wrapper must leave it untouched.
    """

    return not any(key in completion_args for key in ("client", "llm_client"))


def _validate_lifecycle(
    lifecycle: LiteLLMLifecycle | None, completion_args: Mapping[str, Any]
) -> None:
    if lifecycle is not None and not isinstance(lifecycle, LiteLLMLifecycle):
        raise TypeError("LiteLLM lifecycle must be a LiteLLMLifecycle")
    if lifecycle is not None and "client" in completion_args:
        raise ValueError("LiteLLM client and lifecycle cannot be configured together")


@dataclass(frozen=True, slots=True, init=False)
class LiteLLMModel(ModelConnector):
    """A provider-neutral model routed through a framework LiteLLM adapter.

    ``model`` must use LiteLLM's explicit ``provider/model`` form. All keyword
    arguments reach the underlying LiteLLM completion call, including provider
    settings such as ``api_base``, ``api_key``, and generation options.
    ``thinking`` selects a portable on/off mode; omit it to use the provider
    default or pass ``reasoning_effort`` directly for provider-specific levels.
    ``prompt_cache`` enables provider prompt-cache breakpoints where the
    provider requires them; it defaults to ``True`` and never changes OpenAI or
    Gemini requests.
    """

    model: str
    completion_args: Mapping[str, Any] = field(repr=False)
    lifecycle: LiteLLMLifecycle | None = field(default=None, repr=False)
    prompt_cache: bool = field(default=True, repr=False)

    def __init__(
        self,
        model: str,
        *,
        thinking: bool | None = None,
        lifecycle: LiteLLMLifecycle | None = None,
        prompt_cache: bool = True,
        **completion_args: Any,
    ) -> None:
        qualified = _litellm_model_name(model)
        _validate_lifecycle(lifecycle, completion_args)
        if not isinstance(prompt_cache, bool):
            raise TypeError("model prompt_cache must be a boolean")
        object.__setattr__(self, "model", qualified)
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "prompt_cache", prompt_cache)
        object.__setattr__(
            self,
            "completion_args",
            _with_thinking_mode(completion_args, thinking),
        )

    @classmethod
    def from_openai_environment(
        cls,
        *,
        default_model: str | None = None,
        thinking: bool | None = None,
        lifecycle: LiteLLMLifecycle | None = None,
        prompt_cache: bool = True,
        **completion_args: Any,
    ) -> "LiteLLMModel":
        """Use an explicitly configured OpenAI-compatible API, not a default vendor.

        OPENAI_MODEL and OPENAI_BASE_URL are required; OPENAI_API_KEY is optional.
        This reads the process environment without loading dotenv. Explicit
        completion arguments override environment values. Model IDs may contain
        slashes; the optional ``openai/`` prefix selects the wire protocol only.
        """

        return cls(
            _openai_model_name_from_environment(default_model),
            thinking=thinking,
            lifecycle=lifecycle,
            prompt_cache=prompt_cache,
            **_openai_environment_arguments(completion_args),
        )

    def build(self) -> Any:
        """Build for Harnest's selected framework without contacting the provider."""

        from .frameworks.context import current_framework

        return current_framework().build_litellm_model(self)


def resolve_model(model: ModelInput) -> Any:
    """Build connectors in Harnest's current scope; pass framework objects through."""

    return model.build() if isinstance(model, ModelConnector) else model


__all__ = [
    "ModelLifecycleError", "ModelCallRequest", "ModelCallResponse", "ModelLifecycleContext", "ModelMessage",
    "LiteLLMContext",
    "LiteLLMLifecycle",
    "LiteLLMModel",
    "ModelConnector",
    "resolve_model",
]

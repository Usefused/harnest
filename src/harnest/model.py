"""Lazy, framework-aware model connectors for Harnest agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

if TYPE_CHECKING:
    from google.adk.models import BaseLlm

    ModelInput: TypeAlias = str | BaseLlm | "ModelConnector"
else:
    # Avoid importing ADK merely to define or inspect an agent. Runtime
    # validation deliberately accepts custom BaseLlm implementations.
    ModelInput: TypeAlias = Any


class ModelConnector(ABC):
    """A lazily constructed model with ADK and LangGraph adapters.

    Connectors keep optional model-provider imports out of config discovery and
    agent validation. Plain model strings and already-built ADK ``BaseLlm``
    objects do not need a connector.
    """

    @abstractmethod
    def build(self) -> Any:
        """Build the model object accepted by ADK's ``LlmAgent``."""

    def build_for(self, framework: str) -> Any:
        """Build the connector for a supported Harnest framework."""

        if framework == "adk":
            return self.build()
        if framework == "langgraph":
            return self.build_langgraph()
        raise ValueError(f"unsupported agent framework: {framework}")

    def build_langgraph(self) -> Any:
        """Build a LangChain chat model for LangGraph."""

        raise NotImplementedError(
            f"{type(self).__name__} does not implement the LangGraph model adapter"
        )


@dataclass(frozen=True, slots=True, init=False)
class LiteLLMModel(ModelConnector):
    """A provider-neutral model routed through a framework LiteLLM adapter.

    ``model`` must use LiteLLM's explicit ``provider/model`` form. All keyword
    arguments are passed unchanged to the ADK or LangChain adapter, including
    provider settings such as ``api_base``, ``api_key``, and generation options.
    """

    model: str
    completion_args: Mapping[str, Any] = field(repr=False)

    def __init__(self, model: str, **completion_args: Any) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("LiteLLM provider-qualified model name is required")
        qualified = model.strip()
        provider, separator, provider_model = qualified.partition("/")
        if not separator or not provider.strip() or not provider_model.strip():
            raise ValueError(
                "LiteLLM model must be provider-qualified as 'provider/model'"
            )
        if any(character.isspace() for character in qualified):
            raise ValueError(
                "LiteLLM provider-qualified model cannot contain whitespace"
            )
        object.__setattr__(self, "model", qualified)
        object.__setattr__(self, "completion_args", dict(completion_args))

    def build(self) -> Any:
        """Build ADK's ``LiteLlm`` without contacting the provider."""

        try:
            from google.adk.models.lite_llm import LiteLlm
        except ImportError as exc:  # pragma: no cover - optional runtime import
            raise RuntimeError(
                "LiteLLMModel requires Google ADK's LiteLLM support; install "
                "harnest with its runtime dependencies"
            ) from exc
        return LiteLlm(model=self.model, **dict(self.completion_args))

    def build_langgraph(self) -> Any:
        """Build LangChain's LiteLLM chat model without contacting the provider."""

        try:
            from langchain_litellm import ChatLiteLLM
        except ImportError as exc:  # pragma: no cover - optional backend
            raise RuntimeError(
                "LiteLLMModel with LangGraph requires langchain-litellm"
            ) from exc
        return ChatLiteLLM(model=self.model, **dict(self.completion_args))


@dataclass(frozen=True, slots=True, init=False)
class OllamaModel(ModelConnector):
    """An Ollama model routed through the selected LiteLLM integration.

    ``chat=True`` uses LiteLLM's ``ollama_chat`` provider, which is the better
    default for agents that call tools. Additional keyword arguments pass to
    the selected framework adapter and then LiteLLM's completion API.
    """

    model: str
    api_base: str | None
    chat: bool
    completion_args: Mapping[str, Any] = field(repr=False)

    def __init__(
        self,
        model: str = "qwen3.5:cloud",
        *,
        api_base: str | None = None,
        chat: bool = True,
        **completion_args: Any,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Ollama model name is required")
        if api_base is not None and (
            not isinstance(api_base, str) or not api_base.strip()
        ):
            raise ValueError("Ollama api_base must be a non-empty URL")
        if not isinstance(chat, bool):
            raise TypeError("Ollama chat must be a boolean")

        object.__setattr__(self, "model", model.strip())
        object.__setattr__(self, "api_base", api_base.strip() if api_base else None)
        object.__setattr__(self, "chat", chat)
        object.__setattr__(self, "completion_args", dict(completion_args))

    @property
    def litellm_model(self) -> str:
        """Return the provider-qualified LiteLLM model name."""

        if self.model.startswith(("ollama/", "ollama_chat/")):
            return self.model
        provider = "ollama_chat" if self.chat else "ollama"
        return f"{provider}/{self.model}"

    def build(self) -> Any:
        """Build ADK's ``LiteLlm`` without contacting the Ollama server."""

        try:
            from google.adk.models.lite_llm import LiteLlm
        except ImportError as exc:  # pragma: no cover - optional runtime import
            raise RuntimeError(
                "OllamaModel requires Google ADK's LiteLLM support; install "
                "harnest with its runtime dependencies"
            ) from exc

        kwargs = dict(self.completion_args)
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base
        return LiteLlm(model=self.litellm_model, **kwargs)

    def build_langgraph(self) -> Any:
        """Build LangChain's LiteLLM chat model for Ollama."""

        try:
            from langchain_litellm import ChatLiteLLM
        except ImportError as exc:  # pragma: no cover - optional backend
            raise RuntimeError(
                "OllamaModel with LangGraph requires langchain-litellm"
            ) from exc
        kwargs = dict(self.completion_args)
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base
        return ChatLiteLLM(model=self.litellm_model, **kwargs)


def resolve_model(model: ModelInput) -> Any:
    """Resolve a Harnest connector while preserving strings and ADK models."""

    return model.build() if isinstance(model, ModelConnector) else model


def resolve_model_for(model: ModelInput, framework: str) -> Any:
    """Resolve a connector for one compiler backend."""

    return model.build_for(framework) if isinstance(model, ModelConnector) else model

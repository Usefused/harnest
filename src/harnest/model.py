"""Lazy, framework-aware model connectors for Harnest agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

from .model_lifecycle import (
    LiteLLMContext,
    LiteLLMLifecycle,
    _LifecycleLiteLLMClient,
    _attach_lifecycle_resource,
    create_adk_lifecycle_client,
)
from .model_transport import attach_model_transport_binding
from .model_hooks import ModelLifecycleError
from .lifecycle import ModelCallRequest, ModelCallResponse, ModelLifecycleContext, ModelMessage

if TYPE_CHECKING:
    from google.adk.models import BaseLlm

    ModelInput: TypeAlias = str | BaseLlm | "ModelConnector"
else:
    # Avoid importing ADK merely to define or inspect an agent. Runtime
    # validation deliberately accepts custom BaseLlm implementations.
    ModelInput: TypeAlias = Any


DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


def _openai_model_name_from_environment(
    default_model: str = DEFAULT_OPENAI_MODEL,
) -> str:
    """Resolve Harnest's canonical OpenAI-compatible model environment."""

    configured = os.getenv("OPENAI_MODEL", default_model).strip()
    if not configured:
        raise ValueError("OPENAI_MODEL cannot be empty")
    if "/" not in configured:
        configured = f"openai/{configured}"
    if configured.startswith("openai/"):
        return _litellm_model_name(configured)
    # OPENAI_MODEL deliberately selects the OpenAI-compatible transport so the
    # agent and evaluator share OPENAI_API_KEY and OPENAI_BASE_URL. Explicitly
    # authored LiteLLMModel instances remain available for native providers.
    raise ValueError(
        "OPENAI_MODEL must be an OpenAI model name or use the 'openai/' prefix"
    )


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
    # LiteLLM maps `none` to Ollama's `think: false`; an enabled default keeps
    # provider-specific wire details out of agent definitions while callers
    # can still pass reasoning_effort directly when they need an exact level.
    arguments["reasoning_effort"] = "medium" if thinking else "none"
    return arguments


def _langgraph_completion_args(
    adapter_type: Any, completion_args: Mapping[str, Any]
) -> dict[str, Any]:
    """Place LiteLLM call options where ChatLiteLLM will forward them."""

    arguments = dict(completion_args)
    nested = arguments.pop("model_kwargs", {})
    if not isinstance(nested, Mapping):
        raise TypeError("model_kwargs must be a mapping")
    model_kwargs = dict(nested)
    _promote_langgraph_credentials(arguments, model_kwargs)
    adapter_fields = set(getattr(adapter_type, "model_fields", {}))
    for name in tuple(arguments):
        if name in adapter_fields and name != "client":
            continue
        if name in model_kwargs:
            raise ValueError(f"duplicate LiteLLM model option: {name}")
        # ChatLiteLLM owns its `client` field as the LiteLLM delegate and
        # overwrites it during validation. A native provider client belongs to
        # completion kwargs, just like provider extensions and unknown options.
        model_kwargs[name] = arguments.pop(name)
    if model_kwargs:
        arguments["model_kwargs"] = model_kwargs
    return arguments


def _promote_langgraph_credentials(
    arguments: dict[str, Any], model_kwargs: dict[str, Any]
) -> None:
    """Keep nested credentials from being overwritten by empty adapter fields."""

    # ChatLiteLLM appends these fields after model_kwargs, even when None.
    # Preserve an explicitly authored top-level choice when both forms exist.
    for name in ("api_base", "api_key", "organization", "extra_headers"):
        if name in model_kwargs:
            arguments.setdefault(name, model_kwargs.pop(name))


def _langgraph_binding_args(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Capture normalized call options without carrying the LangChain wrapper."""

    flattened = dict(arguments)
    nested = flattened.pop("model_kwargs", {})
    # Credential fields were promoted before validation; other nested options
    # retain ChatLiteLLM's ordinary model_kwargs precedence.
    return {**flattened, **nested}


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
    """

    model: str
    completion_args: Mapping[str, Any] = field(repr=False)
    lifecycle: LiteLLMLifecycle | None = field(default=None, repr=False)

    def __init__(
        self,
        model: str,
        *,
        thinking: bool | None = None,
        lifecycle: LiteLLMLifecycle | None = None,
        **completion_args: Any,
    ) -> None:
        qualified = _litellm_model_name(model)
        _validate_lifecycle(lifecycle, completion_args)
        object.__setattr__(self, "model", qualified)
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(
            self,
            "completion_args",
            _with_thinking_mode(completion_args, thinking),
        )

    @classmethod
    def from_openai_environment(
        cls,
        *,
        default_model: str = DEFAULT_OPENAI_MODEL,
        thinking: bool | None = None,
        lifecycle: LiteLLMLifecycle | None = None,
        **completion_args: Any,
    ) -> "LiteLLMModel":
        """Use OPENAI_MODEL with the adapter's OPENAI_API_KEY/OPENAI_BASE_URL.

        This reads the process environment; it does not load a dotenv file.
        Explicit completion arguments retain their normal adapter precedence.
        """

        return cls(
            _openai_model_name_from_environment(default_model),
            thinking=thinking,
            lifecycle=lifecycle,
            **completion_args,
        )

    def build(self) -> Any:
        """Build ADK's ``LiteLlm`` without contacting the provider."""

        try:
            from google.adk.models.lite_llm import LiteLlm
        except ImportError as exc:  # pragma: no cover - optional runtime import
            raise RuntimeError(
                "LiteLLMModel requires Google ADK's LiteLLM support; install "
                "harnest with its runtime dependencies"
            ) from exc
        if self.lifecycle is None:
            adapter = LiteLlm(model=self.model, **dict(self.completion_args))
            return attach_model_transport_binding(
                adapter,
                model=self.model,
                completion_args=self.completion_args,
                borrowed_client=self.completion_args.get("llm_client"),
            )
        from google.adk.models.lite_llm import LiteLLMClient

        client = create_adk_lifecycle_client(
            LiteLLMClient, self.lifecycle, model=self.model
        )
        adapter = LiteLlm(
            model=self.model,
            llm_client=client,
            **dict(self.completion_args),
        )
        _attach_lifecycle_resource(adapter, client)
        return attach_model_transport_binding(
            adapter,
            model=self.model,
            completion_args=self.completion_args,
            borrowed_client=client,
        )

    def build_langgraph(self) -> Any:
        """Build LangChain's LiteLLM chat model without contacting the provider."""

        try:
            from langchain_litellm import ChatLiteLLM
        except ImportError as exc:  # pragma: no cover - optional backend
            raise RuntimeError(
                "LiteLLMModel with LangGraph requires langchain-litellm"
            ) from exc
        kwargs = _langgraph_completion_args(ChatLiteLLM, self.completion_args)
        adapter = ChatLiteLLM(model=self.model, **kwargs)
        binding_args = _langgraph_binding_args(kwargs)
        if self.lifecycle is None:
            return attach_model_transport_binding(
                adapter, model=self.model, completion_args=binding_args
            )
        client = _LifecycleLiteLLMClient(
            adapter.client, self.lifecycle, model=self.model, framework="langgraph"
        )
        # ChatLiteLLM validates by replacing `client` with the LiteLLM module.
        # Assigning after construction scopes the wrapper to this model only.
        adapter.client = client
        _attach_lifecycle_resource(adapter, client)
        return attach_model_transport_binding(
            adapter,
            model=self.model,
            completion_args=binding_args,
            borrowed_client=client,
        )


@dataclass(frozen=True, slots=True, init=False)
class OllamaModel(ModelConnector):
    """An Ollama model routed through the selected LiteLLM integration.

    ``chat=True`` uses LiteLLM's ``ollama_chat`` provider, which is the better
    default for agents that call tools. ``thinking`` has the same portable
    semantics as ``LiteLLMModel``. Additional keyword arguments pass to the
    selected framework adapter and then LiteLLM's completion API.
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
        thinking: bool | None = None,
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
        object.__setattr__(
            self,
            "completion_args",
            _with_thinking_mode(completion_args, thinking),
        )

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
        adapter_kwargs = _langgraph_completion_args(ChatLiteLLM, kwargs)
        return ChatLiteLLM(model=self.litellm_model, **adapter_kwargs)


def resolve_model(model: ModelInput) -> Any:
    """Resolve a Harnest connector while preserving strings and ADK models."""

    return model.build() if isinstance(model, ModelConnector) else model


def resolve_model_for(model: ModelInput, framework: str) -> Any:
    """Resolve a connector for one compiler backend."""

    return model.build_for(framework) if isinstance(model, ModelConnector) else model


__all__ = [
    "ModelLifecycleError", "ModelCallRequest", "ModelCallResponse", "ModelLifecycleContext", "ModelMessage",
    "LiteLLMContext",
    "LiteLLMLifecycle",
    "LiteLLMModel",
    "ModelConnector",
    "OllamaModel",
    "resolve_model",
    "resolve_model_for",
]

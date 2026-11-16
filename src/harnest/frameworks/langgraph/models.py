"""Build LangChain's ChatLiteLLM adapter for Harnest's LiteLLMModel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...model import LiteLLMModel, _manages_litellm_transport
from ...model_lifecycle import (
    LiteLLMLifecycle,
    _LifecycleLiteLLMClient,
    _attach_lifecycle_resource,
    _prompt_cache_transformers,
)
from ...model_transport import (
    FrameworkToolClient,
    attach_model_transport_binding,
)


def build_litellm_model(model: LiteLLMModel) -> Any:
    """Build LangChain's LiteLLM chat model without contacting the provider."""

    try:
        from langchain_litellm import ChatLiteLLM
    except ImportError as exc:  # pragma: no cover - optional backend
        raise RuntimeError(
            "LiteLLMModel with LangGraph requires langchain-litellm"
        ) from exc
    kwargs = _langgraph_completion_args(ChatLiteLLM, model.completion_args)
    adapter = ChatLiteLLM(model=model.model, **kwargs)
    adapter.client = FrameworkToolClient(adapter.client)
    binding_args = _langgraph_binding_args(kwargs)
    transformers = _prompt_cache_transformers(model.model, model.prompt_cache)
    auto_wrap = bool(transformers) and _manages_litellm_transport(
        model.completion_args
    )
    if model.lifecycle is None and not auto_wrap:
        return attach_model_transport_binding(
            adapter, model=model.model, completion_args=binding_args
        )
    client = _LifecycleLiteLLMClient(
        adapter.client,
        model.lifecycle or LiteLLMLifecycle(),
        model=model.model,
        framework="langgraph",
        request_transformers=transformers,
    )
    # ChatLiteLLM validates by replacing `client` with the LiteLLM module.
    # Assigning after construction scopes the wrapper to this model only.
    adapter.client = client
    _attach_lifecycle_resource(adapter, client)
    return attach_model_transport_binding(
        adapter,
        model=model.model,
        completion_args=binding_args,
        borrowed_client=client,
    )


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

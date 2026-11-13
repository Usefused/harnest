"""Build ADK's LiteLLM adapter for Harnest's LiteLLMModel."""

from __future__ import annotations

from typing import Any

from ...model import LiteLLMModel, _manages_litellm_transport
from ...model_lifecycle import (
    LiteLLMLifecycle,
    _attach_lifecycle_resource,
    _prompt_cache_transformers,
    create_adk_lifecycle_client,
)
from ...model_transport import (
    attach_model_transport_binding,
    framework_adk_client,
)


def build_litellm_model(model: LiteLLMModel) -> Any:
    """Build ADK's ``LiteLlm`` without contacting the provider."""

    try:
        from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
    except ImportError as exc:  # pragma: no cover - optional runtime import
        raise RuntimeError(
            "LiteLLMModel requires Google ADK's LiteLLM support; install "
            "harnest with its runtime dependencies"
        ) from exc
    transformers = _prompt_cache_transformers(model.model, model.prompt_cache)
    auto_wrap = bool(transformers) and _manages_litellm_transport(
        model.completion_args
    )
    if model.lifecycle is None and not auto_wrap:
        adapter = LiteLlm(model=model.model, **dict(model.completion_args))
        if "llm_client" not in model.completion_args:
            adapter.llm_client = framework_adk_client(LiteLLMClient)
        return attach_model_transport_binding(
            adapter,
            model=model.model,
            completion_args=model.completion_args,
            borrowed_client=model.completion_args.get("llm_client"),
        )
    client = create_adk_lifecycle_client(
        LiteLLMClient,
        model.lifecycle or LiteLLMLifecycle(),
        model=model.model,
        request_transformers=transformers,
    )
    adapter = LiteLlm(
        model=model.model,
        llm_client=client,
        **dict(model.completion_args),
    )
    _attach_lifecycle_resource(adapter, client)
    return attach_model_transport_binding(
        adapter,
        model=model.model,
        completion_args=model.completion_args,
        borrowed_client=client,
    )

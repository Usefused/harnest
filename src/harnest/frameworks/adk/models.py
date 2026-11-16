"""Build ADK's LiteLLM adapter and its lifecycle client for Harnest's LiteLLMModel."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ...model import LiteLLMModel, _manages_litellm_transport
from ...model_lifecycle import (
    LiteLLMLifecycle,
    _attach_lifecycle_resource,
    _LifecycleLiteLLMClient,
    _prompt_cache_transformers,
)
from ...model_transport import attach_model_transport_binding, framework_adk_client


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


def create_adk_lifecycle_client(
    client_type: type[Any],
    lifecycle: LiteLLMLifecycle,
    *,
    model: str,
    request_transformers: Sequence[Callable[[dict[str, Any]], Any]] = (),
) -> Any:
    """Create the concrete client type required by ADK's validated adapter."""

    delegate = framework_adk_client(client_type)
    controller = _LifecycleLiteLLMClient(
        delegate,
        lifecycle,
        model=model,
        framework="adk",
        request_transformers=request_transformers,
    )

    class ADKLifecycleClient(client_type):
        async def acompletion(
            self, model: Any, messages: Any, tools: Any, **kwargs: Any
        ) -> Any:
            return await controller.acompletion(
                model=model, messages=messages, tools=tools, **kwargs
            )

        def completion(
            self,
            model: Any,
            messages: Any,
            tools: Any,
            stream: bool = False,
            **kwargs: Any,
        ) -> Any:
            return controller.completion(
                model=model,
                messages=messages,
                tools=tools,
                stream=stream,
                **kwargs,
            )

        async def aclose(self) -> None:
            await controller.aclose()

        def close(self) -> None:
            controller.close()

    client = ADKLifecycleClient()
    client.__harnest_controller__ = controller
    return client

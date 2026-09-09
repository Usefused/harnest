"""ADK provider boundary for optional token policies."""

from __future__ import annotations

from typing import Any

from .model_lifecycle import propagate_litellm_lifecycles
from .token_runtime import observe, output_settings, prepare
from .tokens import TokenPolicy, TokenPolicyError, TokenRequest, _policy_for


def wrap_adk_model(model: Any, policy: TokenPolicy | None, name: str) -> Any:
    """Install the provider boundary only for explicitly configured agents."""
    if policy is None:
        return model
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.registry import LLMRegistry

    delegate = LLMRegistry.new_llm(model) if isinstance(model, str) else model

    class TokenModel(BaseLlm):
        """Delegate model capabilities while keeping reductions off stored state."""

        @property
        def capabilities(self) -> Any:
            """Preserve provider schema/tool capabilities across the wrapper."""
            return delegate.capabilities

        async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
            """Apply policy after callbacks and observe only final provider usage."""
            selected = _policy_for(policy, name)
            call = None
            request = llm_request
            if selected is not None:
                original = _request(llm_request, self.model)
                call = await prepare(original, selected, name)
                if call.request != original or (selected.enforce and selected.max_output_tokens is not None):
                    request = _apply(llm_request, call.request, selected)
            events = delegate.generate_content_async(request, stream=stream)
            try:
                async for event in events:
                    if call is not None and not getattr(event, "partial", False):
                        await observe(call, "after", **_usage(event))
                    yield event
            except Exception:
                if call is not None:
                    await observe(call, "error")
                raise
            finally:
                await events.aclose()

        def connect(self, llm_request: Any) -> Any:
            """Reject active policy on live audio instead of silently bypassing it."""
            if _policy_for(policy, name) is not None:
                raise TokenPolicyError("token policy does not support ADK live connections; disable it explicitly")
            return delegate.connect(llm_request)

    return propagate_litellm_lifecycles(delegate, TokenModel(model=delegate.model))


def _request(native: Any, model: str) -> TokenRequest:
    """Include instructions, function schemas and generation settings in counting."""
    settings = native.config.model_dump(exclude_none=True) if native.config else {}
    system = settings.pop("system_instruction", None)
    tool_groups = settings.pop("tools", [])
    tools = tuple(
        declaration for group in tool_groups
        for declaration in group.get("function_declarations", [])
    )
    # Built-in provider tools must also contribute to the estimated prompt size.
    metadata = {"provider_tools": [group for group in tool_groups if "function_declarations" not in group]}
    return TokenRequest(
        "adk", model,
        tuple(content.model_dump(exclude_none=True) for content in native.contents),
        system, tools, settings, metadata,
    )


def _apply(native: Any, request: TokenRequest, policy: TokenPolicy) -> Any:
    """Clone only model payloads; callable tools remain owned by ADK."""
    from google.genai import types

    current = native.model_copy()
    current.contents = [types.Content.model_validate(item) for item in request.messages]
    settings = output_settings(request.settings, policy, "max_output_tokens")
    groups = native.config.model_dump(exclude_none=True).get("tools", []) if native.config else []
    names = {tool["name"] for tool in request.tools}
    settings["tools"] = _filter_tools(groups, names)
    settings["system_instruction"] = request.system
    current.config = types.GenerateContentConfig.model_validate(settings)
    current.tools_dict = {name: tool for name, tool in native.tools_dict.items() if name in names}
    return current


def _filter_tools(groups: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    """Preserve provider-owned built-ins while filtering function declarations."""
    result = []
    for group in groups:
        if "function_declarations" not in group:
            result.append(group)
            continue
        declarations = [item for item in group["function_declarations"] if item["name"] in names]
        if declarations:
            result.append({**group, "function_declarations": declarations})
    return result


def _usage(event: Any) -> dict[str, int | None]:
    """Read actual usage separately from admission estimates."""
    native = getattr(event, "usage_metadata", None)
    return {
        "input_tokens": getattr(native, "prompt_token_count", None),
        "output_tokens": getattr(native, "candidates_token_count", None),
        "total_tokens": getattr(native, "total_token_count", None),
        "cached_input_tokens": getattr(native, "cached_content_token_count", None),
        "reasoning_tokens": getattr(native, "thoughts_token_count", None),
    }

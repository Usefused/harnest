"""LangChain middleware for optional managed-agent token policies."""

from __future__ import annotations

from typing import Any

from .token_runtime import observe, observe_sync, output_settings, prepare, prepare_sync
from .tokens import TokenPolicy, TokenRequest, _policy_for


def token_middleware(policy: TokenPolicy | None, name: str) -> tuple[Any, ...]:
    """Keep unconfigured agents free of token policy middleware."""
    if policy is None:
        return ()
    from langchain.agents.middleware import AgentMiddleware

    class TokenMiddleware(AgentMiddleware):
        """Transform model-only requests without updating graph checkpoints."""

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            """Apply asynchronous strategies and retain native response identity."""
            selected = _policy_for(policy, name)
            if selected is None:
                return await handler(request)
            original = _request(request)
            call = await prepare(original, selected, name)
            try:
                response = await handler(_apply(request, original, call.request, selected))
            except Exception:
                await observe(call, "error")
                raise
            await observe(call, "after", **_usage(response))
            return response

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            """Apply synchronous strategies with identical budget semantics."""
            selected = _policy_for(policy, name)
            if selected is None:
                return handler(request)
            original = _request(request)
            call = prepare_sync(original, selected, name)
            try:
                response = handler(_apply(request, original, call.request, selected))
            except Exception:
                observe_sync(call, "error")
                raise
            observe_sync(call, "after", **_usage(response))
            return response

    return (TokenMiddleware(),)


def _request(native: Any) -> TokenRequest:
    """Count system messages and tool schemas alongside native message content."""
    from langchain_core.messages import message_to_dict, messages_to_dict
    from langchain_core.utils.function_calling import convert_to_openai_tool

    tools = tuple(_descriptor(tool, convert_to_openai_tool) for tool in native.tools)
    system = message_to_dict(native.system_message) if native.system_message else None
    settings = dict(native.model_settings)
    # Output schemas also occupy prompt space even when no explicit tool exists.
    metadata = {"response_format": _schema(native.response_format)}
    model = getattr(native.model, "model_name", None) or getattr(native.model, "model", None)
    return TokenRequest(
        "langgraph", str(model or type(native.model).__name__),
        tuple(messages_to_dict(native.messages)), system, tools, settings, metadata,
    )


def _schema(value: Any) -> Any:
    """Keep schema estimates deterministic without serializing model objects."""
    schema = getattr(value, "schema", value)
    json_schema = getattr(schema, "model_json_schema", None)
    if callable(json_schema):
        return json_schema()
    return schema if isinstance(schema, dict) else None


def _descriptor(tool: Any, converter: Any) -> dict[str, Any]:
    """Normalize function tool descriptors while keeping provider tools named."""
    converted = converter(tool)
    if "function" in converted:
        return converted["function"]
    return {**converted, "name": converted.get("name", converted["type"])}


def _apply(native: Any, original: TokenRequest, request: TokenRequest, policy: TokenPolicy) -> Any:
    """Override only changed model inputs, leaving persisted native state intact."""
    from langchain_core.messages import messages_from_dict

    updates: dict[str, Any] = {}
    if request.messages != original.messages:
        updates["messages"] = messages_from_dict(list(request.messages))
    if request.system != original.system:
        updates["system_message"] = messages_from_dict([request.system])[0] if request.system else None
    if request.tools != original.tools:
        names = {tool["name"] for tool in request.tools}
        updates["tools"] = [
            tool for tool, descriptor in zip(native.tools, original.tools)
            if descriptor["name"] in names
        ]
    settings = _settings(native, request.settings, policy)
    if settings != native.model_settings:
        updates["model_settings"] = settings
    return native.override(**updates) if updates else native


def _settings(native: Any, configured: dict[str, Any], policy: TokenPolicy) -> dict[str, Any]:
    """Preserve stricter model-owned limits and provider output parameter names."""
    settings = dict(configured)
    if not policy.enforce or policy.max_output_tokens is None:
        return settings
    keys = ("max_completion_tokens", "max_output_tokens", "max_tokens")
    key = policy.output_token_parameter or next((name for name in keys if name in settings), "max_tokens")
    limits = [settings.get(name, getattr(native.model, name, None)) for name in keys]
    present = [value for value in limits if type(value) is int]
    if present:
        settings[key] = min(present)
    return output_settings(settings, policy, key)


def _usage(response: Any) -> dict[str, int | None]:
    """Aggregate completed model message usage, never individual stream chunks."""
    usages = [getattr(message, "usage_metadata", None) for message in response.result]
    reported = [usage for usage in usages if usage]
    result = {
        name: sum(usage[name] for usage in reported if type(usage.get(name)) is int)
        if any(type(usage.get(name)) is int for usage in reported) else None
        for name in ("input_tokens", "output_tokens", "total_tokens")
    }
    result["cached_input_tokens"] = _detail(reported, "input_token_details", "cache_read")
    result["reasoning_tokens"] = _detail(reported, "output_token_details", "reasoning")
    return result


def _detail(usages: list[dict[str, Any]], group: str, key: str) -> int | None:
    """Keep unreported cache/reasoning details distinct from measured zero."""
    values = [usage.get(group, {}).get(key) for usage in usages]
    present = [value for value in values if type(value) is int]
    return sum(present) if present else None

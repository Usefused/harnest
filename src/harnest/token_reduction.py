"""Deterministic reductions on detached model input, never durable history."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from typing import Any

from .tokens import TokenCount, TokenPolicy, TokenPolicyError, TokenRequest


def estimate_tokens(request: TokenRequest) -> TokenCount:
    """Estimate serialized text at four characters/token, including schemas.

    This deliberately labels an approximation: media and provider framing need
    an authored counter for accurate admission decisions.
    """
    value = (request.messages, request.system, request.tools, request.settings, request.context_metadata)
    size = len(json.dumps(value, ensure_ascii=False, default=str))
    return TokenCount((size + 3) // 4, estimated=True)


def reduce_request(request: TokenRequest, policy: TokenPolicy) -> TokenRequest:
    """Apply only explicitly enabled reductions to an isolated working copy."""
    current = deepcopy(request)
    if policy.keep_recent_turns is not None:
        current = _recent_turns(current, policy.keep_recent_turns)
    if policy.max_tool_result_chars is not None:
        current = _tool_text(current, policy.max_tool_result_chars)
    return current


def _is_user_turn(message: dict[str, Any], framework: str) -> bool:
    """Distinguish human turns from ADK user-role function responses."""
    if framework == "langgraph":
        return message.get("type") == "human"
    return message.get("role") == "user" and not any(
        part.get("function_response") for part in message.get("parts", ())
    )


def _is_instruction(message: dict[str, Any], framework: str) -> bool:
    """Pin system/developer messages independently of conversation retention."""
    role = message.get("type") if framework == "langgraph" else message.get("role")
    return role in {"system", "developer"}


def _recent_turns(request: TokenRequest, keep: int) -> TokenRequest:
    """Drop whole earlier turns so active tool-call/result groups stay together."""
    starts = [
        index for index, message in enumerate(request.messages)
        if _is_user_turn(message, request.framework)
    ]
    if len(starts) <= keep:
        return request
    boundary = starts[-keep]
    messages = tuple(
        message for index, message in enumerate(request.messages)
        if index >= boundary or _is_instruction(message, request.framework)
    )
    return replace(request, messages=messages)


def _short_text(value: str, limit: int) -> str:
    """Mark omitted text while keeping the configured character ceiling."""
    if len(value) <= limit:
        return value
    marker = "…[truncated]"
    if limit < len(marker):
        return marker[:limit]
    return value[:limit - len(marker)] + marker


def _short_strings(value: Any, limit: int) -> Any:
    """Preserve JSON structure and scalar types while bounding string leaves."""
    if isinstance(value, str):
        return _short_text(value, limit)
    if isinstance(value, list):
        return [_short_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _short_strings(item, limit) for key, item in value.items()}
    return value


def _tool_text(request: TokenRequest, limit: int) -> TokenRequest:
    """Reduce tool response content only; keep call identifiers and metadata."""
    for message in request.messages:
        if request.framework == "langgraph":
            _langgraph_tool_text(message, limit)
        else:
            _adk_tool_text(message, limit)
    return request


def _langgraph_tool_text(message: dict[str, Any], limit: int) -> None:
    """Keep non-text content blocks intact, including media and provider data."""
    if message.get("type") != "tool":
        return
    data = message["data"]
    content = data.get("content")
    if isinstance(content, str):
        data["content"] = _short_text(content, limit)
    elif isinstance(content, list):
        data["content"] = [_short_block(block, limit) for block in content]


def _short_block(block: Any, limit: int) -> Any:
    """Shorten only recognised text blocks, never URLs or inline media bytes."""
    if isinstance(block, str):
        return _short_text(block, limit)
    if isinstance(block, dict) and block.get("type") == "text":
        return {**block, "text": _short_text(block["text"], limit)}
    return block


def _adk_tool_text(message: dict[str, Any], limit: int) -> None:
    """Retain function response identity while reducing its JSON text leaves."""
    for part in message.get("parts", ()):
        response = part.get("function_response")
        if response:
            response["response"] = _short_strings(response.get("response"), limit)


def selected_tools(request: TokenRequest, names: Any) -> TokenRequest:
    """Allow a strategy to select existing tools without granting new ones."""
    if not isinstance(names, (tuple, list)) or any(not isinstance(n, str) for n in names):
        raise TokenPolicyError("tool_selector must return a list or tuple of tool names")
    known = {tool["name"] for tool in request.tools}
    if len(set(names)) != len(names) or not set(names).issubset(known):
        raise TokenPolicyError("tool_selector returned duplicate or unknown tools")
    # Preserve original ordering for stable prefixes, regardless of search rank.
    return replace(request, tools=tuple(t for t in request.tools if t["name"] in names))

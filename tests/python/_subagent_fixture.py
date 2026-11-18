"""Scripted ADK and LangGraph models plus a journal for SubAgent integration tests.

Each model decides its next step from the conversation it receives, so tests
observe real framework history rather than a canned response list.
"""

from __future__ import annotations

from typing import Any
import uuid

JOURNAL: list[tuple[str, Any]] = []


def record(kind: str, value: Any) -> None:
    JOURNAL.append((kind, value))


def entries(kind: str) -> list[Any]:
    return [value for name, value in JOURNAL if name == kind]


def _lead_step(user_texts: list[str], tool_result: str | None) -> tuple[str, Any]:
    """Call the root tool named by the user, then report what it returned."""

    if tool_result is not None:
        return "text", f"root saw: {tool_result}"
    return "call", (user_texts[-1].strip(), {"topic": "ants"})


def _researcher_step(user_texts: list[str], tool_result: str | None) -> tuple[str, Any]:
    """Answer with the number of user turns seen, calling probe when asked."""

    turns = len(user_texts)
    if tool_result is not None:
        return "text", f"sub turn {turns}: probe={tool_result}"
    if "probe" in user_texts[-1]:
        return "call", ("probe", {})
    return "text", f"sub turn {turns}: {user_texts[-1]}"


_PLANS = {"lead": _lead_step, "researcher": _researcher_step}


def adk_model(plan: str) -> Any:
    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    class ScriptedADKModel(BaseLlm):
        async def generate_content_async(self, llm_request: Any, stream: bool = False):
            user_texts: list[str] = []
            tool_result = None
            for content in llm_request.contents or ():
                for part in content.parts or ():
                    if content.role == "user" and part.text:
                        user_texts.append(part.text)
                        tool_result = None
                    response = getattr(part, "function_response", None)
                    if response is not None:
                        payload = response.response or {}
                        tool_result = str(payload.get("result", payload))
            kind, value = _PLANS[self.model](user_texts, tool_result)
            if kind == "text":
                part = types.Part(text=value)
            else:
                name, args = value
                part = types.Part(function_call=types.FunctionCall(name=name, args=args))
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    return ScriptedADKModel(model=plan)


def graph_model(plan: str) -> Any:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class ScriptedGraphModel(BaseChatModel):
        plan: str

        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
            user_texts = [str(item.content) for item in messages if isinstance(item, HumanMessage)]
            last = messages[-1] if messages else None
            tool_result = str(last.content) if isinstance(last, ToolMessage) else None
            kind, value = _PLANS[self.plan](user_texts, tool_result)
            if kind == "text":
                message = AIMessage(content=value)
            else:
                name, args = value
                message = AIMessage(
                    content="",
                    tool_calls=[{"name": name, "args": args, "id": uuid.uuid4().hex, "type": "tool_call"}],
                )
            return ChatResult(generations=[ChatGeneration(message=message)])

    return ScriptedGraphModel(plan=plan)


def model(framework: str, plan: str) -> Any:
    return adk_model(plan) if framework == "adk" else graph_model(plan)

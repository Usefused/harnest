"""Deterministic managed ADK agent consuming Hatchet through its plugin API."""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.adk.models import BaseLlm, LlmResponse
from google.genai import types

from harnest.agent import Agent


def _record(phase: str) -> None:
    """Record model execution without putting prompts or job results in evidence."""

    target = os.environ.get("HARNEST_HATCHET_CONSUMER_EVENTS")
    if target is None:
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"component": "model", "phase": phase}) + "\n")


def _tool_result(request) -> object | None:
    """Return the first ADK function response after the authored tool completes."""

    for content in request.contents:
        for part in content.parts:
            response = part.function_response
            if response is not None:
                return response.response
    return None


def _require_consumer_tool(request) -> None:
    """Reject plugin-contributed tools while requiring filesystem discovery."""

    names = set(request.tools_dict)
    if names != {"create_report_job"}:
        # This fixture proves plugins expose native capabilities; adding a
        # plugin tool would silently test a different ownership model.
        raise RuntimeError("the consumer must own the only Hatchet-facing tool")


class DeterministicHatchetModel(BaseLlm):
    """Request the consumer-owned job tool once, then report its result."""

    async def generate_content_async(self, request, stream=False):
        """Select the next deterministic response from ADK's tool history."""

        del stream
        _require_consumer_tool(request)
        result = _tool_result(request)
        if result is None:
            _record("submit")
            part = types.Part(
                function_call=types.FunctionCall(
                    id="hatchet-consumer-call",
                    name="create_report_job",
                    args={"topic": "quarterly"},
                )
            )
        else:
            _record("finish")
            part = types.Part(
                text="report:"
                + json.dumps(result, separators=(",", ":"), sort_keys=True)
            )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


root_agent = Agent(
    name="hatchet_consumer",
    description="Submit and await externally executed report jobs.",
    model=DeterministicHatchetModel(model="deterministic-hatchet-consumer"),
    instruction="Use the report job tool and return its completed result.",
)

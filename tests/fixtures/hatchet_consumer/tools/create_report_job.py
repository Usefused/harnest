"""Consumer-owned agent tool backed by the public Hatchet plugin capability."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harnest.extensions.hatchet import hatchet
from harnest.tool import tool


def _record(phase: str) -> None:
    """Record bounded execution evidence without job inputs or outputs."""

    target = os.environ.get("HARNEST_HATCHET_CONSUMER_EVENTS")
    if target is None:
        return
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"component": "tool", "phase": phase}) + "\n")


@tool(durable=True)
async def create_report_job(topic: str) -> Any:
    """Create a durable external report job and return its completed result."""

    _record("enter")
    # Correlation is deliberately implicit: the plugin must bind the current
    # Harnest principal, session, and invocation rather than trusting tool input.
    job = await hatchet.run("consumer-report", {"topic": topic})
    _record("submitted")
    result = await hatchet.wait(job)
    # LangGraph re-enters this frame and reaches the tail return. ADK injects
    # the result as FunctionResponse, so statements after the wait do not run.
    _record("completed")
    return result

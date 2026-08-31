"""Invocation helpers for progressive skill tool tests."""

from __future__ import annotations

import asyncio
from typing import Any

from harnest.context import activate_context, create_agent_context, revoke_context


def run_skill_tool(
    application: Any,
    agent_name: str,
    operation: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call an async model tool inside the same context supplied by runtimes."""

    async def invoke() -> Any:
        active = create_agent_context(
            framework=application.framework,
            agent_name=agent_name,
            invocation_id="skill-test-invocation",
            user_id="skill-test-user",
            session_id="skill-test-session",
            metadata={},
            resources={},
            skill_registry=application.skill_registry,
        )
        try:
            with activate_context(active):
                return await operation(*args, **kwargs)
        finally:
            revoke_context(active)

    return asyncio.run(invoke())


__all__ = ["run_skill_tool"]

"""Keep unassigned graph components outside managed agent sandbox authority.

Each framework adapter wraps its own native graph nodes with these helpers
(``deny_adk_node``, ``deny_langgraph_node``).
"""

import functools
import inspect
from typing import Any

from .context_sandboxes import deny_sandbox_authority


async def deny_event_stream(events: Any) -> Any:
    """Scope iterator work and cleanup without leaking denial to its consumer."""
    try:
        while True:
            with deny_sandbox_authority():
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    break
            yield event
    finally:
        with deny_sandbox_authority():
            await events.aclose()


def deny_graph_callable(operation: Any) -> Any:
    """Deny inherited grants while retaining a callable's sync/async contract."""
    if inspect.iscoroutinefunction(operation):
        @functools.wraps(operation)
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            with deny_sandbox_authority():
                return await operation(*args, **kwargs)
    else:
        @functools.wraps(operation)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            with deny_sandbox_authority():
                return operation(*args, **kwargs)
    return invoke

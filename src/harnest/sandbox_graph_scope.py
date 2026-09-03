"""Keep unassigned graph components outside managed agent sandbox authority."""

import functools
import inspect
from typing import Any

from .context_sandboxes import deny_sandbox_authority


async def _denied_events(events: Any) -> Any:
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


def deny_adk_node(target: Any) -> Any:
    """Retain native node identity, validation, retries, and lifecycle ownership."""
    if getattr(target, "_harnest_sandbox_graph_scoped", False):
        return target
    original = target.run

    @functools.wraps(original)
    async def run(*args: Any, **kwargs: Any) -> Any:
        """Cover actual node execution, including generator advancement."""
        with deny_sandbox_authority():
            events = _denied_events(original(*args, **kwargs))
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    object.__setattr__(target, "run", run)
    object.__setattr__(target, "_harnest_sandbox_graph_scoped", True)
    return target


def deny_langgraph_node(target: Any) -> Any:
    """Protect native Pregel calls without replacing its checkpoint semantics."""
    if getattr(target, "_harnest_sandbox_graph_scoped", False):
        return target
    for name in ("invoke", "ainvoke"):
        object.__setattr__(target, name, deny_graph_callable(getattr(target, name)))
    original_async = target.astream
    original_sync = target.stream

    @functools.wraps(original_async)
    async def astream(*args: Any, **kwargs: Any) -> Any:
        """Protect native streaming while preserving the caller's scope."""
        with deny_sandbox_authority():
            events = _denied_events(original_async(*args, **kwargs))
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    @functools.wraps(original_sync)
    def stream(*args: Any, **kwargs: Any) -> Any:
        """Apply the same boundary to synchronous graph consumption."""
        with deny_sandbox_authority():
            events = original_sync(*args, **kwargs)
        try:
            while True:
                with deny_sandbox_authority():
                    try:
                        event = next(events)
                    except StopIteration:
                        break
                yield event
        finally:
            with deny_sandbox_authority():
                events.close()

    object.__setattr__(target, "astream", astream)
    object.__setattr__(target, "stream", stream)
    object.__setattr__(target, "_harnest_sandbox_graph_scoped", True)
    return target

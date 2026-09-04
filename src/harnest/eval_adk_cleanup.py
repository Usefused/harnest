"""Close evaluator invocation scopes on legacy ADK generator interruption."""

import asyncio
from typing import Any, Awaitable, Callable


def guarded_eval_root(root: Any, close_scope: Callable[[], Awaitable[None]]) -> Any:
    """Copy only legacy agents whose native runner omits cancellation callbacks."""
    if not _legacy_root(root):
        return root
    # The node runner already finalizes callbacks in a finally block. Legacy
    # BaseAgent execution does not; guard its eval-only copy without changing
    # the live application's agent or its model/resource ownership.
    copied = root.model_copy()
    for name in ("run_async", "run_live"):
        method = getattr(copied, name, None)
        if callable(method):
            object.__setattr__(copied, name, _guarded_stream(method, close_scope))
    return copied


def _legacy_root(root: Any) -> bool:
    """Match ADK's current legacy runner branch without changing node execution."""
    from google.adk.agents import BaseAgent, LlmAgent

    return isinstance(root, BaseAgent) and not isinstance(root, LlmAgent)


def guarded_eval_plugins(
    root: Any, plugins: list[Any], close_scope: Callable[[], Awaitable[None]],
) -> list[Any]:
    """Borrow native plugins and guard legacy callback cancellation paths."""
    legacy = _legacy_root(root)
    return [_guarded_plugin(plugin, close_scope, legacy=legacy) for plugin in plugins]


def _guarded_plugin(
    plugin: Any, close_scope: Callable[[], Awaitable[None]], *, legacy: bool,
) -> Any:
    """Delegate to authored objects without mutating their methods or callback state."""
    from google.adk.plugins import BasePlugin

    guarded = BasePlugin(name=plugin.name)
    for name in dir(BasePlugin):
        if name.endswith("_callback"):
            method = getattr(plugin, name)
            callback = _guarded_callback(method, close_scope, name, legacy=legacy)
            setattr(guarded, name, callback)
    # Runner.close is per evaluation case; the CLI/server runtime still owns
    # authored plugins. BasePlugin.close is a no-op on this resource-free proxy.
    return guarded


def _guarded_callback(
    method: Any, close_scope: Callable[[], Awaitable[None]], name: str, *, legacy: bool,
) -> Any:
    """Handle BaseException paths which ADK's normal plugin notification skips."""
    async def callback(*args: Any, **kwargs: Any) -> Any:
        """Unwind in the callback task without treating ordinary errors as cancellation."""
        try:
            result = await method(*args, **kwargs)
            if legacy:
                _require_safe_callback_result(name, result)
            return result
        except (asyncio.CancelledError, GeneratorExit):
            if legacy:
                await close_scope()
            raise
        except Exception:
            if name == "on_run_error_callback":
                # ADK stops error notification if an authored error hook fails;
                # do not depend on later cleanup callbacks being reached.
                await close_scope()
            raise

    return callback


def _require_safe_callback_result(name: str, result: Any) -> None:
    """Fail closed when legacy ADK skips the only cancellable execution scope."""
    from google.genai.types import Content
    from .eval_errors import LEGACY_SHORT_CIRCUIT_MESSAGE, LegacyEvalShortCircuitError

    if name == "before_run_callback" and isinstance(result, Content):
        raise LegacyEvalShortCircuitError(LEGACY_SHORT_CIRCUIT_MESSAGE)


def _guarded_stream(method: Any, close_scope: Callable[[], Awaitable[None]]) -> Any:
    """Leave successful and ordinary-error callbacks under native ADK ownership."""
    async def run(*args: Any, **kwargs: Any) -> Any:
        """Release in the iterator's own task before ContextVar tokens can escape."""
        events = method(*args, **kwargs)
        try:
            async for event in events:
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            try:
                await events.aclose()
            finally:
                await close_scope()
            raise
        finally:
            await events.aclose()

    return run

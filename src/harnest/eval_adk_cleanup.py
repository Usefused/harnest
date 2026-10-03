"""Close evaluator invocation scopes independently of ADK cancellation callbacks."""

from functools import wraps
from threading import Lock
from typing import Any, Awaitable, Callable

_RUNNER_GUARD_LOCK = Lock()


def guarded_eval_app(app: Any, cleanup_factory: Callable[[], Callable[[], Awaitable[None]]]) -> Any:
    """Opt only the evaluator's copied App into owner-task stream cleanup."""
    from google.adk.runners import Runner

    # ADK constructs its own Runner inside evaluation. Install one dispatch shim
    # rather than temporarily patching globals across overlapping evaluations.
    # Unmarked Apps return the original native iterator unchanged. A marker on
    # the App survives ADK's per-case model_copy, without retaining a registry.
    with _RUNNER_GUARD_LOCK:
        for name in ("run_async", "run_live"):
            method = getattr(Runner, name)
            if not getattr(method, "_harnest_eval_guard", False):
                setattr(Runner, name, _runner_stream(method))
    copied = app.model_copy()
    object.__setattr__(copied, "_harnest_eval_cleanup_factory", cleanup_factory)
    return copied


def _runner_stream(method: Any) -> Any:
    """Select cleanup by the runner's App, never by another task's active eval."""
    @wraps(method)
    def run(runner: Any, *args: Any, **kwargs: Any) -> Any:
        """Preserve native iterator ownership for every non-evaluation runner."""
        events = method(runner, *args, **kwargs)
        factory = getattr(runner.app, "_harnest_eval_cleanup_factory", None)
        return events if factory is None else _guarded_stream(events, factory)

    run._harnest_eval_guard = True
    return run


def _legacy_root(root: Any) -> bool:
    """Match ADK's current legacy runner branch without changing node execution."""
    from google.adk.agents import BaseAgent, LlmAgent

    return isinstance(root, BaseAgent) and not isinstance(root, LlmAgent)


def guarded_eval_plugins(
    root: Any, plugins: list[Any], close_scope: Callable[[], Awaitable[None]],
) -> list[Any]:
    """Borrow native plugins while preserving callback error and legacy policies."""
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
    """Keep a failed authored error callback from stranding internal cleanup."""
    async def callback(*args: Any, **kwargs: Any) -> Any:
        """Preserve native callbacks and unwind if their error notification fails."""
        try:
            result = await method(*args, **kwargs)
            if legacy:
                _require_safe_callback_result(name, result)
            return result
        except Exception:
            if name == "on_run_error_callback":
                # ADK stops error notification if an authored error hook fails;
                # do not depend on later cleanup callbacks being reached.
                await close_scope()
            raise

    return callback


def _require_safe_callback_result(name: str, result: Any) -> None:
    """Preserve the managed evaluator's policy against legacy early replacement."""
    from google.genai.types import Content
    from .eval_errors import LEGACY_SHORT_CIRCUIT_MESSAGE, LegacyEvalShortCircuitError

    if name == "before_run_callback" and isinstance(result, Content):
        raise LegacyEvalShortCircuitError(LEGACY_SHORT_CIRCUIT_MESSAGE)


async def _guarded_stream(events: Any, cleanup_factory: Callable[[], Callable[[], Awaitable[None]]]) -> Any:
    """Unwind native callbacks first, then release any scope they left behind."""
    # Iterators may be constructed outside the task that will consume them.
    close_scope = cleanup_factory()
    try:
        async for event in events:
            yield event
    finally:
        try:
            await events.aclose()
        finally:
            # ADK 2.11 skips after_run on cancellation for node and legacy
            # execution alike. Reset tokens here, before the owner task exits;
            # a child agent task or an async-generator finalizer cannot do it.
            await close_scope()

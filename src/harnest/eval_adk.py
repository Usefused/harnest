"""Managed capability ownership around ADK's evaluator-owned native runner."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from .application import CompiledApplication
from .runtime_contract import InvocationRequest, SessionConflictError
from .runtime_extensions import ExtensionRuntimeDriver
from .session import InMemorySessionStore


_EVAL_RUNTIME: ContextVar[ExtensionRuntimeDriver | None] = ContextVar(
    "harnest_adk_eval_runtime", default=None
)


def _extension_runtime(driver: Any) -> ExtensionRuntimeDriver:
    """Locate the capability owner through Harnest's internal wrapper chain."""
    seen: set[int] = set()
    while driver is not None and id(driver) not in seen:
        if isinstance(driver, ExtensionRuntimeDriver):
            return driver
        seen.add(id(driver))
        driver = getattr(driver, "_driver", None)
    raise RuntimeError("ADK evaluation requires a managed Harnest runtime")


@asynccontextmanager
async def adk_evaluation_runtime(application: Any, driver: Any = None):
    """Own CLI capabilities or borrow the playground's existing resource owner."""
    if not isinstance(application, CompiledApplication) or application.framework != "adk":
        yield
        return
    owned = driver is None
    try:
        if owned:
            from .runtime import _runtime_driver

            # ADK owns evaluation sessions. Never start the deployed session or
            # checkpoint backend, background workers, or schedules just to run
            # an eval: a worker could consume unrelated production task rows.
            isolated = replace(
                application, session_store=InMemorySessionStore(), checkpointer=None,
                tasks=(), crons=(),
            )
            driver = _runtime_driver(isolated, enable_cron=False)
        from .runtime_pipeline import start_runtime_pipeline

        await start_runtime_pipeline(driver)
        token = _EVAL_RUNTIME.set(_extension_runtime(driver))
        try:
            yield
        finally:
            _EVAL_RUNTIME.reset(token)
    finally:
        if owned:
            await _close_evaluation_runtime(driver, application.target, sys.exc_info()[1])


async def _close_evaluation_runtime(driver: Any, target: Any, primary: BaseException | None) -> None:
    """Retain the execution failure while cleaning each owned model exactly once."""
    if driver is None:
        from .eval_model_transport import close_owned_eval_model_transports

        # Construction may fail before a runtime can adopt model ownership.
        await close_owned_eval_model_transports(target, primary_error=primary)
        return
    try:
        await driver.close()
    except BaseException as error:
        if primary is None:
            raise
        from ._exception_notes import add_exception_note

        add_exception_note(primary, f"ADK evaluation cleanup also failed ({type(error).__name__}).")


def evaluation_context_plugins() -> tuple[Any, Any]:
    """Bracket native callbacks with the active evaluation's capability scope."""
    from google.adk.plugins import BasePlugin

    scopes: ContextVar[tuple[Any, ...]] = ContextVar("harnest_eval_scopes", default=())
    sessions = InMemorySessionStore()

    class Enter(BasePlugin):
        """Bind invocation resources before authored or identity callbacks run."""

        def __init__(self):
            """Reserve an internal plugin name independently of authored plugins."""
            super().__init__(name="_harnest_eval_context_enter")

        async def before_run_callback(self, *, invocation_context: Any) -> None:
            """Use evaluator identity and isolated session data, never live sessions."""
            runtime = _EVAL_RUNTIME.get()
            if runtime is None:
                return
            native = invocation_context
            request = InvocationRequest(
                input=native.user_content, user_id=native.session.user_id,
                session_id=native.session.id, invocation_id=native.invocation_id,
                metadata={"harnest.eval.framework": "adk"}, state_delta={}, transport="eval",
            )
            try:
                await sessions.create(
                    session_id=request.session_id, user_id=request.user_id,
                    state=dict(native.session.state),
                )
            except SessionConflictError:
                pass  # Later turns share application data within this eval session.
            scope = runtime.native_invocation_context(request, session_store=sessions)
            await scope.__aenter__()
            scopes.set((*scopes.get(), (scope, asyncio.current_task())))

    class Exit(BasePlugin):
        """Release evaluation-owned context after all native callbacks finish."""

        def __init__(self):
            """Keep cleanup last without relying on authored plugin names."""
            super().__init__(name="_harnest_eval_context_exit")

        async def _close(self) -> None:
            """Reset tokens only in their owner task; revoke retained capabilities."""
            pending = scopes.get()
            if not pending:
                return
            scope, owner = pending[-1]
            if owner is not asyncio.current_task():
                raise RuntimeError("ADK evaluation context exited from a different task")
            scopes.set(pending[:-1])
            await scope.__aexit__(None, None, None)

        async def after_run_callback(self, *, invocation_context: Any) -> None:
            """Clean up successful runs and native early-stop/cancellation paths."""
            await self._close()

        async def on_run_error_callback(self, *, invocation_context: Any, error: Exception) -> None:
            """Clean up failures without returning a replacement model response."""
            await self._close()

    return Enter(), Exit()

"""Managed capability ownership around ADK's evaluator-owned native runner."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import asyncio
import importlib
import sys
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import ModuleType
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...bundle import EvalSuite

from ...application import CompiledApplication
from ...runtime_contract import InvocationRequest, SessionConflictError
from ...lifecycle_runtime import LifecycleRuntimeDriver
from ...session import InMemorySessionStore


_EVAL_RUNTIME: ContextVar[LifecycleRuntimeDriver | None] = ContextVar(
    "harnest_adk_eval_runtime", default=None
)


def _extension_runtime(driver: Any) -> LifecycleRuntimeDriver:
    """Locate the capability owner through Harnest's internal wrapper chain."""
    seen: set[int] = set()
    while driver is not None and id(driver) not in seen:
        if isinstance(driver, LifecycleRuntimeDriver):
            return driver
        seen.add(id(driver))
        driver = getattr(driver, "_driver", None)
    raise RuntimeError("ADK evaluation requires a managed Harnest runtime")


@asynccontextmanager
async def adk_evaluation_runtime(application: Any, driver: Any = None) -> AsyncIterator[None]:
    """Own CLI capabilities or borrow the playground's existing resource owner."""
    if not isinstance(application, CompiledApplication) or application.framework != "adk":
        yield
        return
    owned = driver is None
    try:
        if owned:
            from ...runtime import _runtime_driver

            # ADK owns evaluation sessions. Never start the deployed session or
            # checkpoint backend, background workers, or schedules just to run
            # an eval: a worker could consume unrelated production task rows.
            isolated = replace(
                application, session_store=InMemorySessionStore(), checkpointer=None,
                tasks=(), crons=(),
            )
            driver = _runtime_driver(isolated, enable_cron=False)
        from ...runtime_pipeline import start_runtime_pipeline

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
        from ...eval_model_transport import close_owned_eval_model_transports

        # Construction may fail before a runtime can adopt model ownership.
        await close_owned_eval_model_transports(target, primary_error=primary)
        return
    try:
        await driver.close()
    except BaseException as error:
        if primary is None:
            raise
        from ..._exception_notes import add_exception_note

        add_exception_note(primary, f"ADK evaluation cleanup also failed ({type(error).__name__}).")


def evaluation_context_plugins() -> tuple[Any, Any]:
    """Bracket native callbacks with the active evaluation's capability scope."""
    from google.adk.plugins import BasePlugin

    scopes: ContextVar[tuple[Any, ...]] = ContextVar("harnest_eval_scopes", default=())
    sessions = InMemorySessionStore()

    class Enter(BasePlugin):
        """Bind invocation resources before authored or identity callbacks run."""

        def __init__(self) -> None:
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
            scope = runtime.framework_invocation_context(request, session_store=sessions)
            await scope.__aenter__()
            scopes.set((*scopes.get(), (scope, asyncio.current_task())))

    class Exit(BasePlugin):
        """Release evaluation-owned context after all native callbacks finish."""

        def __init__(self) -> None:
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

        def _depth(self) -> int:
            """Identify scopes opened by a stream without taking ownership of its parent."""
            return len(scopes.get())

        async def after_run_callback(self, *, invocation_context: Any) -> None:
            """Clean up successful runs and native early-stop/cancellation paths."""
            await self._close()

        async def on_run_error_callback(self, *, invocation_context: Any, error: Exception) -> None:
            """Clean up failures without returning a replacement model response."""
            await self._close()

    return Enter(), Exit()


@contextmanager
def adk_eval_agent_module(application: Any) -> Iterator[str]:
    """Expose isolated native plugins with managed identity and error reporting."""

    package_name = f"_harnest_adk_eval_{uuid.uuid4().hex}"
    module_name = f"{package_name}.agent"
    package = ModuleType(package_name)
    package.__path__ = []
    module = ModuleType(module_name)
    module.root_agent = application.target
    sys.modules[package_name] = package
    sys.modules[module_name] = module
    try:
        with prepared_adk_eval_app(application.framework_app) as app:
            module.app = app
            module.root_agent = app.root_agent if app is not None else application.target
            yield module_name
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(package_name, None)


def run_builtin_evals(
    artifact: Path,
    suite: EvalSuite,
    *,
    trajectory: str = "business",
    print_results: bool = True,
    result_output: Path | None = None,
) -> int:
    """Run validated ADK eval sets with visible setup and suite milestones."""

    from ...testing import _run_eval_suite

    module_name = f"{artifact.name}.agent"
    return _run_eval_suite(
        "adk",
        suite,
        lambda: _adk_eval_target(module_name),
        trajectory=trajectory,
        print_results=print_results,
        result_output=result_output,
        scope=lambda: _artifact_import_scope(artifact, module_name),
    )


@asynccontextmanager
async def _adk_eval_target(module_name: str) -> AsyncIterator[tuple[str, Any]]:
    """Keep native evaluation capabilities and model clients in one loop."""

    from ...eval_model_transport import close_owned_eval_model_transports

    module = sys.modules.get(module_name)
    target = getattr(module, "root_agent", None)
    application = getattr(module, "application", None)
    try:
        async with adk_evaluation_runtime(application):
            yield module_name, target
    finally:
        # A real compiled application's runtime owns model shutdown. The
        # bare-module compatibility path has no such owner; close only it
        # here so custom clients never receive duplicate close calls.
        if not isinstance(application, CompiledApplication):
            await close_owned_eval_model_transports(target, primary_error=sys.exc_info()[1])


@contextmanager
def _artifact_import_scope(artifact: Path, module_name: str) -> Iterator[None]:
    """Import the compiled artifact for ADK's evaluator, then forget it."""

    import_root = str(artifact.parent)
    sys.path.insert(0, import_root)
    try:
        with _adk_eval_output_filter(module_name):
            yield
    finally:
        if sys.path and sys.path[0] == import_root:
            sys.path.pop(0)
        else:  # pragma: no cover - defensive against third-party sys.path edits
            try:
                sys.path.remove(import_root)
            except ValueError:
                pass
        for name in tuple(sys.modules):
            if name == artifact.name or name.startswith(f"{artifact.name}."):
                sys.modules.pop(name, None)


@contextmanager
def prepared_adk_eval_app(app: Any) -> Iterator[Any]:
    """Share CLI/playground context setup without mutating the live native app."""
    if app is None:
        yield None
        return
    from .context import adk_agent_context_plugins
    from .evaluation import evaluation_context_plugins
    from .evaluation_cleanup import guarded_eval_plugins, guarded_eval_app
    from ...eval_errors import evaluation_error_boundary, evaluation_error_plugin

    enter, exit_plugin = adk_agent_context_plugins(app.root_agent.name)
    eval_enter, eval_exit = evaluation_context_plugins()
    observer = evaluation_error_plugin()

    async def close_scope() -> None:
        """Unwind identity before capabilities when native ADK skips callbacks."""
        try:
            await exit_plugin.after_run_callback(invocation_context=None)
        finally:
            await eval_exit._close()

    def cleanup_factory() -> Callable[[], Awaitable[None]]:
        """Bound fallback cleanup to scopes opened by this particular stream."""
        depth = eval_exit._depth()

        async def close_pending() -> None:
            """Leave inherited/nested scopes intact if native callbacks already unwound."""
            if eval_exit._depth() > depth:
                await close_scope()

        return close_pending
    # Capability setup precedes authored callbacks; identity and resource
    # cleanup run last so after/error callbacks can still use session identity.
    plugins = [
        customer_facing_eval_output_plugin(), eval_enter, enter, observer,
        *list(app.plugins), exit_plugin, eval_exit,
    ]
    with evaluation_error_boundary(observer):
        plugins = guarded_eval_plugins(app.root_agent, plugins, close_scope)
        yield guarded_eval_app(app.model_copy(update={"plugins": plugins}), cleanup_factory)


@contextmanager
def _adk_eval_output_filter(module_name: str) -> Iterator[None]:
    """Prepare managed native evaluation without modifying the compiled app."""


    agent_module = importlib.import_module(module_name)
    app = getattr(agent_module, "app", None)
    plugins = getattr(app, "plugins", None)
    if not isinstance(plugins, list):
        from ...testing import AgentTestError

        raise AgentTestError("compiled ADK eval application has no plugin list")
    missing = object()
    original_root = getattr(agent_module, "root_agent", missing)
    with prepared_adk_eval_app(app) as prepared:
        agent_module.app = prepared
        # ADK's evaluator rebuilds App.root_agent from the module export, so
        # both must point at the isolated legacy cancellation-safe root.
        agent_module.root_agent = prepared.root_agent
        try:
            yield
        finally:
            agent_module.app = app
            if original_root is missing:
                del agent_module.root_agent
            else:
                agent_module.root_agent = original_root


def customer_facing_eval_output_plugin() -> Any:
    """Remove private response parts before ADK metrics inspect model output."""

    from google.adk.plugins import BasePlugin
    from ...runtime_contract import _customer_facing_parts

    class CustomerFacingEvalOutputPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="_harnest_customer_facing_eval_output")

        async def on_event_callback(
            self, *, invocation_context: Any, event: Any
        ) -> None:
            """Mutate evaluator-owned events without exposing hidden parts."""

            del invocation_context
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None)
            if parts is None:
                return
            visible_parts = list(_customer_facing_parts(parts))
            if len(visible_parts) != len(parts):
                content.parts = visible_parts

    return CustomerFacingEvalOutputPlugin()

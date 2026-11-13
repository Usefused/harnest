"""Serve advanced ADK applications through ADK's own FastAPI server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import inspect
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from ... import runtime as _runtime
from ...runtime import AgentRuntimeError
from ...runtime_auth import Authenticator, install_authentication
from ..base import RuntimeSessions


def _create_adk_fastapi_app(
    artifact: str | Path,
    *,
    application: Any,
    bind_host: str,
    session_service: Any,
    manage_credential_provider: bool = True,
) -> Any:
    """Create ADK's official app without inspecting private route closures."""

    try:
        from google.adk.cli.fast_api import get_fast_api_app
        from google.adk.cli.utils.base_agent_loader import BaseAgentLoader
    except ImportError as exc:
        raise AgentRuntimeError(
            "Google ADK's FastAPI server dependencies are required"
        ) from exc

    compiled_app = application.framework_app
    if compiled_app is None:
        raise AgentRuntimeError("compiled ADK application does not contain an App")
    card = _runtime.load_agent_card(artifact)
    app_name = compiled_app.root_agent.name

    class CompiledAgentLoader(BaseAgentLoader):
        def load_agent(self, agent_name: str) -> Any:
            if agent_name != app_name:
                raise ValueError(f"unknown compiled agent: {agent_name}")
            return compiled_app

        def list_agents(self) -> list[str]:
            return [app_name]

        def list_agents_detailed(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": app_name,
                    "display_name": card.get("name"),
                    "description": getattr(compiled_app.root_agent, "description", ""),
                    "type": type(compiled_app.root_agent).__name__,
                }
            ]

    runtime_root = Path(tempfile.mkdtemp(prefix="harnest-adk-state-"))
    agents_dir = runtime_root / "adk_agents"
    (agents_dir / app_name).mkdir(parents=True)

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        """Keep ADK host state inside the final runtime pipeline lifetime."""

        from ...lifecycle_runtime import (
            _close_credential_provider,
            _start_credential_provider,
        )

        provider = application.credential_provider
        provider_started = False
        try:
            # Direct construction has no outer Harnest pipeline. Production
            # serving disables this owner so the composed pipeline starts the
            # same provider between plugins and extension resources.
            if manage_credential_provider and provider is not None:
                await _start_credential_provider(provider)
                provider_started = True
            yield
        finally:
            try:
                if provider_started:
                    await _close_credential_provider(provider)
            finally:
                shutil.rmtree(runtime_root, ignore_errors=True)

    kwargs: dict[str, Any] = {
        "agents_dir": str(agents_dir),
        "agent_loader": CompiledAgentLoader(),
        "artifact_service_uri": "memory://",
        "memory_service_uri": "memory://",
        "use_local_storage": False,
        "web": False,
        "a2a": False,
        "host": bind_host,
        "lifespan": lifespan,
        "reload_agents": False,
        "auto_create_session": False,
    }
    if "bind_host" in inspect.signature(get_fast_api_app).parameters:
        kwargs["bind_host"] = bind_host
    credential_service = None
    if application.credential_provider is not None:
        from .credentials import adk_credential_service

        credential_service = adk_credential_service(application.credential_provider)
    try:
        # Without a provider, retain ADK's in-memory warning because that
        # credential durability choice remains user-actionable.
        if session_service is None:
            return _build_adk_fastapi_app(
                get_fast_api_app,
                session_service_uri="memory://",
                credential_service=credential_service,
                kwargs=kwargs,
            )
        from .sessions import register_adk_session_service

        with register_adk_session_service(session_service) as service_uri:
            return _build_adk_fastapi_app(
                get_fast_api_app,
                session_service_uri=service_uri,
                credential_service=credential_service,
                kwargs=kwargs,
            )
    except Exception:
        shutil.rmtree(runtime_root, ignore_errors=True)
        raise

def _build_adk_fastapi_app(
    factory: Any,
    *,
    session_service_uri: str,
    credential_service: Any,
    kwargs: Mapping[str, Any],
) -> Any:
    """Use ADK's supported credential seam or its public server constructor."""

    if credential_service is None:
        return factory(session_service_uri=session_service_uri, **kwargs)
    if "credential_service" in inspect.signature(factory).parameters:
        return factory(
            session_service_uri=session_service_uri,
            credential_service=credential_service,
            **kwargs,
        )
    # ADK 2.8's convenience factory hardcodes in-memory credentials. ApiServer
    # exposes the service explicitly, so compose the equivalent production-safe
    # surface until the convenience factory gains that parameter.
    return _build_adk_api_server(
        session_service_uri=session_service_uri,
        credential_service=credential_service,
        kwargs=kwargs,
    )

def _build_adk_api_server(
    *,
    session_service_uri: str,
    credential_service: Any,
    kwargs: Mapping[str, Any],
) -> Any:
    """Construct ADK's public ApiServer with non-persisting credentials."""

    from google.adk.cli import fast_api as adk_fast_api

    agents_dir = str(kwargs["agents_dir"])
    loader = kwargs["agent_loader"]
    loader._allow_special_agents = False
    adk_fast_api.load_services_module(agents_dir)
    memory_service = adk_fast_api.create_memory_service_from_options(
        base_dir=agents_dir,
        memory_service_uri=str(kwargs["memory_service_uri"]),
    )
    session_service = adk_fast_api.create_session_service_from_options(
        base_dir=agents_dir,
        session_service_uri=session_service_uri,
        session_db_kwargs=None,
        use_local_storage=False,
    )
    artifact_service = adk_fast_api.create_artifact_service_from_options(
        base_dir=agents_dir,
        artifact_service_uri=str(kwargs["artifact_service_uri"]),
        strict_uri=True,
        use_local_storage=False,
    )
    server = adk_fast_api.ApiServer(
        agent_loader=loader,
        session_service=session_service,
        memory_service=memory_service,
        artifact_service=artifact_service,
        credential_service=credential_service,
        eval_sets_manager=adk_fast_api.LocalEvalSetsManager(agents_dir=agents_dir),
        eval_set_results_manager=adk_fast_api.LocalEvalSetResultsManager(
            agents_dir=agents_dir
        ),
        agents_dir=agents_dir,
        auto_create_session=False,
    )
    server.default_app_name = loader.list_agents()[0]
    app = server.get_fast_api_app(
        lifespan=kwargs["lifespan"],
        bind_host=kwargs.get("bind_host"),
    )
    adk_fast_api.maybe_install_request_metrics_middleware(
        app, otel_to_cloud=False
    )
    return app

def _build_native_adk_app(
    artifact: str | Path,
    *,
    application: Any,
    bind_host: str,
    card: Mapping[str, Any],
    sessions: RuntimeSessions,
    request_timeout: float,
    max_concurrency: int,
    max_request_bytes: int,
    playground_enabled: bool,
    openapi_enabled: bool,
    live_enabled: bool,
    agui_enabled: bool,
    agent_principal_required: bool,
    authenticator: Authenticator | None,
    telemetry_exporter_factories: Any,
) -> tuple[Any, Any]:
    """Apply neutral transport and API-documentation policy to native ADK routes."""

    from ...neutral_runtime import create_neutral_router
    from ...runtime_openapi import configure_openapi
    from ...http_lifecycle import install_http_lifecycle
    from ...playground import create_playground_router
    from ...playground_studio import PlaygroundStudioService
    from ...server_limits import install_request_size_limit
    from ...server_transports import install_live_policy
    from ...telemetry import configure_observability

    os.environ.setdefault("OTEL_SERVICE_NAME", application.name)
    app = _create_adk_fastapi_app(
        artifact,
        application=application,
        bind_host=bind_host,
        session_service=sessions.framework_service,
        manage_credential_provider=False,
    )
    driver = _runtime._runtime_driver(
        application,
        card=card,
        extra_endpoints={"adkRun": "/run", "adkRunSse": "/run_sse"},
        sessions=sessions,
    )
    pipeline_driver = driver
    from ..._eval_capture_runtime import with_eval_capture

    # Capture wraps the request path only; evals and startup keep the raw
    # driver so eval runs are never captured as production traffic.
    driver = with_eval_capture(driver, application)
    trace_store = None
    if playground_enabled:
        from ...playground_trace import (
            PlaygroundTraceRuntimeDriver,
            PlaygroundTraceStore,
        )

        trace_store = PlaygroundTraceStore()
        driver = PlaygroundTraceRuntimeDriver(driver, trace_store)
        from ...playground_connectors import PlaygroundConnectorsService
        from ...playground_eval import PlaygroundEvalService
        from ...playground_mcp import PlaygroundMCPService

        eval_service = PlaygroundEvalService(
            artifact,
            application,
            pipeline_driver,
        )
        app.router.routes.extend(
            create_playground_router(
                trace_store, eval_service, openapi_enabled=openapi_enabled,
                mcp_service=PlaygroundMCPService(Path(artifact) / "source", application.framework),
                studio_service=PlaygroundStudioService(Path(artifact) / "source", mode=application.mode, framework=application.framework),
                connectors_service=PlaygroundConnectorsService(),
                work_driver=pipeline_driver,
            ).routes
        )
    neutral = create_neutral_router(
        driver,
        request_timeout=request_timeout,
        max_concurrency=max_concurrency,
        max_request_bytes=max_request_bytes,
        live_enabled=live_enabled,
        agui_enabled=agui_enabled,
        agent_principal_required=agent_principal_required,
        asset_store=application.asset_store,
        asset_stores=application.asset_stores,
        a2a_task_store=_runtime._a2a_task_store(application),
        http_routes=application.http_routes,
        lifecycle_extensions=application.lifecycle_extensions,
    )
    # ADK owns a flat public route table, so preserve it when adding neutral APIs.
    app.router.routes.extend(neutral.routes)
    _runtime._attach_driver_lifecycle(app, driver, startup_driver=pipeline_driver)
    install_http_lifecycle(app, application.lifecycle_extensions)
    install_authentication(app, authenticator)
    install_request_size_limit(app, max_request_bytes)
    install_live_policy(app, live_enabled)
    configure_openapi(app, enabled=openapi_enabled)
    telemetry = configure_observability(
        application.name,
        framework=application.framework,
        use_global_providers=_adk_owns_otel(),
        exporter_factories=telemetry_exporter_factories,
    )
    return app, telemetry

def _adk_owns_otel() -> bool:
    names = (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    )
    return any(os.getenv(name, "").strip() for name in names)

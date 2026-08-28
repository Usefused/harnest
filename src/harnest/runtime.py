"""Process bootstrap for standalone compiled Harnest artifacts.

The public wire protocol lives once in ``neutral_runtime``. Framework-specific
session and event translation lives in the two runtime driver modules.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping
import uuid

from ._library import (
    LibraryConventionError,
    LibraryImportError,
    activate_authored_library,
    release_authored_library,
)
from .runtime_auth import Authenticator, install_authentication
from .session import ADKSessionStorage, SessionStore
from .server_config import (
    DEFAULT_SERVER_CONFIG,
    SERVER_CONFIG_FILENAME,
    ServerConfigError,
    load_server_config,
    validate_max_request_bytes,
)

_MAX_CARD_BYTES = 4 * 1024 * 1024
_DIRECT_USER_ID = "_harnest_direct"


class AgentRuntimeError(RuntimeError):
    """A compiled artifact cannot be loaded or served safely."""


class AgentExecutionTimeout(RuntimeError):
    """An invocation exceeded its configured deadline."""


def _apply_observability_defaults(framework: str) -> None:
    if framework == "adk":
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT"
        )
        os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")


def load_compiled_application(artifact: str | Path) -> Any:
    """Load the portable application and retain its library for lazy imports."""

    directory = Path(artifact).resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise AgentRuntimeError(f"compiled artifact directory is invalid: {directory}")
    entrypoint = directory / "agent.py"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise AgentRuntimeError(f"compiled artifact is missing agent.py: {directory}")
    source_root = directory / "source"
    try:
        library_acquired = activate_authored_library(source_root)
    except (LibraryConventionError, LibraryImportError) as exc:
        raise AgentRuntimeError(f"invalid authored library: {exc}") from exc
    try:
        return _import_compiled_application(entrypoint)
    except Exception:
        if library_acquired:
            release_authored_library(source_root)
        raise


def _import_compiled_application(entrypoint: Path) -> Any:
    """Import an artifact entrypoint after its authored namespace is ready."""

    module_name = f"_harnest_compiled_agent_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, entrypoint)
    if spec is None or spec.loader is None:
        raise AgentRuntimeError(f"cannot load compiled agent from {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise AgentRuntimeError(
            f"failed to load compiled agent: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        sys.modules.pop(spec.name, None)

    from .application import CompiledApplication

    application = getattr(module, "application", None)
    if not isinstance(application, CompiledApplication):
        raise AgentRuntimeError(
            "compiled agent.py must export a CompiledApplication named application"
        )
    return application


def load_compiled_app(artifact: str | Path) -> Any:
    """Load the native ADK App from an ADK compiled artifact."""

    application = load_compiled_application(artifact)
    _apply_observability_defaults(application.framework)
    if application.framework != "adk" or application.native_app is None:
        release_authored_library(Path(artifact).resolve() / "source")
        raise AgentRuntimeError("compiled application does not contain an ADK App")
    return application.native_app


def load_compiled_agent(artifact: str | Path) -> Any:
    """Load the selected framework's managed or advanced target."""

    return load_compiled_application(artifact).target


def load_agent_card(artifact: str | Path) -> dict[str, Any]:
    """Load the compiled YAML or JSON Agent Card."""

    path = Path(artifact).resolve() / "source" / "agent-card.yaml"
    if path.is_symlink() or not path.is_file():
        raise AgentRuntimeError(f"compiled artifact is missing agent-card.yaml: {path}")
    try:
        if path.stat().st_size > _MAX_CARD_BYTES:
            raise AgentRuntimeError("agent card exceeds the 4 MiB limit")
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentRuntimeError(f"unable to read agent card: {exc}") from exc
    try:
        import yaml

        value = yaml.safe_load(contents)
    except ImportError:
        try:
            value = json.loads(contents)
        except ValueError as exc:
            raise AgentRuntimeError(
                "PyYAML is required to serve a YAML agent card"
            ) from exc
    except Exception as exc:
        raise AgentRuntimeError(f"invalid agent card YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentRuntimeError("agent card must contain a JSON object")
    return value


def _runtime_driver(
    application: Any,
    *,
    card: Mapping[str, Any] | None = None,
    extra_endpoints: Mapping[str, str] | None = None,
    adk_session_service: Any | None = None,
    langgraph_session_store: SessionStore | None = None,
) -> Any:
    """Select a backend once, at the process boundary."""

    if application.framework == "adk":
        driver = _adk_runtime_driver(
            application,
            card=card,
            extra_endpoints=extra_endpoints,
            session_service=adk_session_service,
        )
    elif application.framework == "langgraph":
        driver = _langgraph_runtime_driver(
            application,
            card=card,
            session_store=langgraph_session_store,
        )
    else:
        raise AgentRuntimeError(f"unsupported framework: {application.framework}")
    return _wrap_runtime_driver(application, driver)


def _adk_runtime_driver(
    application: Any,
    *,
    card: Mapping[str, Any] | None,
    extra_endpoints: Mapping[str, str] | None,
    session_service: Any | None,
) -> Any:
    """Give one ADK service sole ownership of native session persistence."""

    from .checkpoint import ADKStore
    from .runtime_adk import ADKRuntimeDriver

    provider = application.checkpointer
    if isinstance(provider, ADKStore):
        if session_service is not None:
            raise AgentRuntimeError(
                "ADKStore cannot be combined with an injected ADK session service"
            )
        session_service = provider.session_service
    if application.session_store is not None and session_service is None:
        from .session_adk import create_adk_session_service

        session_service = create_adk_session_service(application.session_store)
    return ADKRuntimeDriver(
        application,
        card=card,
        extra_endpoints=extra_endpoints,
        session_service=session_service,
    )


def _langgraph_runtime_driver(
    application: Any,
    *,
    card: Mapping[str, Any] | None,
    session_store: SessionStore | None,
) -> Any:
    """Resolve compiled versus host-injected LangGraph session ownership."""

    from .runtime_langgraph import LangGraphRuntimeDriver

    lifecycle_store = application.session_store
    if lifecycle_store is not None and session_store is not None:
        raise AgentRuntimeError(
            "@lifecycle.session_store cannot be combined with an injected "
            "LangGraph session store"
        )
    return LangGraphRuntimeDriver(
        application,
        card=card,
        session_store=(
            lifecycle_store if lifecycle_store is not None else session_store
        ),
    )


def _wrap_runtime_driver(application: Any, driver: Any) -> Any:
    """Place storage lifecycle outside framework execution and inside extensions."""

    if application.session_store is not None or application.checkpointer is not None:
        from .runtime_session import StorageRuntimeDriver

        driver = StorageRuntimeDriver(
            driver,
            application.session_store,
            application.checkpointer,
        )
    if application.extensions or application.context_values:
        from .runtime_extensions import ExtensionRuntimeDriver

        return ExtensionRuntimeDriver(
            driver,
            application.extensions,
            context_values=application.context_values,
        )
    return driver


def _direct_result(result: Any) -> dict[str, Any]:
    calls, responses = [], []
    for event in result.events:
        if event.get("type") == "tool_call":
            calls.append(
                {
                    "id": event.get("id"),
                    "name": event.get("name"),
                    "args": event.get("arguments"),
                }
            )
        elif event.get("type") == "tool_result":
            responses.append(
                {
                    "id": event.get("id", event.get("callId")),
                    "name": event.get("name"),
                    "response": event.get("result", event.get("output")),
                }
            )
    return {
        "text": result.text,
        "toolCalls": calls,
        "toolResponses": responses,
        "result": result.result,
    }


async def run_agent_message(
    artifact: str | Path,
    message: str,
    *,
    request_timeout: float = 300,
) -> dict[str, Any]:
    """Run one request through the same driver used by the HTTP server."""

    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    application = load_compiled_application(artifact)
    try:
        return await _run_agent_message(
            artifact,
            application,
            message,
            request_timeout=request_timeout,
        )
    finally:
        release_authored_library(Path(artifact).resolve() / "source")


async def _run_agent_message(
    artifact: str | Path,
    application: Any,
    message: str,
    *,
    request_timeout: float,
) -> dict[str, Any]:
    """Run direct execution while the caller owns the library lifecycle."""

    _apply_observability_defaults(application.framework)

    from .neutral_runtime import InvocationRequest, require_customer_facing_output
    from .telemetry import configure_observability

    configure_observability(application.name, framework=application.framework)
    try:
        card = load_agent_card(artifact)
    except AgentRuntimeError:
        # Direct execution does not expose discovery metadata over HTTP.
        card = {}
    driver = _runtime_driver(application, card=card)
    session_id = uuid.uuid4().hex
    await driver.create_session(
        session_id=session_id, user_id=_DIRECT_USER_ID, state={}
    )
    request = InvocationRequest(
        input=message,
        user_id=_DIRECT_USER_ID,
        session_id=session_id,
        invocation_id=f"direct_{uuid.uuid4().hex}",
        metadata={},
        state_delta={},
        transport="direct",
    )
    from .logging import get_logger
    from .tracing import span

    logger = get_logger(f"runtime.{application.framework}")
    try:
        with span(
            "harnest.agent.invoke",
            **{
                "harnest.agent.name": application.name,
                "harnest.framework": application.framework,
                "harnest.mode": application.mode,
                "harnest.transport": "direct",
                "harnest.streaming": False,
            },
        ):
            logger.info("agent.invocation.started", agent_name=application.name)
            try:
                result = await asyncio.wait_for(
                    driver.invoke(request), timeout=request_timeout
                )
                require_customer_facing_output(result.text, result.result)
            except asyncio.TimeoutError as exc:
                logger.error("agent.invocation.timeout", agent_name=application.name)
                raise AgentExecutionTimeout(
                    "agent execution exceeded its deadline"
                ) from exc
            except Exception:
                logger.exception("agent.invocation.failed", agent_name=application.name)
                raise
            logger.info("agent.invocation.completed", agent_name=application.name)
        return _direct_result(result)
    finally:
        await driver.close()


def _create_adk_fastapi_app(
    artifact: str | Path,
    *,
    application: Any,
    bind_host: str,
    session_service: Any,
) -> Any:
    """Create ADK's official app without inspecting private route closures."""

    try:
        from google.adk.cli.fast_api import get_fast_api_app
        from google.adk.cli.utils.base_agent_loader import BaseAgentLoader
    except ImportError as exc:
        raise AgentRuntimeError(
            "Google ADK's FastAPI server dependencies are required"
        ) from exc

    compiled_app = application.native_app
    if compiled_app is None:
        raise AgentRuntimeError("compiled ADK application does not contain an App")
    card = load_agent_card(artifact)
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
    async def lifespan(_app: Any):
        try:
            yield
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
    try:
        if session_service is None:
            return get_fast_api_app(session_service_uri="memory://", **kwargs)
        from .session_adk import register_adk_session_service

        with register_adk_session_service(session_service) as service_uri:
            return get_fast_api_app(session_service_uri=service_uri, **kwargs)
    except Exception:
        shutil.rmtree(runtime_root, ignore_errors=True)
        raise


def _exposes_native_adk_api(application: Any) -> bool:
    """Keep framework-owned HTTP contracts behind the advanced-mode boundary."""

    return application.framework == "adk" and application.mode == "advanced"


def create_fastapi_app(
    artifact: str | Path,
    *,
    bind_host: str = "127.0.0.1",
    request_timeout: float = 300,
    max_concurrency: int = 8,
    max_request_bytes: int = DEFAULT_SERVER_CONFIG.limits.max_request_bytes,
    playground_enabled: bool = True,
    adk_session_storage: ADKSessionStorage | None = None,
    langgraph_session_store: SessionStore | None = None,
    authenticator: Authenticator | None = None,
) -> Any:
    """Build the neutral server and opt advanced ADK agents into native routes."""

    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    if max_concurrency < 1:
        raise ValueError("max concurrency must be at least one")
    max_request_bytes = validate_max_request_bytes(max_request_bytes)
    if not isinstance(playground_enabled, bool):
        raise TypeError("playground_enabled must be boolean")
    application = load_compiled_application(artifact)
    try:
        return _build_fastapi_app(
            artifact,
            application=application,
            bind_host=bind_host,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            playground_enabled=playground_enabled,
            adk_session_storage=adk_session_storage,
            langgraph_session_store=langgraph_session_store,
            authenticator=authenticator,
        )
    except Exception:
        release_authored_library(Path(artifact).resolve() / "source")
        raise


def _build_fastapi_app(
    artifact: str | Path,
    *,
    application: Any,
    bind_host: str,
    request_timeout: float,
    max_concurrency: int,
    max_request_bytes: int,
    playground_enabled: bool,
    adk_session_storage: ADKSessionStorage | None,
    langgraph_session_store: SessionStore | None,
    authenticator: Authenticator | None,
) -> Any:
    """Construct the server after its authored library has been acquired."""

    _validate_session_storage_choice(
        application, adk_session_storage, langgraph_session_store
    )
    card = load_agent_card(artifact)
    _apply_observability_defaults(application.framework)
    authenticator = _resolve_lifecycle_authenticator(
        application.extensions, authenticator
    )

    from .neutral_runtime import create_neutral_app
    from .telemetry import configure_observability, instrument_fastapi

    adk_session_service = _runtime_adk_session_service(
        artifact,
        application,
        adk_session_storage,
        langgraph_session_store,
    )
    native_adk = _exposes_native_adk_api(application)
    if native_adk:
        app, telemetry = _build_native_adk_app(
            artifact,
            application=application,
            bind_host=bind_host,
            card=card,
            adk_session_service=adk_session_service,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            playground_enabled=playground_enabled,
            authenticator=authenticator,
        )
    else:
        telemetry = configure_observability(
            application.name, framework=application.framework
        )
        app = create_neutral_app(
            _runtime_driver(
                application,
                card=card,
                adk_session_service=adk_session_service,
                langgraph_session_store=langgraph_session_store,
            ),
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            playground_enabled=playground_enabled,
            authenticator=authenticator,
        )
    instrument_fastapi(app, telemetry)
    _attach_library_lifecycle(app, Path(artifact).resolve() / "source")
    return app


def _validate_session_storage_choice(
    application: Any,
    adk_storage: ADKSessionStorage | None,
    langgraph_store: SessionStore | None,
) -> None:
    """Reject host storage injection when the compiled agent owns that boundary."""

    if application.session_store is not None and (
        adk_storage is not None or langgraph_store is not None
    ):
        raise AgentRuntimeError(
            "@lifecycle.session_store cannot be combined with injected session storage"
        )


def _runtime_adk_session_service(
    artifact: str | Path,
    application: Any,
    adk_storage: ADKSessionStorage | None,
    langgraph_store: SessionStore | None,
) -> Any | None:
    """Select one ADK session authority before the native application is built."""

    from .checkpoint import ADKStore

    if isinstance(application.checkpointer, ADKStore):
        if adk_storage is not None:
            raise AgentRuntimeError(
                "ADKStore cannot be combined with deployment ADK session storage"
            )
        return application.checkpointer.session_service
    service = _resolve_adk_session_service(
        artifact, application, adk_storage, langgraph_store
    )
    if (
        application.framework != "adk"
        or service is not None
        or application.session_store is None
    ):
        return service
    from .session_adk import create_adk_session_service

    return create_adk_session_service(application.session_store)


def _build_native_adk_app(
    artifact: str | Path,
    *,
    application: Any,
    bind_host: str,
    card: Mapping[str, Any],
    adk_session_service: Any,
    request_timeout: float,
    max_concurrency: int,
    max_request_bytes: int,
    playground_enabled: bool,
    authenticator: Authenticator | None,
) -> tuple[Any, Any]:
    from .neutral_runtime import create_neutral_router
    from .playground import create_playground_router
    from .server_limits import install_request_size_limit
    from .telemetry import configure_observability

    os.environ.setdefault("OTEL_SERVICE_NAME", application.name)
    app = _create_adk_fastapi_app(
        artifact,
        application=application,
        bind_host=bind_host,
        session_service=adk_session_service,
    )
    driver = _runtime_driver(
        application,
        card=card,
        extra_endpoints={"adkRun": "/run", "adkRunSse": "/run_sse"},
        adk_session_service=adk_session_service,
    )
    trace_store = None
    if playground_enabled:
        from .playground_trace import (
            PlaygroundTraceRuntimeDriver,
            PlaygroundTraceStore,
        )

        trace_store = PlaygroundTraceStore()
        driver = PlaygroundTraceRuntimeDriver(driver, trace_store)
        app.router.routes.extend(create_playground_router(trace_store).routes)
    neutral = create_neutral_router(
        driver,
        request_timeout=request_timeout,
        max_concurrency=max_concurrency,
        max_request_bytes=max_request_bytes,
    )
    # ADK owns a flat public route table, so preserve it when adding neutral APIs.
    app.router.routes.extend(neutral.routes)
    app.router.add_event_handler("shutdown", driver.close)
    install_authentication(app, authenticator)
    install_request_size_limit(app, max_request_bytes)
    telemetry = configure_observability(
        application.name,
        framework=application.framework,
        use_global_providers=_adk_owns_otel(),
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


def _resolve_lifecycle_authenticator(
    listeners: Any, authenticator: Authenticator | None
) -> Authenticator | None:
    from .runtime_extensions import lifecycle_authenticator

    discovered = lifecycle_authenticator(listeners)
    if discovered is not None and authenticator is not None:
        raise AgentRuntimeError(
            "auth lifecycle listeners cannot be combined with an injected authenticator"
        )
    return discovered or authenticator


def _resolve_adk_session_service(
    artifact: str | Path,
    application: Any,
    storage: ADKSessionStorage | None,
    langgraph_store: SessionStore | None,
) -> Any | None:
    if application.framework == "adk":
        if langgraph_store is not None:
            raise AgentRuntimeError(
                "langgraph_session_store cannot be used with an ADK agent"
            )
        if storage is None:
            return None
        return storage.create_service(str(Path(artifact).resolve() / "source"))
    if storage is not None:
        raise AgentRuntimeError(
            "adk_session_storage cannot be used with a LangGraph agent"
        )
    return None


def _attach_library_lifecycle(app: Any, source_root: Path) -> None:
    """Keep lazy imports valid through shutdown, including custom lifespans."""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any):
        try:
            async with original_lifespan(application):
                yield
        finally:
            release_authored_library(source_root)

    # Wrapping the active lifespan works for neutral and native ADK apps;
    # shutdown event handlers are skipped by frameworks with custom lifespans.
    app.router.lifespan_context = lifespan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harnest-agent")
    parser.add_argument("--artifact", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--request-timeout", type=float, default=None)
    parser.add_argument("--max-concurrency", type=int, default=None)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        default=None,
        help="allow a non-loopback bind; this server has no built-in authentication",
    )
    args = parser.parse_args(argv)
    try:
        server = load_server_config(args.artifact / SERVER_CONFIG_FILENAME)
        server = server.with_overrides(
            host=args.host,
            port=args.port,
            request_timeout_seconds=args.request_timeout,
            max_concurrent_requests=args.max_concurrency,
            allow_remote=args.allow_remote,
        )
        http = server.http
        if not http.allow_remote and http.host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ServerConfigError(
                "non-loopback binds require http.allowRemote: true in server.yaml"
            )
        app = create_fastapi_app(
            args.artifact,
            bind_host=http.host,
            request_timeout=http.request_timeout_seconds,
            max_concurrency=http.max_concurrent_requests,
            max_request_bytes=server.limits.max_request_bytes,
            playground_enabled=server.playground.enabled,
        )
        import uvicorn
    except (
        AgentRuntimeError,
        ImportError,
        OSError,
        ServerConfigError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"harnest-agent: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=http.host,
        port=http.port,
        log_level="info",
        limit_concurrency=http.max_concurrent_requests,
        timeout_keep_alive=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

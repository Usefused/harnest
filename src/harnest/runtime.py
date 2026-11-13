"""Process bootstrap for standalone compiled Harnest artifacts.

The public wire protocol lives once in ``neutral_runtime``. Framework-specific
session and event translation lives in the two runtime driver modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping
import uuid

from ._library import (
    LibraryConventionError,
    LibraryImportError,
    activate_authored_library,
    release_authored_library,
)
from .agent_principal import (
    AgentRuntimePrincipal,
    resolve_nested_agent_principal,
    validate_agent_principal_runtime,
)
from .runtime_auth import Authenticator
from .runtime_contract import (
    AgentInfo, InvocationRequest, InvocationResult, NoCustomerFacingOutputError,
    ResponseRequest, RuntimeDriver, RuntimeEvent, SessionConflictError,
    SessionMessage, SessionRecord,
)
from .frameworks.base import (
    FrameworkConventionError,
    RuntimeSessions,
    UnknownFrameworkError,
)
from .frameworks.registry import get_framework
from .session import SessionStore
from .server_config import (
    DEFAULT_SERVER_CONFIG,
    HTTPServerConfig,
    SERVER_CONFIG_FILENAME,
    ServerConfigError,
    load_server_config,
    validate_max_request_bytes,
)

_MAX_CARD_BYTES = 4 * 1024 * 1024
_COMPILED_MANIFEST_FILENAME = "harnest-manifest.json"
_DIRECT_USER_ID = "_harnest_direct"


class AgentRuntimeError(RuntimeError):
    """A compiled artifact cannot be loaded or served safely."""


class AgentExecutionTimeout(RuntimeError):
    """An invocation exceeded its configured deadline."""


def _apply_observability_defaults(framework: str) -> None:
    """Apply the privacy-safe telemetry defaults the selected framework declares."""

    for name, value in get_framework(framework).observability_defaults().items():
        os.environ.setdefault(name, value)


def load_compiled_application(artifact: str | Path) -> Any:
    """Load the application and retain authored namespaces for lazy imports."""

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
        raise AgentRuntimeError(f"invalid authored Python namespace: {exc}") from exc
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


def load_framework_app(artifact: str | Path) -> Any:
    """Load the framework-specific application object from a compiled artifact."""

    application = load_compiled_application(artifact)
    _apply_observability_defaults(application.framework)
    if application.framework_app is None:
        _release_application_extensions(application)
        release_authored_library(Path(artifact).resolve() / "source")
        raise AgentRuntimeError("compiled application does not expose a framework-specific application")
    return application.framework_app


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
    sessions: RuntimeSessions | None = None,
    manage_credential_provider: bool = True,
    enable_cron: bool = True,
) -> Any:
    """Select a backend once and retain schedule ownership at the host boundary."""

    continuations = _external_continuation_runtime(application)
    extension_manager = _extension_runtime_manager(
        application, continuation_runtime=continuations
    )
    try:
        adapter = get_framework(application.framework)
    except UnknownFrameworkError as exc:
        raise AgentRuntimeError(f"unsupported framework: {application.framework}") from exc
    driver = adapter.create_runtime_driver(
        application,
        card=card,
        extra_endpoints=extra_endpoints,
        sessions=sessions if sessions is not None else RuntimeSessions(),
        extension_manager=extension_manager,
    )
    if application.mode == "managed" and application.kind == "agent":
        from .dynamic_agent_plugins import DynamicAgentPluginRuntimeDriver

        # The backend fork shares session authority while each immutable plugin
        # set receives its own provider target and MCP connection lifecycle.
        driver = DynamicAgentPluginRuntimeDriver(driver, application)
    pipeline = _wrap_runtime_driver(
        application,
        driver,
        manage_credential_provider=manage_credential_provider,
        extension_manager=extension_manager,
    )
    if continuations is not None:
        from .external_continuation_driver import ExternalContinuationRuntimeDriver

        pipeline = ExternalContinuationRuntimeDriver(pipeline, continuations)
    if application.tasks:
        from .runtime_task import TaskRuntimeDriver

        pipeline = TaskRuntimeDriver(
            pipeline,
            _task_runtime_manager(
                application,
                extension_manager=extension_manager,
                continuation_runtime=continuations,
                enable_cron=enable_cron,
            ),
        )
    if continuations is not None:
        # Bind the final wrapper so callback-driven resumes receive the same
        # context, extension, storage, and task capabilities as user invocations.
        continuations.bind_driver(pipeline)
    return pipeline


def _task_runtime_manager(application: Any, **options: Any) -> Any:
    """Require registered storage; workers never select an implicit backend."""

    from .runtime_task_store import ProviderTaskRuntimeManager

    # The inner storage lifecycle owns the shared provider. The task wrapper
    # only owns workers, which must stop before that lifecycle closes pools.
    return ProviderTaskRuntimeManager(application, manage_storage=False, **options)


def _extension_runtime_manager(
    application: Any, *, continuation_runtime: Any | None = None
) -> Any | None:
    """Adopt the compiler's extension activation for one runtime pipeline."""

    if not application.extensions:
        return None
    from .extension_runtime_manager import ExtensionRuntimeManager

    return ExtensionRuntimeManager(
        application.extensions,
        framework=application.framework,
        root_agent_name=application.name,
        custom_stores=application.runtime_capabilities.custom_stores,
        continuation_runtime=continuation_runtime,
        # The compiled application transfers one namespace acquisition to the
        # process runtime; closing this manager consumes exactly that ownership.
        release_namespaces_on_close=True,
    )


def _external_continuation_runtime(application: Any) -> Any | None:
    """Attach durable response recovery whenever Harnest owns checkpoints."""

    extension_declared = any(
        "context.continuations" in item.descriptor.capabilities
        for item in application.extensions
    )
    from .checkpoint import HarnestStore

    store = application.runtime_capabilities.checkpointer
    if isinstance(store, HarnestStore):
        from .external_continuation import ExternalContinuationRuntime

        return ExternalContinuationRuntime(store, application_id=application.name)
    if extension_declared:
        # Native framework persistence cannot atomically own Harnest's portable
        # waiting state, so advanced mode fails closed for this capability.
        raise AgentRuntimeError(
            "extensions declaring context.continuations require a "
            "HarnestStore checkpoint provider"
        )
    return None


def _a2a_task_store(application: Any) -> Any | None:
    """Use the application checkpointer for A2A snapshots when it supports them."""

    from .checkpoint import A2ATaskPersistence

    persistence = application.runtime_capabilities.checkpointer
    if not isinstance(persistence, A2ATaskPersistence):
        return None
    from .runtime_a2a_store import HarnestA2ATaskStore

    # The adapter adds protocol serialization only; lifecycle and connections
    # remain owned by the already-registered Harnest storage resource.
    return HarnestA2ATaskStore(
        persistence,
        application_id=application.name,
    )


def _wrap_runtime_driver(
    application: Any,
    driver: Any,
    *,
    manage_credential_provider: bool = True,
    extension_manager: Any | None = None,
) -> Any:
    """Delegate all capability wrapper ordering to the runtime pipeline."""

    from .runtime_pipeline import build_runtime_pipeline

    return build_runtime_pipeline(
        driver,
        application.runtime_capabilities,
        application.lifecycle_extensions,
        manage_credential_provider=manage_credential_provider,
        extension_manager=extension_manager,
    )


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
    agent_principal: AgentRuntimePrincipal | None = None,
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
            agent_principal=resolve_nested_agent_principal(agent_principal),
        )
    finally:
        release_authored_library(Path(artifact).resolve() / "source")


async def _run_agent_message(
    artifact: str | Path,
    application: Any,
    message: str,
    *,
    request_timeout: float,
    agent_principal: AgentRuntimePrincipal | None,
) -> dict[str, Any]:
    """Run direct execution while the caller owns the library lifecycle."""

    _apply_observability_defaults(application.framework)

    from .runtime_contract import InvocationRequest, require_customer_facing_output
    from .telemetry import configure_observability

    driver = None
    telemetry = None
    try:
        telemetry = configure_observability(
            application.name,
            framework=application.framework,
            exporter_factories=application.telemetry_exporters,
        )
        try:
            card = load_agent_card(artifact)
        except AgentRuntimeError:
            # Direct execution does not expose discovery metadata over HTTP.
            card = {}
        # A one-shot invocation may execute tasks, but it must never compete
        # with a long-lived server for periodic schedule ownership.
        driver = _runtime_driver(application, card=card, enable_cron=False)
        validate_agent_principal_runtime(agent_principal, driver.info)
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
            agent_principal=agent_principal,
            transport="direct",
        )
        from .logging import get_logger
        from .tracing import span

        logger = get_logger(f"runtime.{application.framework}")
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
        if driver is None:
            # No runtime manager exists to adopt the compiler acquisition.
            _release_application_extensions(application)
        else:
            await driver.close()
        if telemetry is not None:
            telemetry.force_flush()


def _application_with_asset_store(application: Any) -> Any:
    """Install the fixed-ceiling development store when none was authored."""

    if application.asset_store is not None:
        return application
    from .assets import MemoryAssetStore

    default = MemoryAssetStore()
    stores = dict(getattr(application, "asset_stores", {}))
    stores["default"] = default
    return replace(application, asset_store=default, asset_stores=stores)


def create_fastapi_app(
    artifact: str | Path,
    *,
    bind_host: str = "127.0.0.1",
    request_timeout: float = 300,
    max_concurrency: int = 8,
    max_request_bytes: int = DEFAULT_SERVER_CONFIG.limits.max_request_bytes,
    playground_enabled: bool = True,
    openapi_enabled: bool = True,
    live_enabled: bool = True,
    agui_enabled: bool = True,
    agent_principal_required: bool = False,
    session_store: SessionStore | None = None,
    framework_session_storage: Any | None = None,
    authenticator: Authenticator | None = None,
) -> Any:
    """Build a server with explicit policy and no UI when production assets are absent."""

    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    if max_concurrency < 1:
        raise ValueError("max concurrency must be at least one")
    max_request_bytes = validate_max_request_bytes(max_request_bytes)
    # A transport opt-in must never rely on the truthiness of a string.
    if not isinstance(live_enabled, bool):
        raise TypeError("live_enabled must be boolean")
    if not isinstance(agui_enabled, bool):
        raise TypeError("agui_enabled must be boolean")
    if not isinstance(playground_enabled, bool):
        raise TypeError("playground_enabled must be boolean")
    from .playground import playground_available

    playground_enabled = playground_enabled and playground_available()
    if not isinstance(agent_principal_required, bool):
        raise TypeError("agent_principal_required must be boolean")
    application = load_compiled_application(artifact)
    application = _application_with_asset_store(application)
    try:
        return _build_fastapi_app(
            artifact,
            application=application,
            bind_host=bind_host,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            playground_enabled=playground_enabled,
            openapi_enabled=openapi_enabled,
            live_enabled=live_enabled,
            agui_enabled=agui_enabled,
            agent_principal_required=agent_principal_required,
            session_store=session_store,
            framework_session_storage=framework_session_storage,
            authenticator=authenticator,
        )
    except Exception:
        _release_application_extensions(application)
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
    openapi_enabled: bool,
    live_enabled: bool,
    agui_enabled: bool,
    agent_principal_required: bool,
    session_store: SessionStore | None,
    framework_session_storage: Any | None,
    authenticator: Authenticator | None,
) -> Any:
    """Apply the same live transport choice to neutral and native server backends."""

    _validate_session_storage_choice(
        application, framework_session_storage, session_store
    )
    card = load_agent_card(artifact)
    _apply_observability_defaults(application.framework)
    authenticator = _resolve_lifecycle_authenticator(
        application.lifecycle_extensions, authenticator
    )

    from .neutral_runtime import create_neutral_app
    from .playground_studio import PlaygroundStudioService
    from .telemetry import (
        configure_observability,
        instrument_fastapi,
    )

    adapter = get_framework(application.framework)
    sessions = _host_sessions(
        adapter, artifact, application, framework_session_storage, session_store
    )
    if adapter.exposes_framework_server(application):
        app, telemetry = adapter.build_framework_server(
            artifact,
            application=application,
            bind_host=bind_host,
            card=card,
            sessions=sessions,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            playground_enabled=playground_enabled,
            openapi_enabled=openapi_enabled,
            live_enabled=live_enabled,
            agui_enabled=agui_enabled,
            agent_principal_required=agent_principal_required,
            authenticator=authenticator,
            telemetry_exporter_factories=application.telemetry_exporters,
        )
    else:
        telemetry = configure_observability(
            application.name,
            framework=application.framework,
            exporter_factories=application.telemetry_exporters,
        )
        driver = _runtime_driver(application, card=card, sessions=sessions)
        eval_service = None
        mcp_service = None
        connectors_service = None
        if playground_enabled:
            from .playground_connectors import PlaygroundConnectorsService
            from .playground_eval import PlaygroundEvalService
            from .playground_mcp import PlaygroundMCPService

            mcp_service = PlaygroundMCPService(Path(artifact) / "source", application.framework)
            connectors_service = PlaygroundConnectorsService()
            eval_service = PlaygroundEvalService(
                artifact,
                application,
                driver,
            )
        from ._eval_capture_runtime import with_eval_capture

        # The eval service above keeps the raw driver so eval runs are never
        # captured as production traffic.
        app = create_neutral_app(
            with_eval_capture(driver, application),
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
            max_request_bytes=max_request_bytes,
            asset_store=application.asset_store,
            http_routes=application.http_routes,
            lifecycle_extensions=application.lifecycle_extensions,
            playground_enabled=playground_enabled,
            openapi_enabled=openapi_enabled,
            live_enabled=live_enabled,
            agui_enabled=agui_enabled,
            agent_principal_required=agent_principal_required,
            playground_eval_service=eval_service,
            playground_mcp_service=mcp_service,
            playground_connectors_service=connectors_service,
            playground_studio_service=PlaygroundStudioService(Path(artifact) / "source", mode=application.mode, framework=application.framework) if playground_enabled else None,
            authenticator=authenticator,
            a2a_task_store=_a2a_task_store(application),
        )
    instrument_fastapi(app, telemetry)
    _attach_library_lifecycle(app, Path(artifact).resolve() / "source")
    return app


def _host_sessions(
    adapter: Any,
    artifact: str | Path,
    application: Any,
    framework_storage: Any | None,
    store: SessionStore | None,
) -> RuntimeSessions:
    """Let the framework choose one session authority before its server is built."""

    try:
        return adapter.host_sessions(
            artifact, application, framework_storage=framework_storage, store=store
        )
    except FrameworkConventionError as exc:
        raise AgentRuntimeError(str(exc)) from exc


def _validate_session_storage_choice(
    application: Any,
    framework_storage: Any | None,
    store: SessionStore | None,
) -> None:
    """Reject host storage injection when the compiled agent owns that boundary."""

    if application.session_store is not None and (
        framework_storage is not None or store is not None
    ):
        raise AgentRuntimeError(
            "@lifecycle.storage.sessions cannot be combined with injected session storage"
        )


def _attach_driver_lifecycle(
    app: Any, driver: Any, *, startup_driver: Any | None = None
) -> None:
    """Run the final pipeline inside ADK's active custom lifespan."""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        """Own pipeline startup and cleanup around the framework lifespan."""

        from .runtime_pipeline import start_runtime_pipeline

        try:
            selected = driver if startup_driver is None else startup_driver
            await start_runtime_pipeline(selected)
            async with original_lifespan(application):
                try:
                    yield
                finally:
                    # FastAPI skips shutdown handlers when a custom lifespan
                    # owns the app, so cleanup must occur before it exits.
                    await driver.close()
        except BaseException:
            # Startup failures before ``yield`` still transfer no live runtime
            # to FastAPI; idempotent close must reclaim partial acquisitions.
            await driver.close()
            raise

    app.router.lifespan_context = lifespan


def _resolve_lifecycle_authenticator(
    listeners: Any, authenticator: Authenticator | None
) -> Authenticator | None:
    from .lifecycle_runtime import lifecycle_authenticator

    discovered = lifecycle_authenticator(listeners)
    if discovered is not None and authenticator is not None:
        raise AgentRuntimeError(
            "auth lifecycle listeners cannot be combined with an injected authenticator"
        )
    return discovered or authenticator


def _attach_library_lifecycle(app: Any, source_root: Path) -> None:
    """Keep lazy imports valid through shutdown, including custom lifespans."""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        """Retain authored modules until application shutdown completes."""

        try:
            async with original_lifespan(application):
                yield
        finally:
            release_authored_library(source_root)

    # Wrapping the active lifespan works for neutral and native ADK apps;
    # shutdown event handlers are skipped by frameworks with custom lifespans.
    app.router.lifespan_context = lifespan


def _release_application_extensions(application: Any) -> None:
    """Release an unadopted compiler acquisition without exposing namespaces."""

    from .extensions import release_extensions

    extensions = tuple(getattr(application, "extensions", ()))
    release_extensions(tuple(item.descriptor for item in extensions))


def _runtime_parser() -> argparse.ArgumentParser:
    """Build the generated launcher's explicit run/serve command surface."""

    parser = argparse.ArgumentParser(prog="harnest-agent")
    parser.add_argument("--artifact", type=Path, required=True, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the compiled agent over HTTP")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--request-timeout", type=float, default=None)
    serve.add_argument("--max-concurrency", type=int, default=None)
    serve.add_argument("--authoring-root", type=Path, default=None, help=argparse.SUPPRESS)
    serve.add_argument("--playground-assets", type=Path, default=None, help=argparse.SUPPRESS)
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        default=None,
        help="allow a non-loopback bind; this server has no built-in authentication",
    )
    run = commands.add_parser("run", help="invoke the compiled agent in this process")
    run.add_argument("message", nargs="?", help="message to invoke (otherwise read stdin)")
    run.add_argument("--session", default=None)
    run.add_argument("--output", choices=("text", "json", "ndjson"), default="text")
    return parser


def _load_server(args: Any) -> tuple[Any, Any]:
    """Resolve server defaults and transport opt-ins before constructing the app."""

    server = load_server_config(args.artifact / SERVER_CONFIG_FILENAME)
    server = server.with_overrides(
        host=args.host,
        port=args.port,
        request_timeout_seconds=args.request_timeout,
        max_concurrent_requests=args.max_concurrency,
        allow_remote=args.allow_remote,
    )
    http = server.http
    if not http.allow_remote and http.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ServerConfigError(
            "non-loopback binds require http.allowRemote: true in server.yaml"
        )
    application = _create_server_app(args, server)
    return application, http


def _create_server_app(args: Any, server: Any) -> Any:
    """Capture CLI workspace capabilities only for the local Playground."""

    from .playground import playground_assets

    directory = getattr(args, "playground_assets", None)
    from .playground_authoring import authoring_workspace

    root = getattr(args, "authoring_root", None) if directory is not None else None
    eval_root = _local_eval_root(directory, server.http)
    with playground_assets(directory), authoring_workspace(root, eval_root=eval_root):
        return _create_configured_app(args.artifact, server, directory is not None)


def _local_eval_root(directory: Path | None, http: HTTPServerConfig) -> Path | None:
    """Keep deployed launchers and remote-enabled servers outside workspace capture."""

    if directory is None or http.allow_remote or http.host not in {"localhost", "127.0.0.1", "::1"}:
        return None
    value = os.environ.get("HARNEST_PLAYGROUND_EVAL_ROOT")
    return Path(value) if value else None


def _create_configured_app(artifact: Path, server: Any, development: bool) -> Any:
    """Apply the compiled HTTP policy without making UI a production dependency."""

    from .playground import playground_available
    from .server_concurrency import install_concurrency_limit

    http = server.http
    playground_enabled = development and server.playground.enabled
    app = create_fastapi_app(
        artifact,
        bind_host=http.host,
        request_timeout=http.request_timeout_seconds,
        max_concurrency=http.max_concurrent_requests,
        max_request_bytes=server.limits.max_request_bytes,
        playground_enabled=playground_enabled,
        openapi_enabled=server.openapi,
        live_enabled=server.live,
        agui_enabled=server.agui,
        agent_principal_required=server.agent_principal == "required",
    )
    # The cap lives in the app rather than uvicorn so it counts in-flight
    # requests on both neutral and native ADK routes, not idle connections.
    install_concurrency_limit(
        app,
        http.max_concurrent_requests,
        playground_enabled=playground_enabled and playground_available(),
    )
    return app


def _serve_command(args: Any) -> int:
    """Serve a compiled artifact after validating its network exposure policy."""

    try:
        app, http = _load_server(args)
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
    from .runtime_openapi import announce_openapi

    announce_openapi(app, host=http.host, port=http.port)
    uvicorn.run(
        app,
        host=http.host,
        port=http.port,
        log_level="info",
        # http.maxConcurrentRequests is enforced in-app by
        # ConcurrencyLimitMiddleware; uvicorn's limit_concurrency counted idle
        # keep-alive connections and rejected parallel Playground asset loads.
        timeout_keep_alive=10,
        # Dynamic session and asset paths contain opaque private identifiers;
        # Harnest's structured telemetry remains the privacy-safe request signal.
        access_log=False,
    )
    return 0


def _read_local_message(stream: Any) -> str:
    """Read a bounded prompt from stdin so it never appears in process arguments."""

    value = stream.read(_MAX_CARD_BYTES + 1)
    if len(value.encode("utf-8")) > _MAX_CARD_BYTES:
        raise ValueError("local invocation input exceeds the 4 MiB limit")
    if not value.strip():
        raise ValueError("local invocation requires a non-empty message on stdin")
    return value


def _compiled_cli_enabled(artifact: str | Path) -> bool:
    """Read the immutable CLI opt-in before importing any authored code."""

    path = Path(artifact).resolve() / _COMPILED_MANIFEST_FILENAME
    manifest = _read_compiled_manifest(path)
    return _manifest_cli_enabled(manifest)


def _read_compiled_manifest(path: Path) -> dict[str, Any]:
    """Decode one bounded, unambiguous compiled manifest object."""

    if path.is_symlink() or not path.is_file():
        raise AgentRuntimeError(f"compiled artifact is missing {path.name}: {path}")
    try:
        if path.stat().st_size > _MAX_CARD_BYTES:
            raise AgentRuntimeError("compiled manifest exceeds the 4 MiB limit")
        manifest = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_manifest_keys,
        )
    except AgentRuntimeError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AgentRuntimeError(f"invalid compiled manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise AgentRuntimeError("compiled manifest must contain a JSON object")
    return manifest


def _manifest_cli_enabled(manifest: dict[str, Any]) -> bool:
    """Resolve the strict compiled CLI policy from the current manifest."""

    interfaces = manifest.get("interfaces")
    if not isinstance(interfaces, dict) or set(interfaces) != {"cli"}:
        raise AgentRuntimeError("compiled manifest interfaces must contain only cli")
    enabled = interfaces["cli"]
    if not isinstance(enabled, bool):
        raise AgentRuntimeError("compiled manifest interfaces.cli must be a boolean")
    return enabled


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous policy keys before using a compiled manifest."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


async def _run_local_artifact(args: Any, message: str) -> None:
    """Run one CLI turn while owning the compiled library and driver lifetimes."""

    application = load_compiled_application(args.artifact)
    try:
        await _run_local_application(application, args, message)
    finally:
        release_authored_library(args.artifact.resolve() / "source")


async def _run_local_application(application: Any, args: Any, message: str) -> None:
    """Execute the shared local adapter with scheduling disabled for one-shot use."""

    from ._context_agent import LocalAgentRuntime
    from .runtime_cli import run_local_cli
    from .telemetry import configure_observability

    _apply_observability_defaults(application.framework)
    driver = None
    telemetry = configure_observability(
        application.name,
        framework=application.framework,
        exporter_factories=application.telemetry_exporters,
    )
    try:
        try:
            card = load_agent_card(args.artifact)
        except AgentRuntimeError:
            card = {}
        driver = _runtime_driver(application, card=card, enable_cron=False)
        runtime = LocalAgentRuntime(
            driver,
            user_id=_DIRECT_USER_ID,
            transport="cli",
            trigger="user",
        )
        await run_local_cli(
            runtime,
            message,
            session_id=args.session,
            output=args.output,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    finally:
        if driver is None:
            _release_application_extensions(application)
        else:
            await driver.close()
        telemetry.force_flush()


def _run_command(args: Any) -> int:
    """Invoke one message, retaining safe task diagnostics and shell cancellation."""

    from .runtime_task import TaskRuntimeError

    try:
        if not _compiled_cli_enabled(args.artifact):
            raise AgentRuntimeError(
                "CLI invocation is disabled; set spec.interfaces.cli: true "
                "before compiling the agent"
            )
        # A positional message must not block on stdin; both forms share size validation.
        positional = getattr(args, "message", None)
        source = StringIO(positional) if positional is not None else sys.stdin
        message = _read_local_message(source)
        asyncio.run(_run_local_artifact(args, message))
    except KeyboardInterrupt:
        return 130
    except TaskRuntimeError as exc:
        # Task boundaries already sanitize provider failures; hiding their text
        # loses actionable storage and worker diagnostics without adding privacy.
        print(f"harnest-agent: {exc}", file=sys.stderr)
        return 1
    except (AgentRuntimeError, ImportError, OSError, TypeError, ValueError) as exc:
        print(f"harnest-agent: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Provider exceptions can contain request content. The structured runtime
        # log retains the exception type while terminal output stays payload-free.
        print(
            f"harnest-agent: local invocation failed with {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch one explicit generated-artifact command."""

    args = _runtime_parser().parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    return _serve_command(args)


# Keep the authored runtime contract narrower than this process-bootstrap module.
# Generated artifacts may call ``main`` directly, but it is not a library API.
__all__ = [
    "AgentExecutionTimeout",
    "AgentInfo",
    "AgentRuntimeError",
    "InvocationRequest",
    "InvocationResult",
    "NoCustomerFacingOutputError",
    "ResponseRequest",
    "RuntimeDriver",
    "RuntimeEvent",
    "SessionConflictError",
    "SessionMessage",
    "SessionRecord",
    "create_fastapi_app",
    "load_agent_card",
    "load_compiled_agent",
    "load_framework_app",
    "load_compiled_application",
    "run_agent_message",
]


if __name__ == "__main__":
    raise SystemExit(main())

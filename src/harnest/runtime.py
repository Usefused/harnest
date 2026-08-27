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
    """Load the portable application boundary from a compiled artifact."""

    directory = Path(artifact).resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise AgentRuntimeError(f"compiled artifact directory is invalid: {directory}")
    entrypoint = directory / "agent.py"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise AgentRuntimeError(f"compiled artifact is missing agent.py: {directory}")
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
) -> Any:
    """Select a backend once, at the process boundary."""

    if application.framework == "adk":
        from .runtime_adk import ADKRuntimeDriver

        driver = ADKRuntimeDriver(
            application, card=card, extra_endpoints=extra_endpoints
        )
    elif application.framework == "langgraph":
        from .runtime_langgraph import LangGraphRuntimeDriver

        driver = LangGraphRuntimeDriver(application, card=card)
    else:
        raise AgentRuntimeError(f"unsupported framework: {application.framework}")
    if application.extensions:
        from .runtime_extensions import ExtensionRuntimeDriver

        return ExtensionRuntimeDriver(driver, application.extensions)
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
    _apply_observability_defaults(application.framework)

    from .neutral_runtime import InvocationRequest
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
        "session_service_uri": "memory://",
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
        return get_fast_api_app(**kwargs)
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
) -> Any:
    """Build the neutral server and opt advanced ADK agents into native routes."""

    if request_timeout <= 0:
        raise ValueError("request timeout must be greater than zero")
    if max_concurrency < 1:
        raise ValueError("max concurrency must be at least one")
    application = load_compiled_application(artifact)
    card = load_agent_card(artifact)
    _apply_observability_defaults(application.framework)

    from .neutral_runtime import create_neutral_app, create_neutral_router
    from .telemetry import configure_observability, instrument_fastapi

    if _exposes_native_adk_api(application):
        os.environ.setdefault("OTEL_SERVICE_NAME", application.name)
        app = _create_adk_fastapi_app(
            artifact, application=application, bind_host=bind_host
        )
        driver = _runtime_driver(
            application,
            card=card,
            extra_endpoints={"adkRun": "/run", "adkRunSse": "/run_sse"},
        )
        neutral_router = create_neutral_router(
            driver,
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
        )
        # FastAPI 0.135+ retains included routers as private lazy route-table
        # entries. ADK exposes its native routes as a flat public table, so keep
        # that contract when adding Harnest's dependency-free neutral routes.
        app.router.routes.extend(neutral_router.routes)
        app.router.add_event_handler("shutdown", driver.close)
        adk_owns_otel = any(
            os.getenv(name, "").strip()
            for name in (
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            )
        )
        telemetry = configure_observability(
            application.name,
            framework=application.framework,
            use_global_providers=adk_owns_otel,
        )
    else:
        telemetry = configure_observability(
            application.name, framework=application.framework
        )
        app = create_neutral_app(
            _runtime_driver(application, card=card),
            request_timeout=request_timeout,
            max_concurrency=max_concurrency,
        )
    instrument_fastapi(app, telemetry)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harnest-agent")
    parser.add_argument("--artifact", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback bind; this server has no built-in authentication",
    )
    args = parser.parse_args(argv)
    if not args.allow_remote and args.host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "harnest-agent: non-loopback binds require --allow-remote; "
            "the server has no built-in authentication",
            file=sys.stderr,
        )
        return 2
    try:
        app = create_fastapi_app(
            args.artifact,
            bind_host=args.host,
            request_timeout=args.request_timeout,
            max_concurrency=args.max_concurrency,
        )
        import uvicorn
    except (AgentRuntimeError, ImportError, OSError, TypeError, ValueError) as exc:
        print(f"harnest-agent: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        limit_concurrency=args.max_concurrency,
        timeout_keep_alive=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

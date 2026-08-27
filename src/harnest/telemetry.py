"""Runtime-owned OpenTelemetry configuration for compiled agents."""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from opentelemetry import trace


_LOCK = threading.Lock()
_STATE: "TelemetryState | None" = None
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return normalized == "true"


def _otel_enabled() -> bool:
    if _boolean("OTEL_SDK_DISABLED", False):
        return False
    explicit = os.getenv("HARNEST_OTEL_ENABLED")
    if explicit is not None and explicit.strip():
        return _boolean("HARNEST_OTEL_ENABLED", False)
    return any(
        os.getenv(name, "").strip()
        for name in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        )
    )


def _level() -> int:
    value = os.getenv("HARNEST_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(value)
    if not isinstance(level, int):
        raise ValueError(f"unsupported HARNEST_LOG_LEVEL: {value}")
    return level


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        value: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "severity": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        if context.is_valid:
            value["traceId"] = f"{context.trace_id:032x}"
            value["spanId"] = f"{context.span_id:016x}"
        attributes = {
            key: item
            for key, item in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS
            and key
            not in {
                "message",
                "asctime",
                "otelSpanID",
                "otelTraceID",
                "otelServiceName",
                "otelTraceSampled",
            }
        }
        if attributes:
            value["attributes"] = attributes
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(slots=True)
class TelemetryState:
    service_name: str
    framework: str
    enabled: bool
    tracer_provider: Any | None = None
    logger_provider: Any | None = None
    owns_tracer_provider: bool = False
    owns_logger_provider: bool = False

    def force_flush(self, timeout_millis: int = 5000) -> None:
        providers = (
            self.tracer_provider if self.owns_tracer_provider else None,
            self.logger_provider if self.owns_logger_provider else None,
        )
        for provider in providers:
            flush = getattr(provider, "force_flush", None)
            if callable(flush):
                try:
                    flush(timeout_millis=timeout_millis)
                except TypeError:
                    flush(timeout_millis)

    def shutdown(self) -> None:
        providers = (
            self.logger_provider if self.owns_logger_provider else None,
            self.tracer_provider if self.owns_tracer_provider else None,
        )
        for provider in providers:
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()


def _exporter_name(signal: str) -> str:
    explicit = os.getenv(f"OTEL_{signal.upper()}_EXPORTER")
    if explicit is not None and explicit.strip():
        return explicit.strip().lower()
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return "otlp"
    current = f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT"
    other = (
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
        if signal == "traces"
        else "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    if os.getenv(current, "").strip():
        return "otlp"
    if os.getenv(other, "").strip():
        return "none"
    return "otlp"


def _trace_exporter(name: str) -> Any | None:
    if name == "none":
        return None
    if name == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()
    if name != "otlp":
        raise ValueError(f"unsupported OTEL_TRACES_EXPORTER: {name}")
    protocol = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    ).strip()
    if protocol != "http/protobuf":
        raise ValueError(
            "Harnest's bundled exporter supports OTLP http/protobuf; set "
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf"
        )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    return OTLPSpanExporter()


def _log_exporter(name: str) -> Any | None:
    if name == "none":
        return None
    if name == "console":
        from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter

        return ConsoleLogRecordExporter()
    if name != "otlp":
        raise ValueError(f"unsupported OTEL_LOGS_EXPORTER: {name}")
    protocol = os.getenv(
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
    ).strip()
    if protocol != "http/protobuf":
        raise ValueError(
            "Harnest's bundled exporter supports OTLP http/protobuf; set "
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf"
        )
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    return OTLPLogExporter()


def _configure_agent_logger(logger_provider: Any | None) -> None:
    agent_logger = logging.getLogger("harnest.agent")
    agent_logger.setLevel(_level())
    agent_logger.propagate = False
    for handler in tuple(agent_logger.handlers):
        if getattr(handler, "__harnest_telemetry__", False):
            agent_logger.removeHandler(handler)
            handler.close()
    if _boolean("HARNEST_LOG_CONSOLE", True):
        console = logging.StreamHandler()
        console.__harnest_telemetry__ = True
        console.setFormatter(_JSONFormatter())
        agent_logger.addHandler(console)
    if logger_provider is not None:
        try:
            from opentelemetry.instrumentation.logging.handler import LoggingHandler
        except ImportError:
            # OTel's SDK owns the handler. The instrumentation package merely
            # re-exports it in some release trains and is not required here.
            from opentelemetry.sdk._logs import LoggingHandler

        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        handler.__harnest_telemetry__ = True
        agent_logger.addHandler(handler)


def configure_observability(
    service_name: str,
    *,
    framework: str,
    service_version: str | None = None,
    span_exporter: Any | None = None,
    log_exporter: Any | None = None,
    use_global_providers: bool = False,
    set_global_providers: bool = True,
) -> TelemetryState:
    """Configure console logging and optional OTLP traces/logs once per process."""

    global _STATE
    if not isinstance(service_name, str) or not service_name.strip():
        raise ValueError("telemetry service_name must be a non-empty string")
    if framework not in {"adk", "langgraph"}:
        raise ValueError("telemetry framework must be adk or langgraph")
    with _LOCK:
        if _STATE is not None:
            return _STATE
        enabled = _otel_enabled() or span_exporter is not None or log_exporter is not None
        tracer_provider, logger_provider, owns_providers = _providers(
            enabled=enabled, service_name=service_name, framework=framework,
            service_version=service_version, span_exporter=span_exporter,
            log_exporter=log_exporter, use_global_providers=use_global_providers,
            set_global_providers=set_global_providers,
        )
        _configure_agent_logger(logger_provider)
        _STATE = TelemetryState(
            service_name=service_name.strip(),
            framework=framework,
            enabled=enabled,
            tracer_provider=tracer_provider,
            logger_provider=logger_provider,
            owns_tracer_provider=owns_providers,
            owns_logger_provider=owns_providers,
        )
        if _STATE.owns_tracer_provider or _STATE.owns_logger_provider:
            atexit.register(_STATE.shutdown)
        return _STATE


def _providers(**options: Any) -> tuple[Any | None, Any | None, bool]:
    if not options["enabled"]:
        return None, None, False
    if _should_adopt_global(options):
        if options["span_exporter"] is not None or options["log_exporter"] is not None:
            raise ValueError("custom exporters cannot be combined with global providers")
        from opentelemetry import _logs

        return trace.get_tracer_provider(), _logs.get_logger_provider(), False
    tracer_provider, logger_provider = _new_providers(options)
    if options["set_global_providers"]:
        from opentelemetry import _logs

        trace.set_tracer_provider(tracer_provider)
        _logs.set_logger_provider(logger_provider)
    return tracer_provider, logger_provider, True


def _should_adopt_global(options: Mapping[str, Any]) -> bool:
    if options["use_global_providers"] or not options["set_global_providers"]:
        return bool(options["use_global_providers"])
    from opentelemetry import _logs
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk.trace import TracerProvider

    return isinstance(trace.get_tracer_provider(), TracerProvider) or isinstance(
        _logs.get_logger_provider(), LoggerProvider
    )


def _new_providers(options: Mapping[str, Any]) -> tuple[Any, Any]:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    attributes = {
        "service.name": os.getenv("OTEL_SERVICE_NAME", options["service_name"]).strip(),
        "harnest.framework": options["framework"],
    }
    if options["service_version"]:
        attributes["service.version"] = options["service_version"]
    resource = Resource.create(attributes)
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    selected_span = options["span_exporter"]
    if selected_span is None:
        selected_span = _trace_exporter(_exporter_name("traces"))
    selected_log = options["log_exporter"]
    if selected_log is None:
        selected_log = _log_exporter(_exporter_name("logs"))
    if selected_span is not None:
        tracer_provider.add_span_processor(BatchSpanProcessor(selected_span))
    if selected_log is not None:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(selected_log))
    return tracer_provider, logger_provider


def get_tracer(name: str, *, version: str | None = None) -> Any:
    state = _STATE
    if state is not None and state.tracer_provider is not None:
        return state.tracer_provider.get_tracer(name, version)
    return trace.get_tracer(name, version)


def instrument_fastapi(app: Any, state: TelemetryState) -> None:
    """Attach server request spans to one FastAPI application."""

    if not state.enabled or state.tracer_provider is None:
        return

    def register_flush() -> None:
        add_event_handler = getattr(app, "add_event_handler", None)
        if callable(add_event_handler):
            add_event_handler("shutdown", state.force_flush)
            return
        on_shutdown = getattr(getattr(app, "router", None), "on_shutdown", None)
        if isinstance(on_shutdown, list):
            on_shutdown.append(state.force_flush)

    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        register_flush()
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    excluded_urls = os.getenv("HARNEST_OTEL_EXCLUDED_URLS")
    if excluded_urls is None:
        excluded_urls = os.getenv(
            "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
            "/healthz,/.well-known/agent-card.json",
        )
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=state.tracer_provider,
        excluded_urls=excluded_urls,
    )
    register_flush()


def _reset_for_testing() -> None:
    global _STATE
    with _LOCK:
        if _STATE is not None:
            _STATE.shutdown()
        _STATE = None
        _configure_agent_logger(None)


__all__ = [
    "TelemetryState",
    "configure_observability",
    "get_tracer",
    "instrument_fastapi",
]

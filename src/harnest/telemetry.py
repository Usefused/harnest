"""Runtime-owned OpenTelemetry configuration for compiled agents."""

from __future__ import annotations

import atexit
import inspect
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from opentelemetry import trace


_LOCK = threading.Lock()
_STATE: "TelemetryState | None" = None
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


class TelemetryExporterError(RuntimeError):
    """A runtime telemetry-exporter factory returned an invalid destination."""


@dataclass(frozen=True, slots=True)
class TelemetryExporter:
    """One named telemetry destination with optional trace and log exporters."""

    name: str
    traces: Any | None = None
    logs: Any | None = None

    def __post_init__(self) -> None:
        """Validate signal exporters without initializing external services."""

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("telemetry exporter name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())
        if self.traces is None and self.logs is None:
            raise ValueError("telemetry exporter must provide traces or logs")
        _validate_signal_exporter(self.traces, signal="traces")
        _validate_signal_exporter(self.logs, signal="logs")


def _validate_signal_exporter(exporter: Any | None, *, signal: str) -> None:
    """Require the OpenTelemetry SDK contract for the selected signal."""

    if exporter is None:
        return
    if signal == "traces":
        from opentelemetry.sdk.trace.export import SpanExporter

        expected = SpanExporter
    else:
        from opentelemetry.sdk._logs.export import LogRecordExporter

        expected = LogRecordExporter
    if not isinstance(exporter, expected):
        raise TypeError(
            f"telemetry {signal} exporter must implement {expected.__name__}; "
            f"got {type(exporter).__name__}"
        )


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
    span_processors: tuple[Any, ...] = ()
    log_processors: tuple[Any, ...] = ()
    shutdown_called: bool = False

    def force_flush(self, timeout_millis: int = 5000) -> None:
        """Flush owned providers or only processors added to adopted providers."""

        targets = self._flush_targets()
        for target in targets:
            flush = getattr(target, "force_flush", None)
            if callable(flush):
                try:
                    flush(timeout_millis=timeout_millis)
                except TypeError:
                    flush(timeout_millis)

    def _flush_targets(self) -> tuple[Any, ...]:
        """Avoid flushing every exporter attached to a provider Harnest adopted."""

        values: list[Any] = []
        if self.owns_tracer_provider and self.tracer_provider is not None:
            values.append(self.tracer_provider)
        else:
            values.extend(self.span_processors)
        if self.owns_logger_provider and self.logger_provider is not None:
            values.append(self.logger_provider)
        else:
            values.extend(self.log_processors)
        return tuple(values)

    def shutdown(self) -> None:
        """Shut down providers or processors created and owned by Harnest."""

        if self.shutdown_called:
            return
        self.shutdown_called = True
        for target in self._shutdown_targets():
            shutdown = getattr(target, "shutdown", None)
            if callable(shutdown):
                shutdown()

    def _shutdown_targets(self) -> tuple[Any, ...]:
        """Close added processors when their adopted provider remains host-owned."""

        values: list[Any] = []
        if self.owns_logger_provider and self.logger_provider is not None:
            values.append(self.logger_provider)
        else:
            values.extend(reversed(self.log_processors))
        if self.owns_tracer_provider and self.tracer_provider is not None:
            values.append(self.tracer_provider)
        else:
            values.extend(reversed(self.span_processors))
        return tuple(values)


def resolve_telemetry_exporters(
    factories: Sequence[Any],
) -> tuple[TelemetryExporter, ...]:
    """Call discovered exporter factories only during runtime bootstrap."""

    resolved: list[TelemetryExporter] = []
    try:
        for factory in factories:
            resolved.append(_call_telemetry_factory(factory))
    except BaseException:
        _shutdown_exporters(resolved)
        raise
    names = [item.name for item in resolved]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        _shutdown_exporters(resolved)
        raise TelemetryExporterError(
            "duplicate telemetry exporter names: " + ", ".join(duplicates)
        )
    return tuple(resolved)


def _shutdown_exporters(exporters: Sequence[TelemetryExporter]) -> None:
    """Release constructed destinations when runtime validation cannot continue."""

    seen: set[int] = set()
    for destination in reversed(exporters):
        for exporter in (destination.logs, destination.traces):
            if exporter is None or id(exporter) in seen:
                continue
            seen.add(id(exporter))
            shutdown = getattr(exporter, "shutdown", None)
            if callable(shutdown):
                shutdown()


def _call_telemetry_factory(factory: Any) -> TelemetryExporter:
    """Resolve one factory without retaining a potentially sensitive failure."""

    callback = getattr(factory, "callback", None)
    identity = getattr(factory, "identity", repr(factory))
    if not callable(callback):
        raise TelemetryExporterError(
            f"telemetry exporter factory {identity} is not callable"
        )
    failure: TelemetryExporterError | None = None
    try:
        value = callback()
    except Exception as error:
        failure = TelemetryExporterError(
            f"telemetry exporter factory {identity} failed with "
            f"{type(error).__name__}"
        )
        value = None
    if failure is not None:
        raise failure
    if inspect.isawaitable(value):
        closer = getattr(value, "close", None)
        if callable(closer):
            closer()
        raise TelemetryExporterError(
            f"telemetry exporter factory {identity} must be synchronous"
        )
    if not isinstance(value, TelemetryExporter):
        raise TelemetryExporterError(
            f"telemetry exporter factory {identity} must return "
            f"TelemetryExporter; got {type(value).__name__}"
        )
    return value


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
    exporters: Sequence[TelemetryExporter] = (),
    exporter_factories: Sequence[Any] = (),
    use_global_providers: bool = False,
    set_global_providers: bool = True,
) -> TelemetryState:
    """Configure console logging and optional OTLP traces/logs once per process."""

    global _STATE
    destinations = _validated_configuration(service_name, framework, exporters)
    with _LOCK:
        if _STATE is not None:
            return _STATE
        authored = resolve_telemetry_exporters(exporter_factories)
        destinations = _validated_destinations((*destinations, *authored))
        otel_enabled = _otel_enabled()
        enabled = _observability_enabled(
            otel_enabled, span_exporter, log_exporter, destinations
        )
        try:
            providers = _providers(
                enabled=enabled, service_name=service_name, framework=framework,
                service_version=service_version, span_exporter=span_exporter,
                log_exporter=log_exporter,
                use_global_providers=use_global_providers,
                set_global_providers=set_global_providers,
                exporters=destinations, otel_enabled=otel_enabled,
            )
        except BaseException:
            _shutdown_exporters(destinations)
            raise
        (
            tracer_provider,
            logger_provider,
            owns_tracer_provider,
            owns_logger_provider,
            span_processors,
            log_processors,
        ) = providers
        _configure_agent_logger(logger_provider)
        _STATE = TelemetryState(
            service_name=service_name.strip(),
            framework=framework,
            enabled=enabled,
            tracer_provider=tracer_provider,
            logger_provider=logger_provider,
            owns_tracer_provider=owns_tracer_provider,
            owns_logger_provider=owns_logger_provider,
            span_processors=span_processors,
            log_processors=log_processors,
        )
        if any(
            (
                _STATE.owns_tracer_provider,
                _STATE.owns_logger_provider,
                bool(_STATE.span_processors),
                bool(_STATE.log_processors),
            )
        ):
            atexit.register(_STATE.shutdown)
        return _STATE


def _validated_configuration(
    service_name: str,
    framework: str,
    exporters: Sequence[TelemetryExporter],
) -> tuple[TelemetryExporter, ...]:
    """Validate stable bootstrap inputs before claiming the process singleton."""

    if not isinstance(service_name, str) or not service_name.strip():
        raise ValueError("telemetry service_name must be a non-empty string")
    if framework not in {"adk", "langgraph"}:
        raise ValueError("telemetry framework must be adk or langgraph")
    return _validated_destinations(exporters)


def _validated_destinations(
    exporters: Sequence[TelemetryExporter],
) -> tuple[TelemetryExporter, ...]:
    """Require typed destinations with unique stable names."""

    destinations = tuple(exporters)
    if any(not isinstance(item, TelemetryExporter) for item in destinations):
        raise TypeError("exporters must contain only TelemetryExporter values")
    names = [item.name for item in destinations]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise TelemetryExporterError(
            "duplicate telemetry exporter names: " + ", ".join(duplicates)
        )
    return destinations


def _observability_enabled(
    otel_enabled: bool,
    span_exporter: Any | None,
    log_exporter: Any | None,
    exporters: Sequence[TelemetryExporter],
) -> bool:
    """Enable providers when any environment or authored destination exists."""

    return any(
        (
            otel_enabled,
            span_exporter is not None,
            log_exporter is not None,
            bool(exporters),
        )
    )


def _providers(
    **options: Any,
) -> tuple[
    Any | None,
    Any | None,
    bool,
    bool,
    tuple[Any, ...],
    tuple[Any, ...],
]:
    """Create providers or extend adopted providers with authored exporters."""

    if not options["enabled"]:
        return None, None, False, False, (), ()
    resource = _telemetry_resource(options)
    tracer_provider, owns_tracer = _select_tracer_provider(resource, options)
    logger_provider, owns_logger = _select_logger_provider(resource, options)
    span_processors, log_processors = _attach_exporters(
        tracer_provider,
        logger_provider,
        _signal_exporters(
            options, "traces", include_environment=owns_tracer
        ),
        _signal_exporters(options, "logs", include_environment=owns_logger),
    )
    if options["set_global_providers"] and owns_tracer:
        trace.set_tracer_provider(tracer_provider)
    if options["set_global_providers"] and owns_logger:
        from opentelemetry import _logs

        _logs.set_logger_provider(logger_provider)
    return (
        tracer_provider,
        logger_provider,
        owns_tracer,
        owns_logger,
        span_processors,
        log_processors,
    )


def _telemetry_resource(options: Mapping[str, Any]) -> Any:
    """Build the shared resource used by providers Harnest creates."""

    from opentelemetry.sdk.resources import Resource

    attributes = {
        "service.name": os.getenv("OTEL_SERVICE_NAME", options["service_name"]).strip(),
        "harnest.framework": options["framework"],
    }
    if options["service_version"]:
        attributes["service.version"] = options["service_version"]
    return Resource.create(attributes)


def _select_tracer_provider(
    resource: Any, options: Mapping[str, Any]
) -> tuple[Any, bool]:
    """Adopt only a capable tracer provider or create a signal-local provider."""

    from opentelemetry.sdk.trace import TracerProvider

    current = trace.get_tracer_provider()
    if _can_adopt(current, "add_span_processor", options):
        return current, False
    return TracerProvider(resource=resource, shutdown_on_exit=False), True


def _select_logger_provider(
    resource: Any, options: Mapping[str, Any]
) -> tuple[Any, bool]:
    """Adopt only a capable logger provider or create a signal-local provider."""

    from opentelemetry import _logs
    from opentelemetry.sdk._logs import LoggerProvider

    current = _logs.get_logger_provider()
    if _can_adopt(current, "add_log_record_processor", options):
        return current, False
    return LoggerProvider(resource=resource, shutdown_on_exit=False), True


def _can_adopt(provider: Any, method: str, options: Mapping[str, Any]) -> bool:
    """Adopt globals only when requested and capable of accepting processors."""

    adoption_enabled = (
        options["use_global_providers"] or options["set_global_providers"]
    )
    return adoption_enabled and callable(getattr(provider, method, None))


def _signal_exporters(
    options: Mapping[str, Any], signal: str, *, include_environment: bool
) -> tuple[Any, ...]:
    """Select the default signal exporter and all authored destinations."""

    key = "span_exporter" if signal == "traces" else "log_exporter"
    values: list[Any] = []
    explicit = options[key]
    if explicit is not None:
        values.append(explicit)
    elif include_environment and options["otel_enabled"]:
        factory = _trace_exporter if signal == "traces" else _log_exporter
        selected = factory(_exporter_name(signal))
        if selected is not None:
            values.append(selected)
    values.extend(
        getattr(destination, signal)
        for destination in options["exporters"]
        if getattr(destination, signal) is not None
    )
    return tuple(values)


def _attach_exporters(
    tracer_provider: Any,
    logger_provider: Any,
    span_exporters: Sequence[Any],
    log_exporters: Sequence[Any],
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Attach one batch processor per destination and return owned additions."""

    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    add_span = getattr(tracer_provider, "add_span_processor", None)
    add_log = getattr(logger_provider, "add_log_record_processor", None)
    if span_exporters and not callable(add_span):
        raise TelemetryExporterError(
            "the active tracer provider cannot accept telemetry exporters"
        )
    if log_exporters and not callable(add_log):
        raise TelemetryExporterError(
            "the active logger provider cannot accept telemetry exporters"
        )
    span_processors = tuple(BatchSpanProcessor(item) for item in span_exporters)
    log_processors = tuple(BatchLogRecordProcessor(item) for item in log_exporters)
    try:
        for processor in span_processors:
            add_span(processor)
        for processor in log_processors:
            add_log(processor)
    except BaseException:
        _shutdown_processors((*log_processors, *span_processors))
        raise
    return span_processors, log_processors


def _shutdown_processors(processors: Sequence[Any]) -> None:
    """Release batch workers when provider attachment cannot complete."""

    for processor in reversed(processors):
        shutdown = getattr(processor, "shutdown", None)
        if callable(shutdown):
            shutdown()


def get_tracer(name: str, *, version: str | None = None) -> Any:
    state = _STATE
    if state is not None and state.tracer_provider is not None:
        return state.tracer_provider.get_tracer(name, version)
    return trace.get_tracer(name, version)


def instrument_fastapi(app: Any, state: TelemetryState) -> None:
    """Attach server request spans to one FastAPI application."""

    if not state.enabled or state.tracer_provider is None:
        return
    _attach_telemetry_lifecycle(app, state)
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
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


def _attach_telemetry_lifecycle(app: Any, state: TelemetryState) -> None:
    """Shut down owned providers and processors after the application lifespan."""

    router = getattr(app, "router", None)
    original = getattr(router, "lifespan_context", None)
    if not callable(original) or getattr(app, "__harnest_telemetry_lifespan__", False):
        return

    @asynccontextmanager
    async def lifespan(application: Any):
        try:
            async with original(application):
                yield
        finally:
            state.shutdown()

    router.lifespan_context = lifespan
    app.__harnest_telemetry_lifespan__ = True


def _reset_for_testing() -> None:
    global _STATE
    with _LOCK:
        if _STATE is not None:
            _STATE.shutdown()
        _STATE = None
        _configure_agent_logger(None)


__all__ = [
    "TelemetryExporter",
    "TelemetryExporterError",
    "TelemetryState",
    "configure_observability",
    "get_tracer",
    "instrument_fastapi",
    "resolve_telemetry_exporters",
]

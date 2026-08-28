from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from harnest.logging import get_logger
from harnest.telemetry import (
    TelemetryExporter,
    _exporter_name,
    _reset_for_testing,
    configure_observability,
)
from harnest.tracing import current_trace_ids, span, traced


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _TrackingSpanExporter(InMemorySpanExporter):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        super().shutdown()


class _TrackingLogExporter(InMemoryLogRecordExporter):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        super().shutdown()


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_for_testing()

    def tearDown(self) -> None:
        _reset_for_testing()

    def test_structured_logger_binds_attributes_without_mutating_parent(self):
        capture = _Capture()
        root = logging.getLogger("harnest.agent")
        root.addHandler(capture)
        try:
            with patch.dict(
                os.environ,
                {
                    "HARNEST_LOG_CONSOLE": "false",
                    "HARNEST_OTEL_ENABLED": "false",
                },
            ):
                configure_observability("demo", framework="langgraph")
                parent = get_logger("tools.lookup", component="search")
                parent.bind(request_id="req-1").info(
                    "lookup.completed", result_count=3
                )
                parent.info("lookup.cached")
        finally:
            root.removeHandler(capture)

        self.assertEqual([record.msg for record in capture.records], [
            "lookup.completed",
            "lookup.cached",
        ])
        self.assertEqual(capture.records[0].component, "search")
        self.assertEqual(capture.records[0].request_id, "req-1")
        self.assertEqual(capture.records[0].result_count, 3)
        self.assertFalse(hasattr(capture.records[1], "request_id"))

    def test_signal_specific_endpoints_do_not_enable_the_other_exporter(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
                "http://collector/v1/traces"
            )
            self.assertEqual(_exporter_name("traces"), "otlp")
            self.assertEqual(_exporter_name("logs"), "none")

        with patch.dict(os.environ, {}, clear=True):
            os.environ["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] = (
                "http://collector/v1/logs"
            )
            self.assertEqual(_exporter_name("traces"), "none")
            self.assertEqual(_exporter_name("logs"), "otlp")

    def test_manual_spans_and_logs_share_trace_context_and_export(self):
        spans = InMemorySpanExporter()
        logs = InMemoryLogRecordExporter()
        with patch.dict(
            os.environ,
            {
                "HARNEST_LOG_CONSOLE": "false",
                "HARNEST_OTEL_ENABLED": "false",
            },
        ):
            state = configure_observability(
                "demo",
                framework="langgraph",
                span_exporter=spans,
                log_exporter=logs,
                set_global_providers=False,
            )
            with span("tool.lookup", query={"kind": "docs"}):
                trace_id, span_id = current_trace_ids()
                get_logger("tools.lookup").info(
                    "lookup.completed", result_count=2
                )
            state.force_flush()

        finished_spans = spans.get_finished_spans()
        finished_logs = logs.get_finished_logs()
        self.assertEqual(len(finished_spans), 1)
        self.assertEqual(finished_spans[0].name, "tool.lookup")
        self.assertEqual(
            finished_spans[0].attributes["query"], '{"kind": "docs"}'
        )
        self.assertEqual(len(trace_id or ""), 32)
        self.assertEqual(len(span_id or ""), 16)
        self.assertEqual(len(finished_logs), 1)
        record = finished_logs[0].log_record
        self.assertEqual(record.body, "lookup.completed")
        self.assertEqual(record.attributes["result_count"], 2)
        self.assertEqual(record.trace_id, int(trace_id, 16))
        self.assertEqual(record.span_id, int(span_id, 16))

    def test_multiple_destinations_select_traces_and_logs_independently(self):
        first_spans = InMemorySpanExporter()
        second_spans = InMemorySpanExporter()
        second_logs = InMemoryLogRecordExporter()
        with patch.dict(
            os.environ,
            {
                "HARNEST_LOG_CONSOLE": "false",
                "HARNEST_OTEL_ENABLED": "false",
            },
        ):
            state = configure_observability(
                "demo",
                framework="langgraph",
                exporters=(
                    TelemetryExporter(name="trace-only", traces=first_spans),
                    TelemetryExporter(
                        name="combined",
                        traces=second_spans,
                        logs=second_logs,
                    ),
                ),
                set_global_providers=False,
            )
            with span("agent.work"):
                get_logger("agent").info("agent.completed")
            state.force_flush()

        self.assertEqual(
            [item.name for item in first_spans.get_finished_spans()],
            ["agent.work"],
        )
        self.assertEqual(
            [item.name for item in second_spans.get_finished_spans()],
            ["agent.work"],
        )
        self.assertEqual(len(second_logs.get_finished_logs()), 1)

    def test_runtime_factories_run_only_for_the_first_process_bootstrap(self):
        calls: list[str] = []
        first_spans = InMemorySpanExporter()

        def first():
            calls.append("first")
            return TelemetryExporter(name="first", traces=first_spans)

        def ignored():
            calls.append("ignored")
            return TelemetryExporter(
                name="ignored", traces=InMemorySpanExporter()
            )

        environment = {
            "HARNEST_LOG_CONSOLE": "false",
            "HARNEST_OTEL_ENABLED": "false",
        }
        with patch.dict(os.environ, environment):
            initial = configure_observability(
                "demo",
                framework="langgraph",
                exporter_factories=(
                    SimpleNamespace(callback=first, identity="first.py:1:first"),
                ),
                set_global_providers=False,
            )
            repeated = configure_observability(
                "demo",
                framework="langgraph",
                exporter_factories=(
                    SimpleNamespace(
                        callback=ignored, identity="ignored.py:1:ignored"
                    ),
                ),
                set_global_providers=False,
            )

        self.assertIs(repeated, initial)
        self.assertEqual(calls, ["first"])

    def test_adopted_providers_shut_down_only_added_processors(self):
        spans = _TrackingSpanExporter()
        logs = _TrackingLogExporter()
        tracer_provider = TracerProvider(shutdown_on_exit=False)
        logger_provider = LoggerProvider(shutdown_on_exit=False)
        environment = {
            "HARNEST_LOG_CONSOLE": "false",
            "HARNEST_OTEL_ENABLED": "false",
        }
        with (
            patch.dict(os.environ, environment),
            patch(
                "harnest.telemetry.trace.get_tracer_provider",
                return_value=tracer_provider,
            ),
            patch(
                "opentelemetry._logs.get_logger_provider",
                return_value=logger_provider,
            ),
        ):
            state = configure_observability(
                "demo",
                framework="adk",
                exporters=(
                    TelemetryExporter(name="adopted", traces=spans, logs=logs),
                ),
                use_global_providers=True,
                set_global_providers=False,
            )
            state.shutdown()

        self.assertFalse(state.owns_tracer_provider)
        self.assertFalse(state.owns_logger_provider)
        self.assertEqual(spans.shutdowns, 1)
        self.assertEqual(logs.shutdowns, 1)

    def test_provider_adoption_is_independent_per_signal(self):
        tracer_provider = TracerProvider(shutdown_on_exit=False)
        with (
            patch.dict(
                os.environ,
                {
                    "HARNEST_LOG_CONSOLE": "false",
                    "HARNEST_OTEL_ENABLED": "false",
                },
            ),
            patch(
                "harnest.telemetry.trace.get_tracer_provider",
                return_value=tracer_provider,
            ),
            patch(
                "opentelemetry._logs.get_logger_provider",
                return_value=object(),
            ),
        ):
            state = configure_observability(
                "demo",
                framework="adk",
                exporters=(
                    TelemetryExporter(
                        name="traces", traces=InMemorySpanExporter()
                    ),
                ),
                use_global_providers=True,
                set_global_providers=False,
            )

        self.assertFalse(state.owns_tracer_provider)
        self.assertTrue(state.owns_logger_provider)

    def test_destination_requires_name_signal_and_matching_sdk_type(self):
        spans = InMemorySpanExporter()
        with self.assertRaisesRegex(ValueError, "name"):
            TelemetryExporter(name="", traces=spans)
        with self.assertRaisesRegex(ValueError, "traces or logs"):
            TelemetryExporter(name="empty")
        with self.assertRaisesRegex(TypeError, "SpanExporter"):
            TelemetryExporter(name="wrong", traces=object())

    def test_traced_supports_sync_and_async_functions(self):
        spans = InMemorySpanExporter()
        with patch.dict(
            os.environ,
            {
                "HARNEST_LOG_CONSOLE": "false",
                "HARNEST_OTEL_ENABLED": "false",
            },
        ):
            state = configure_observability(
                "demo",
                framework="adk",
                span_exporter=spans,
                set_global_providers=False,
            )

            @traced("calculate.sync", component="test")
            def calculate(value: int) -> int:
                return value * 2

            @traced("calculate.async", component="test")
            async def calculate_async(value: int) -> int:
                return value + 1

            @traced("calculate.generator", component="test")
            def calculate_many():
                yield 1
                yield 2

            @traced("calculate.async_generator", component="test")
            async def calculate_many_async():
                yield 3
                yield 4

            self.assertEqual(calculate(3), 6)
            self.assertEqual(asyncio.run(calculate_async(3)), 4)
            self.assertEqual(list(calculate_many()), [1, 2])

            async def collect_async() -> list[int]:
                return [item async for item in calculate_many_async()]

            self.assertEqual(asyncio.run(collect_async()), [3, 4])
            state.force_flush()

        self.assertEqual(
            {item.name for item in spans.get_finished_spans()},
            {
                "calculate.sync",
                "calculate.async",
                "calculate.generator",
                "calculate.async_generator",
            },
        )


if __name__ == "__main__":
    unittest.main()

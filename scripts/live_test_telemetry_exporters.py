"""Live OTLP fan-out probe for one compiled Harnest agent."""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from harnest.bundle import compile_artifact
from harnest.runtime import run_agent_message


class _Receiver(ThreadingHTTPServer):
    """Capture decoded OTLP requests received over a real HTTP listener."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[Any] = []
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    """Decode the two OTLP signals exercised by this live probe."""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length", "0"))
        payload = self.rfile.read(length)
        request, response = _decode(self.path, payload)
        with self.server.lock:
            self.server.requests.append(request)
        body = response.SerializeToString()
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: Any) -> None:
        """Keep the successful live probe output concise."""


def _decode(path: str, payload: bytes) -> tuple[Any, Any]:
    """Decode one OTLP request and return its matching empty response."""

    if path == "/v1/traces":
        request = ExportTraceServiceRequest()
        request.ParseFromString(payload)
        return request, ExportTraceServiceResponse()
    if path == "/v1/logs":
        request = ExportLogsServiceRequest()
        request.ParseFromString(payload)
        return request, ExportLogsServiceResponse()
    raise AssertionError(f"unexpected OTLP path: {path}")


def _write_agent(root: Path) -> None:
    """Create a deterministic graph whose extensions fan out both signals."""

    files = {
        "agent.py": _AGENT,
        "instructions.md": "Reply deterministically.\n",
        "agent-card.yaml": (
            "name: OTLP live\ndescription: OTLP live probe\nversion: 0.1.0\n"
        ),
        "lib/storage.py": _STORAGE,
        "extensions/storage.py": _STORAGE_EXTENSION,
        "extensions/telemetry.py": _TELEMETRY_EXTENSION,
    }
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


def _start_receiver() -> tuple[_Receiver, threading.Thread]:
    """Start one local OTLP receiver on an ephemeral port."""

    receiver = _Receiver()
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    return receiver, thread


def _stop_receiver(receiver: _Receiver, thread: threading.Thread) -> None:
    """Stop one local receiver without leaking its listener thread."""

    receiver.shutdown()
    receiver.server_close()
    thread.join(timeout=5)


def _signal_records(receiver: _Receiver) -> tuple[list[Any], list[Any]]:
    """Split captured protobuf requests into trace and log records."""

    return _trace_records(receiver.requests), _log_records(receiver.requests)


def _trace_records(requests: list[Any]) -> list[Any]:
    """Flatten spans from captured OTLP trace requests."""

    return [
        span
        for request in requests
        if isinstance(request, ExportTraceServiceRequest)
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]


def _log_records(requests: list[Any]) -> list[Any]:
    """Flatten log records from captured OTLP log requests."""

    return [
        record
        for request in requests
        if isinstance(request, ExportLogsServiceRequest)
        for resource in request.resource_logs
        for scope in resource.scope_logs
        for record in scope.log_records
    ]


def _resource_attributes(request: Any) -> dict[str, str]:
    """Read string resource attributes from either OTLP request type."""

    resources = getattr(request, "resource_spans", None)
    if resources is None:
        resources = request.resource_logs
    return {
        attribute.key: attribute.value.string_value
        for resource in resources
        for attribute in resource.resource.attributes
    }


def _captured_resources(receiver: _Receiver) -> dict[str, str]:
    """Merge the identical resource identity attached to captured signals."""

    return {
        key: value
        for request in receiver.requests
        for key, value in _resource_attributes(request).items()
    }


def _records_by_body(logs: list[Any]) -> dict[str, Any]:
    """Index runtime log records by their stable event body."""

    return {record.body.string_value: record for record in logs}


def _assert_receiver(receiver: _Receiver, framework: str) -> None:
    """Require correlated runtime signals and their Harnest resource identity."""

    spans, logs = _signal_records(receiver)
    invocation = next(span for span in spans if span.name == "harnest.agent.invoke")
    bodies = _records_by_body(logs)
    resources = _captured_resources(receiver)
    assert "agent.invocation.started" in bodies
    assert "agent.invocation.completed" in bodies
    assert invocation.trace_id
    assert bodies["agent.invocation.started"].trace_id == invocation.trace_id
    assert bodies["agent.invocation.completed"].trace_id == invocation.trace_id
    assert resources["service.name"] == "otlp_live"
    assert resources["harnest.framework"] == framework


def run(framework: str) -> None:
    """Compile without endpoint values, then export one live run twice."""

    first, first_thread = _start_receiver()
    second, second_thread = _start_receiver()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent"
            artifact = Path(temporary) / "artifact"
            _write_agent(root)
            compile_artifact(root, artifact, framework=framework)
            environment = {
                "FIRST_OTLP": f"http://127.0.0.1:{first.server_port}",
                "SECOND_OTLP": f"http://127.0.0.1:{second.server_port}",
                "HARNEST_OTEL_ENABLED": "false",
                "HARNEST_LOG_CONSOLE": "false",
            }
            os.environ.update(environment)
            result = asyncio.run(run_agent_message(artifact, "live telemetry"))
            assert result["text"] == "received:live telemetry"
        _assert_receiver(first, framework)
        _assert_receiver(second, framework)
    finally:
        _stop_receiver(first, first_thread)
        _stop_receiver(second, second_thread)


def main() -> int:
    """Parse the isolated framework selection and run the live probe."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--framework", choices=("adk", "langgraph"), required=True)
    arguments = parser.parse_args()
    run(arguments.framework)
    print(f"live OTLP fan-out passed for {arguments.framework}")
    return 0


_AGENT = '''"""Deterministic graph for the live telemetry probe."""
from harnest import Edge, Event, Graph, START

def respond(value):
    return Event(output=f"received:{value}", message=f"received:{value}")

root_agent = Graph(
    name="otlp_live",
    nodes={"respond": respond},
    edges=(Edge(START, "respond"),),
)
'''

_STORAGE = """from harnest import MemoryStore\nstore = MemoryStore()\n"""

_STORAGE_EXTENSION = '''from harnest import lifecycle
from harnest.lib.storage import store

@lifecycle.session_store
def sessions():
    return store

@lifecycle.checkpointer
def checkpoints():
    return store
'''

_TELEMETRY_EXTENSION = '''import os
from harnest import TelemetryExporter, lifecycle
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

def destination(name, endpoint):
    return TelemetryExporter(
        name=name,
        traces=OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
        logs=OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"),
    )

@lifecycle.telemetry_exporter
def first():
    return destination("first", os.environ["FIRST_OTLP"])

@lifecycle.telemetry_exporter
def second():
    return destination("second", os.environ["SECOND_OTLP"])
'''


if __name__ == "__main__":
    raise SystemExit(main())

"""External Hatchet worker with deterministic live-test release control."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import unquote, urlsplit

from hatchet_sdk import Context, Hatchet
from pydantic import BaseModel


class ReportInput(BaseModel):
    """Small deterministic payload owned by the external worker."""

    topic: str


_LOCK = Lock()
_RELEASES: dict[str, Event] = {}
_EVIDENCE: dict[str, dict[str, Any]] = {}


def _correlation(context: Context) -> str:
    """Require the privacy-safe identifier supplied as Hatchet metadata."""

    value = context.additional_metadata.get("harnest.correlation_id")
    if not isinstance(value, str) or not value:
        raise ValueError("missing Harnest correlation metadata")
    return value


def _record(correlation_id: str, **values: Any) -> None:
    """Update bounded live-test evidence without persisting credentials or inputs."""

    with _LOCK:
        current = _EVIDENCE.setdefault(correlation_id, {})
        current.update(values)


def _release(correlation_id: str) -> None:
    """Release one known or soon-to-start job deterministically."""

    with _LOCK:
        gate = _RELEASES.setdefault(correlation_id, Event())
        gate.set()


def _gate(correlation_id: str) -> Event:
    """Return the stable gate shared by the task and HTTP control endpoint."""

    with _LOCK:
        return _RELEASES.setdefault(correlation_id, Event())


_HATCHET = Hatchet()


@_HATCHET.task(name="consumer-report", input_validator=ReportInput)
def consumer_report(job_input: ReportInput, context: Context) -> dict[str, str]:
    """Wait for explicit release so tests can observe suspension before completion."""

    correlation_id = _correlation(context)
    _record(
        correlation_id,
        state="started",
        workflow_run_id=context.workflow_run_id,
    )
    if not _gate(correlation_id).wait(timeout=120):
        _record(correlation_id, state="timed_out")
        raise TimeoutError("live-test release was not received")
    _record(correlation_id, state="completed")
    return {
        "report": f"external report for {job_input.topic}",
        "correlation_id": correlation_id,
    }


class _ControlHandler(BaseHTTPRequestHandler):
    """Expose only deterministic health, evidence, and release operations."""

    def do_GET(self) -> None:
        """Return health or one correlation-scoped evidence record."""

        path = urlsplit(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        correlation_id = _path_value(path, "/evidence/")
        if correlation_id is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        with _LOCK:
            evidence = dict(_EVIDENCE.get(correlation_id, {}))
        status = HTTPStatus.OK if evidence else HTTPStatus.NOT_FOUND
        self._json(status, evidence or {"error": "not_found"})

    def do_POST(self) -> None:
        """Release exactly one correlation identifier."""

        correlation_id = _path_value(urlsplit(self.path).path, "/release/")
        if correlation_id is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        _release(correlation_id)
        self._json(HTTPStatus.ACCEPTED, {"released": True})

    def log_message(self, format: str, *args: Any) -> None:
        """Keep request paths and correlation identifiers out of container logs."""

        del format, args

    def _json(self, status: HTTPStatus, document: dict[str, Any]) -> None:
        """Write one bounded JSON response for the local control contract."""

        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _path_value(path: str, prefix: str) -> str | None:
    """Decode a single non-empty path segment without accepting nested routes."""

    if not path.startswith(prefix):
        return None
    value = unquote(path[len(prefix) :])
    if not value or "/" in value:
        return None
    return value


def _start_control_server() -> None:
    """Run test control separately from Hatchet's blocking worker loop."""

    host = os.getenv("HARNEST_HATCHET_CONTROL_HOST", "127.0.0.1")
    port = int(os.getenv("HARNEST_HATCHET_CONTROL_PORT", "8099"))
    server = ThreadingHTTPServer((host, port), _ControlHandler)
    Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    _start_control_server()
    _HATCHET.worker(
        "harnest-external-fixture",
        workflows=[consumer_report],
    ).start()

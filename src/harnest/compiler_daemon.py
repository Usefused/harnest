"""Reload-only compiler process with a line-delimited JSON protocol."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, TextIO

from .bundle import compile_artifact


def _required_string(request: dict[str, Any], name: str) -> str:
    """Return one non-empty protocol string or reject the malformed request."""

    value = request.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"compiler request field {name!r} must be a non-empty string")
    return value


def _compile_request(request: dict[str, Any]) -> dict[str, Any]:
    """Compile one immutable generation from a validated daemon request."""

    request_id = _required_string(request, "id")
    cli_enabled = request.get("cliEnabled", False)
    if not isinstance(cli_enabled, bool):
        raise ValueError("compiler request field 'cliEnabled' must be a boolean")
    manifest = compile_artifact(
        Path(_required_string(request, "source")),
        Path(_required_string(request, "output")),
        entrypoint=_required_string(request, "entrypoint"),
        framework=_required_string(request, "framework"),
        mode=_required_string(request, "mode"),
        cli_enabled=cli_enabled,
    )
    return {"id": request_id, "ok": True, "digest": manifest["digest"]}


def _response(raw: str) -> dict[str, Any]:
    """Decode one request while preserving its identifier in error responses."""

    request_id = ""
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("compiler request must be a JSON object")
        candidate = request.get("id")
        if isinstance(candidate, str):
            request_id = candidate
        # Authored imports may print; reserve stdout exclusively for the protocol.
        with redirect_stdout(sys.stderr):
            return _compile_request(request)
    except Exception as exc:
        return {
            "id": request_id,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def serve(input_stream: TextIO, output_stream: TextIO) -> None:
    """Serve serial compile requests until the owning Go supervisor closes stdin."""

    for raw in input_stream:
        if not raw.strip():
            continue
        json.dump(_response(raw), output_stream, separators=(",", ":"))
        output_stream.write("\n")
        output_stream.flush()


def main() -> None:
    """Run the private reload compiler protocol on standard input and output."""

    serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()

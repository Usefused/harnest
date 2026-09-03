"""Bounded reader for Docker's non-TTY multiplexed execution stream."""

from __future__ import annotations

import struct
from typing import Any, Callable


def _read(raw: Any, count: int, check: Callable[[], None]) -> bytes:
    """Cap each raw read independently of the untrusted frame's advertised size."""
    check()
    reader = getattr(raw, "recv", None) or raw.read
    return reader(min(count, 65536))


def _exact(raw: Any, count: int, check: Callable[[], None]) -> bytes:
    """Read only the fixed-size framing header, rejecting partial EOF."""
    data = bytearray()
    while len(data) < count:
        chunk = _read(raw, count - len(data), check)
        if not chunk:
            if data:
                raise RuntimeError("sandbox output frame ended unexpectedly")
            return b""
        data.extend(chunk)
    return bytes(data)


def collect_output(
    raw: Any, limit: int, check: Callable[[], None], overflow: type[Exception],
) -> tuple[bytes, bytes]:
    """Accumulate at most the configured combined stdout/stderr byte budget."""
    streams = (bytearray(), bytearray())
    remaining = limit
    while True:
        header = _exact(raw, 8, check)
        if not header:
            return bytes(streams[0]), bytes(streams[1])
        channel, size = struct.unpack(">BxxxL", header)
        if channel not in (1, 2):
            raise RuntimeError("sandbox output contained an invalid stream")
        if size > remaining:
            raise overflow("sandbox output exceeded its byte budget")
        _read_frame(raw, streams[channel - 1], size, check)
        remaining -= size


def _read_frame(raw: Any, output: bytearray, size: int, check: Callable[[], None]) -> None:
    """Stream a validated frame with fixed-size reads and strict EOF checking."""
    while size:
        chunk = _read(raw, size, check)
        if not chunk:
            raise RuntimeError("sandbox output frame ended unexpectedly")
        output.extend(chunk)
        size -= len(chunk)

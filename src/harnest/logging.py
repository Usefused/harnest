"""Structured agent logging backed by Python logging and OpenTelemetry."""

from __future__ import annotations

import json
import logging as _logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


_RESERVED = frozenset(
    set(_logging.makeLogRecord({}).__dict__)
    | {"message", "asctime", "otelSpanID", "otelTraceID", "otelServiceName"}
)


def _attribute_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)) and all(
        item is None or isinstance(item, (str, int, float, bool)) for item in value
    ):
        return tuple(value)
    if isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _attributes(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError("log attribute names must be non-empty strings")
        normalized = key.strip()
        if normalized in _RESERVED:
            normalized = f"agent.{normalized}"
        result[normalized] = _attribute_value(value)
    return result


@dataclass(frozen=True, slots=True)
class Logger:
    """A small structured facade over an agent-scoped standard logger."""

    name: str
    _bound: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("logger name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(
            self, "_bound", MappingProxyType(_attributes(dict(self._bound)))
        )

    @property
    def stdlib(self) -> _logging.Logger:
        """Return the underlying standard-library logger."""

        return _logging.getLogger(f"harnest.agent.{self.name}")

    def bind(self, **attributes: Any) -> "Logger":
        """Return a logger carrying immutable attributes on every record."""

        return Logger(self.name, {**dict(self._bound), **attributes})

    def log(
        self,
        level: int,
        event: str,
        *args: Any,
        exc_info: Any = None,
        **attributes: Any,
    ) -> None:
        """Emit a structured event at an explicit standard logging level."""

        if not isinstance(event, str) or not event:
            raise ValueError("log event must be a non-empty string")
        extra = _attributes({**dict(self._bound), **attributes})
        self.stdlib.log(level, event, *args, extra=extra, exc_info=exc_info)

    def debug(self, event: str, *args: Any, **attributes: Any) -> None:
        """Emit a structured debug event."""

        self.log(_logging.DEBUG, event, *args, **attributes)

    def info(self, event: str, *args: Any, **attributes: Any) -> None:
        """Emit a structured informational event."""

        self.log(_logging.INFO, event, *args, **attributes)

    def warning(self, event: str, *args: Any, **attributes: Any) -> None:
        """Emit a structured warning event."""

        self.log(_logging.WARNING, event, *args, **attributes)

    def error(self, event: str, *args: Any, **attributes: Any) -> None:
        """Emit a structured error event."""

        self.log(_logging.ERROR, event, *args, **attributes)

    def critical(self, event: str, *args: Any, **attributes: Any) -> None:
        """Emit a structured critical event."""

        self.log(_logging.CRITICAL, event, *args, **attributes)

    def exception(self, event: str, *args: Any, **attributes: Any) -> None:
        """Emit an error event carrying the active exception traceback."""

        self.log(_logging.ERROR, event, *args, exc_info=True, **attributes)


def get_logger(name: str = "agent", **attributes: Any) -> Logger:
    """Create a structured logger in Harnest's agent logging namespace."""

    return Logger(name, attributes)


__all__ = ["Logger", "get_logger"]

"""Privacy-safe Docker extension lifecycle logs and traces."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from harnest.logging import get_logger
from harnest.tracing import get_tracer
from opentelemetry.trace import Status, StatusCode


_EXTENSION_NAME = "docker"
_EXTENSION_ATTRIBUTE = "harnest.extension.name"
_OPERATION_ATTRIBUTE = "harnest.extension.operation"
_LOGGER = get_logger(
    "extension.docker",
    **{_EXTENSION_ATTRIBUTE: _EXTENSION_NAME},
)
_TRACER = get_tracer("harnest.extension.docker")


@contextmanager
def docker_operation(
    operation: str,
    *,
    topology_id: str,
    attributes: Mapping[str, str | int | float | bool] | None = None,
) -> Iterator[Any]:
    """Correlate one lifecycle operation without recording Docker payloads."""

    values: dict[str, Any] = {
        _EXTENSION_ATTRIBUTE: _EXTENSION_NAME,
        _OPERATION_ATTRIBUTE: operation,
        "harnest.docker.topology_id": topology_id,
        **dict(attributes or {}),
    }
    event = f"docker.{operation}"
    with _TRACER.start_as_current_span(
        event,
        attributes=values,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        _LOGGER.info(f"{event}.started", **values)
        try:
            yield span
        except BaseException as error:
            # The exception type is operationally useful; provider messages can
            # contain daemon endpoints, image references, or command output.
            _LOGGER.error(
                f"{event}.failed",
                **values,
                **{"error.type": type(error).__name__},
            )
            span.set_attribute("error.type", type(error).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        else:
            _LOGGER.info(f"{event}.completed", **values)


__all__ = ["docker_operation"]

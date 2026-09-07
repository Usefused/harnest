"""Verify Docker lifecycle telemetry remains attributable and private."""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

import pytest

from harnest_extension_docker.lib import telemetry


class _Capture(logging.Handler):
    """Retain emitted extension records for structured-attribute assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Append one record without formatting provider-controlled values."""

        self.records.append(record)


def test_operation_logs_and_traces_include_extension_name() -> None:
    """Every lifecycle signal identifies Docker with the same stable field."""

    capture = _Capture()
    logger = logging.getLogger("harnest.agent.extension.docker")
    prior_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    span = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=span)
    context.__exit__ = Mock(return_value=False)
    try:
        with patch.object(
            telemetry._TRACER,
            "start_as_current_span",
            return_value=context,
        ) as start:
            with telemetry.docker_operation(
                "service.start",
                topology_id="topology-1",
                attributes={"harnest.docker.service.name": "api"},
            ):
                pass
    finally:
        logger.removeHandler(capture)
        logger.setLevel(prior_level)

    assert [record.getMessage() for record in capture.records] == [
        "docker.service.start.started",
        "docker.service.start.completed",
    ]
    for record in capture.records:
        assert record.__dict__["harnest.extension.name"] == "docker"
        assert record.__dict__["harnest.extension.operation"] == "service.start"
    assert start.call_args.args == ("docker.service.start",)
    assert start.call_args.kwargs["record_exception"] is False
    assert start.call_args.kwargs["set_status_on_exception"] is False
    assert start.call_args.kwargs["attributes"] == {
        "harnest.extension.name": "docker",
        "harnest.extension.operation": "service.start",
        "harnest.docker.topology_id": "topology-1",
        "harnest.docker.service.name": "api",
    }


def test_failed_operation_does_not_log_provider_message() -> None:
    """Failure logs retain only exception type, not Docker's raw diagnostic."""

    capture = _Capture()
    logger = logging.getLogger("harnest.agent.extension.docker")
    prior_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    span = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=span)
    context.__exit__ = Mock(return_value=False)
    try:
        with patch.object(
            telemetry._TRACER,
            "start_as_current_span",
            return_value=context,
        ):
            with pytest.raises(OSError, match="private daemon endpoint"):
                with telemetry.docker_operation(
                    "network.create", topology_id="topology-1"
                ):
                    raise OSError("private daemon endpoint")
    finally:
        logger.removeHandler(capture)
        logger.setLevel(prior_level)

    failed = capture.records[-1]
    assert failed.getMessage() == "docker.network.create.failed"
    assert failed.__dict__["harnest.extension.name"] == "docker"
    assert failed.__dict__["error.type"] == "OSError"
    assert "private daemon endpoint" not in failed.getMessage()
    span.set_attribute.assert_called_once_with("error.type", "OSError")
    assert span.set_status.call_args.args[0].status_code is telemetry.StatusCode.ERROR
    assert span.set_status.call_args.args[0].description is None

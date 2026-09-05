"""Adversarial Docker stream and lifecycle tests without a daemon."""

from __future__ import annotations

import io
import socket
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from harnest.sandbox import SandboxCancelledError, control
from harnest_extension_docker.lib.guard import (
    GuardedContainer,
    OutputLimitError,
    SandboxCleanupError,
)
from harnest_extension_docker.lib.socket_stream import collect_output


def _frame(channel: int, value: bytes) -> bytes:
    """Encode Docker's raw attach framing for deterministic transport tests."""

    return struct.pack(">BxxxL", channel, len(value)) + value


def _fake_container(raw: object) -> Mock:
    """Expose the minimal Docker API while retaining inspectable calls."""

    api = Mock(timeout=60)
    api.exec_create.return_value = {"Id": "owned-exec"}
    api.exec_start.side_effect = lambda *args, **kwargs: raw
    api.exec_inspect.return_value = {"ExitCode": 0}
    return Mock(id="owned-container", client=SimpleNamespace(api=api))


class _BlockedSocket:
    """Simulate an execution that never exits or produces another byte."""

    def __init__(self) -> None:
        """Expose blocking progress and termination to the test."""

        self.entered = threading.Event()
        self.closed = threading.Event()

    def read(self, _count: int) -> bytes:
        """Block until host cancellation closes the stream."""

        self.entered.set()
        self.closed.wait(5)
        return b""

    def close(self) -> None:
        """Release the blocked reader when host enforcement aborts."""

        self.closed.set()


def _guard(
    raw: object, *, timeout: float = 1, limit: int = 1024
) -> tuple[GuardedContainer, Mock]:
    """Create a guard without involving Docker initialization."""

    owner = SimpleNamespace(timeout_seconds=timeout, _container=None)
    native = _fake_container(raw)
    guarded = GuardedContainer(owner, native, limit)
    owner._container = guarded
    return guarded, native


def test_combined_output_limit_unicode_and_bounded_reads() -> None:
    """Share one byte budget while keeping each untrusted read bounded."""

    value = "hello 🌍".encode()
    raw = io.BytesIO(_frame(1, value) + _frame(2, b"err"))
    assert collect_output(
        raw, len(value) + 3, lambda: None, OutputLimitError
    ) == (value, b"err")
    payload = b"a" * 200000
    tracked = Mock(wraps=io.BytesIO(_frame(1, payload)))
    del tracked.recv
    assert collect_output(
        tracked, len(payload), lambda: None, OutputLimitError
    )[0] == payload
    assert max(call.args[0] for call in tracked.read.call_args_list) <= 65536


def test_malformed_or_oversized_frames_fail_before_unbounded_read() -> None:
    """Reject invalid framing and advertised sizes before reading payloads."""

    oversized = io.BytesIO(struct.pack(">BxxxL", 1, 2**32 - 1))
    with pytest.raises(OutputLimitError):
        collect_output(oversized, 1024, lambda: None, OutputLimitError)
    assert oversized.tell() == 8
    for value in (
        b"\x01",
        _frame(3, b"bad"),
        struct.pack(">BxxxL", 1, 3) + b"x",
    ):
        with pytest.raises(RuntimeError):
            collect_output(io.BytesIO(value), 100, lambda: None, OutputLimitError)


def test_host_deadline_terminates_silent_execution() -> None:
    """The host removes work that ignores its in-container deadline."""

    guarded, native = _guard(_BlockedSocket(), timeout=0.06)
    started = time.monotonic()
    result = guarded.exec_run(["native-command"], demux=True)
    assert result.exit_code == 124
    assert time.monotonic() - started < 0.5
    native.remove.assert_called_once_with(force=True, v=True)
    assert guarded.failed is True
    assert guarded.owner._container is None


def test_timeout_closes_a_real_blocked_socket_reader() -> None:
    """Closing the real socket interrupts the daemon reader thread."""

    left, right = socket.socketpair()
    raw = left.makefile("rb", buffering=0)
    guarded, native = _guard(raw, timeout=0.06)
    try:
        assert guarded.exec_run(["native-command"], demux=True).exit_code == 124
        assert right.recv(1) == b""
        native.remove.assert_called_once_with(force=True, v=True)
    finally:
        left.close()
        right.close()


def test_late_create_response_cannot_start_cancelled_work() -> None:
    """An exec ID returned after timeout cannot authorize a later start."""

    guarded, native = _guard(io.BytesIO(), timeout=0.06)
    release = threading.Event()

    def delayed_create(*_args: object, **_kwargs: object) -> dict[str, str]:
        """Return only after the host deadline has elapsed."""

        release.wait(2)
        return {"Id": "late-exec"}

    native.client.api.exec_create.side_effect = delayed_create
    assert guarded.exec_run(["native-command"]).exit_code == 124
    release.set()
    time.sleep(0.02)
    native.client.api.exec_start.assert_not_called()


def test_output_overflow_removes_owned_container() -> None:
    """Exceeded output becomes a bounded result after terminating work."""

    guarded, native = _guard(io.BytesIO(_frame(1, b"a" * 100)), limit=16)
    result = guarded.exec_run(["native-command"], demux=True)
    assert result.exit_code != 0
    assert b"max_output_bytes" in result.output[1]
    native.remove.assert_called_once_with(force=True, v=True)


def test_success_quiesces_then_restarts_retained_container() -> None:
    """Retained files survive while detached child processes do not."""

    guarded, native = _guard(io.BytesIO(_frame(1, b"first")))
    native.client.api.exec_start.side_effect = [
        io.BytesIO(_frame(1, b"first")),
        io.BytesIO(_frame(1, b"second")),
    ]
    assert guarded.exec_run(["first"], demux=True).output[0] == b"first"
    assert guarded.exec_run(["second"], demux=True).output[0] == b"second"
    native.start.assert_called_once_with()
    assert native.stop.call_count == 2
    native.remove.assert_not_called()


def test_failed_quiescence_removes_and_poisons_executor() -> None:
    """A container with possibly live work cannot be reused."""

    guarded, native = _guard(io.BytesIO(_frame(1, b"done")))
    native.stop.side_effect = OSError("daemon stop failed")
    with pytest.raises(OSError, match="stop failed"):
        guarded.exec_run(["native-command"], demux=True)
    assert guarded.failed is True
    native.remove.assert_called_once_with(force=True, v=True)
    with pytest.raises(RuntimeError, match="cannot be reused"):
        guarded.exec_run(["later"], demux=True)


def test_cancellation_propagates_after_cleanup() -> None:
    """Cancellation removes owned work and remains visible to the caller."""

    guarded, native = _guard(_BlockedSocket())
    checks = 0

    with control.execute(30) as current:
        original_check = current.check

        def check() -> None:
            """Revoke admission only after the execution begins."""

            nonlocal checks
            checks += 1
            if checks > 5:
                current.cancelled.set()
            original_check()

        with patch.object(current, "check", side_effect=check):
            with pytest.raises(SandboxCancelledError):
                guarded.exec_run(["native-command"], demux=True)
    native.remove.assert_called_once_with(force=True, v=True)


def test_uncertain_cleanup_retains_handle_for_retry() -> None:
    """Removal uncertainty never loses the exact poisoned container owner."""

    guarded, native = _guard(io.BytesIO(_frame(1, b"oversized")), limit=1)
    native.remove.side_effect = OSError("private daemon detail")
    with pytest.raises(SandboxCleanupError) as caught:
        guarded.exec_run(["native-command"], demux=True)
    assert caught.value.failed_executor is guarded.owner
    assert "private" not in str(caught.value)
    assert guarded.removed is False
    native.remove.side_effect = None
    guarded.close()
    guarded.close()
    assert native.remove.call_count == 2

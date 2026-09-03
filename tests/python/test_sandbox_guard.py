"""Adversarial transport and native startup tests without a Docker daemon."""

import io
import os
import socket
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harnest.sandbox_guard import (
    GuardedContainer, OutputLimitError, SandboxCleanupError,
    close_guarded_executor, create_guarded_executor, guard_failed,
)
from harnest.sandbox_socket import collect_output


def frame(channel, value):
    """Encode Docker's raw attach framing for deterministic transport tests."""
    return struct.pack(">BxxxL", channel, len(value)) + value


def fake_container(raw):
    """Expose the minimal native Docker API while retaining inspectable calls."""
    api = Mock(timeout=60)
    api.exec_create.return_value = {"Id": "owned-exec"}
    api.exec_start.side_effect = lambda *args, **kwargs: raw
    api.exec_inspect.return_value = {"ExitCode": 0}
    return Mock(id="owned-container", client=SimpleNamespace(api=api))


class BlockedSocket:
    """Simulate a supervisor that never exits or produces another byte."""

    def __init__(self):
        """Expose blocking progress and termination to the test."""
        self.entered, self.closed = threading.Event(), threading.Event()

    def read(self, count):
        """Remain blocked regardless of what the in-container supervisor does."""
        self.entered.set()
        self.closed.wait(5)
        return b""

    def close(self):
        """Let host cancellation release the daemon reader."""
        self.closed.set()


class SandboxSocketTests(unittest.TestCase):
    def test_combined_limit_and_unicode(self):
        value = "hello 🌍".encode()
        raw = io.BytesIO(frame(1, value) + frame(2, b"err"))
        self.assertEqual(collect_output(raw, len(value) + 3, lambda: None, OutputLimitError), (value, b"err"))

    def test_large_frame_rejected_before_payload_read(self):
        raw = io.BytesIO(struct.pack(">BxxxL", 1, 2**32 - 1))
        with self.assertRaises(OutputLimitError):
            collect_output(raw, 1024, lambda: None, OutputLimitError)
        self.assertEqual(raw.tell(), 8)

    def test_reads_are_bounded_independently_of_frame_size(self):
        payload = b"a" * 200000
        raw = Mock(wraps=io.BytesIO(frame(1, payload)))
        del raw.recv
        self.assertEqual(collect_output(raw, len(payload), lambda: None, OutputLimitError)[0], payload)
        self.assertLessEqual(max(call.args[0] for call in raw.read.call_args_list), 65536)

    def test_both_streams_share_one_budget(self):
        raw = io.BytesIO(frame(1, b"abcd") + frame(2, b"efgh"))
        with self.assertRaises(OutputLimitError):
            collect_output(raw, 7, lambda: None, OutputLimitError)

    def test_truncated_and_invalid_frames_fail_closed(self):
        for value in (b"\x01", frame(3, b"bad"), struct.pack(">BxxxL", 1, 3) + b"x"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                collect_output(io.BytesIO(value), 100, lambda: None, OutputLimitError)


class SandboxGuardTests(unittest.TestCase):
    def test_native_validation_failure_is_not_masked_by_cleanup(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            create_guarded_executor({"image": "python:3.12", "timeout_seconds": -1}, 1024)

    def make_guard(self, raw, timeout=1, limit=1024):
        """Create a guard without involving native initialization."""
        owner = SimpleNamespace(timeout_seconds=timeout, _container=None)
        native = fake_container(raw)
        guard = GuardedContainer(owner, native, limit)
        owner._container = guard
        return guard, native

    def test_host_deadline_terminates_silent_supervisor(self):
        guard, native = self.make_guard(BlockedSocket(), timeout=0.06)
        started = time.monotonic()
        result = guard.exec_run(["native-command"], demux=True)
        self.assertEqual(result.exit_code, 124)
        self.assertLess(time.monotonic() - started, 0.5)
        native.remove.assert_called_once_with(force=True, v=True)
        self.assertTrue(guard.failed)
        self.assertIsNone(guard.owner._container)

    def test_timeout_releases_a_real_blocked_socket_reader(self):
        left, right = socket.socketpair()
        raw = left.makefile("rb", buffering=0)
        guard, native = self.make_guard(raw, timeout=0.06)
        try:
            self.assertEqual(guard.exec_run(["native-command"], demux=True).exit_code, 124)
            self.assertEqual(right.recv(1), b"")
            native.remove.assert_called_once_with(force=True, v=True)
        finally:
            left.close()
            right.close()

    def test_late_exec_create_response_cannot_start_cancelled_work(self):
        guard, native = self.make_guard(io.BytesIO(), timeout=0.06)
        release = threading.Event()

        def delayed_create(*args, **kwargs):
            """Return an exec ID only after its caller has already timed out."""
            release.wait(2)
            return {"Id": "late-exec"}

        native.client.api.exec_create.side_effect = delayed_create
        self.assertEqual(guard.exec_run(["native-command"]).exit_code, 124)
        release.set()
        time.sleep(0.02)
        native.client.api.exec_start.assert_not_called()

    def test_output_overflow_terminates_owned_container(self):
        guard, native = self.make_guard(io.BytesIO(frame(1, b"a" * 100)), limit=16)
        result = guard.exec_run(["native-command"], demux=True)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(b"max_output_bytes", result.output[1])
        native.remove.assert_called_once_with(force=True, v=True)

    def test_success_keeps_native_container_for_filesystem_reuse(self):
        guard, native = self.make_guard(io.BytesIO(frame(1, b"ok")))
        result = guard.exec_run(["native-command"], demux=True)
        self.assertEqual(result.output, (b"ok", b""))
        native.remove.assert_not_called()
        native.stop.assert_called_once_with(timeout=0)
        self.assertFalse(guard.failed)

    def test_next_execution_restarts_same_container_after_quiescence(self):
        guard, native = self.make_guard(io.BytesIO(frame(1, b"first")))
        native.client.api.exec_start.side_effect = [
            io.BytesIO(frame(1, b"first")), io.BytesIO(frame(1, b"second")),
        ]
        self.assertEqual(guard.exec_run(["first"], demux=True).output[0], b"first")
        self.assertTrue(guard.stopped)
        self.assertEqual(guard.exec_run(["second"], demux=True).output[0], b"second")
        native.start.assert_called_once_with()
        self.assertEqual(native.stop.call_count, 2)
        native.remove.assert_not_called()

    def test_failed_quiescence_removes_and_poisons_executor(self):
        guard, native = self.make_guard(io.BytesIO(frame(1, b"done")))
        native.stop.side_effect = OSError("daemon stop failed")
        with self.assertRaises(OSError):
            guard.exec_run(["native-command"], demux=True)
        self.assertTrue(guard.failed)
        native.remove.assert_called_once_with(force=True, v=True)
        with self.assertRaisesRegex(RuntimeError, "cannot be reused"):
            guard.exec_run(["later"], demux=True)

    def test_cancellation_propagates_after_cleanup(self):
        from harnest.sandbox_control import SandboxCancelledError

        guard, native = self.make_guard(BlockedSocket())
        control = Mock()
        checks = 0

        def check():
            """Revoke admission once execution has begun."""
            nonlocal checks
            checks += 1
            if checks > 5:
                raise SandboxCancelledError("sandbox execution cancelled")

        control.check.side_effect = check
        with patch("harnest.sandbox_guard.current_control", return_value=control):
            with self.assertRaises(SandboxCancelledError):
                guard.exec_run(["native-command"], demux=True)
        native.remove.assert_called_once_with(force=True, v=True)

    def test_uncertain_cleanup_keeps_poisoned_handle_and_can_retry(self):
        guard, native = self.make_guard(io.BytesIO(frame(1, b"oversized")), limit=1)
        native.remove.side_effect = OSError("private daemon detail")
        with self.assertRaises(SandboxCleanupError) as caught:
            guard.exec_run(["native-command"], demux=True)
        self.assertIs(caught.exception.failed_executor, guard.owner)
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(guard.failed)
        self.assertFalse(guard.removed)
        self.assertEqual(native.client.api.timeout, 60)
        native.remove.side_effect = None
        guard.close()
        guard.close()
        self.assertEqual(native.remove.call_count, 2)

    def test_native_constructor_verification_failure_is_cleaned(self):
        native = fake_container(io.BytesIO(frame(2, b"missing")))
        native.client.api.exec_inspect.return_value = {"ExitCode": 1}
        client = Mock()
        client.containers.create.return_value = native
        with patch("docker.from_env", return_value=client):
            with self.assertRaisesRegex(ValueError, "python3"):
                create_guarded_executor({"image": "python:3.12"}, 1024)
        native.remove.assert_called_once_with(force=True, v=True)
        client.close.assert_called_once()

    def test_native_constructor_cleanup_failure_retains_executor(self):
        native = fake_container(io.BytesIO())
        native.client.api.exec_inspect.return_value = {"ExitCode": 1}
        native.remove.side_effect = OSError("daemon offline")
        client = Mock()
        client.containers.create.return_value = native
        with patch("docker.from_env", return_value=client):
            with self.assertRaises(SandboxCleanupError) as caught:
                create_guarded_executor({"image": "python:3.12"}, 1024)
        executor = caught.exception.failed_executor
        self.assertTrue(guard_failed(executor))
        native.remove.side_effect = None
        close_guarded_executor(executor)
        self.assertEqual(native.remove.call_count, 2)

    def test_native_start_failure_cleans_created_container(self):
        native = fake_container(io.BytesIO())
        native.start.side_effect = RuntimeError("image start failed")
        client = Mock()
        client.containers.create.return_value = native
        with patch("docker.from_env", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "image start failed"):
                create_guarded_executor({"image": "python:3.12"}, 1024)
        native.remove.assert_called_once_with(force=True, v=True)
        client.close.assert_called_once()

    def test_unknown_create_outcome_refuses_automatic_replacement(self):
        client = Mock()
        client.containers.create.side_effect = OSError("create response lost")
        with patch("docker.from_env", return_value=client):
            with self.assertRaises(SandboxCleanupError) as caught:
                create_guarded_executor({"image": "python:3.12"}, 1024)
        executor = caught.exception.failed_executor
        self.assertTrue(guard_failed(executor))
        self.assertIsNone(executor._container)
        with self.assertRaises(SandboxCleanupError):
            close_guarded_executor(executor)
        client.close.assert_called_once()

    def test_native_missing_image_pull_fallback_still_works(self):
        from docker.errors import ImageNotFound

        native = fake_container(io.BytesIO(frame(1, b"/bin/python3")))
        client = Mock()
        client.containers.client = client
        client.containers.create.side_effect = [ImageNotFound("not found"), native]
        with patch("docker.from_env", return_value=client):
            executor = create_guarded_executor({"image": "python:3.12"}, 1024)
        try:
            client.images.pull.assert_called_once_with("python:3.12", platform=None)
            self.assertEqual(client.containers.create.call_count, 2)
            self.assertFalse(guard_failed(executor))
        finally:
            close_guarded_executor(executor)

    def test_native_start_failure_retains_uncertain_cleanup_handle(self):
        native = fake_container(io.BytesIO())
        native.start.side_effect = RuntimeError("image start failed")
        native.remove.side_effect = OSError("daemon unavailable")
        client = Mock()
        client.containers.create.return_value = native
        with patch("docker.from_env", return_value=client):
            with self.assertRaises(SandboxCleanupError) as caught:
                create_guarded_executor({"image": "python:3.12"}, 1024)
        executor = caught.exception.failed_executor
        self.assertIs(executor._container, native)
        self.assertTrue(guard_failed(executor))
        native.remove.side_effect = None
        close_guarded_executor(executor)
        self.assertEqual(native.remove.call_count, 2)

    def test_expired_create_response_cannot_start_new_container(self):
        from harnest.sandbox_control import execution_control

        native = fake_container(io.BytesIO())
        client = Mock()
        with execution_control(None) as control:
            def revoked_create(*args, **kwargs):
                """Make authority expire while Docker allocates the owned resource."""
                control.cancelled.set()
                return native

            client.containers.create.side_effect = revoked_create
            with patch("docker.from_env", return_value=client):
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    create_guarded_executor({"image": "python:3.12"}, 1024)
        native.start.assert_not_called()
        native.remove.assert_called_once_with(force=True, v=True)

    def test_dockerfile_build_and_native_security_settings_are_preserved(self):
        native = fake_container(io.BytesIO(frame(1, b"/bin/python3")))
        client = Mock()
        client.containers.create.return_value = native
        with patch("docker.from_env", return_value=client):
            executor = create_guarded_executor({"docker_path": "."}, 1024)
        try:
            client.images.build.assert_called_once_with(
                path=os.path.abspath("."), tag="adk-code-executor:latest", rm=True,
            )
            self.assertEqual(client.containers.create.call_args.kwargs, {
                "image": "adk-code-executor:latest", "command": None,
                "detach": True, "tty": True,
                "network_disabled": True, "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges"],
            })
        finally:
            executor._ContainerCodeExecutor__cleanup_container()
            executor._ContainerCodeExecutor__cleanup_container()
        native.remove.assert_called_once_with(force=True, v=True)

    def test_native_wrapper_and_provider_options_are_preserved(self):
        from google.adk.code_executors.code_execution_utils import CodeExecutionInput
        from google.adk.code_executors.container_code_executor import _TIMEOUT_WRAPPER

        native = fake_container(None)
        native.client.api.exec_start.side_effect = [
            io.BytesIO(frame(1, b"/bin/python3")), io.BytesIO(frame(1, b"42\n")),
        ]
        client = Mock()
        client.containers.create.return_value = native
        with patch("docker.DockerClient", return_value=client) as factory:
            executor = create_guarded_executor({
                "image": "python:3.12", "base_url": "unix:///test.sock",
                "network_enabled": True, "timeout_seconds": 2,
                "error_retry_attempts": 7, "code_block_delimiters": [("<py>", "</py>")],
            }, 1024)
        try:
            result = executor.execute_code(None, CodeExecutionInput(code="print(42)"))
            self.assertEqual(result.stdout, "42\n")
            self.assertEqual(executor.error_retry_attempts, 7)
            self.assertEqual(executor.code_block_delimiters, [("<py>", "</py>")])
            self.assertFalse(guard_failed(executor))
            factory.assert_called_once_with(base_url="unix:///test.sock")
            self.assertFalse(client.containers.create.call_args.kwargs["network_disabled"])
            command = native.client.api.exec_create.call_args.args[1]
            self.assertEqual(command, ["python3", "-c", _TIMEOUT_WRAPPER, "2", "print(42)"])
        finally:
            close_guarded_executor(executor)
            close_guarded_executor(executor)
        native.remove.assert_called_once_with(force=True, v=True)
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

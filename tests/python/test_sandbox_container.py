"""Test lazy native Docker delegation without connecting to a Docker daemon."""

import concurrent.futures
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harnest.sandbox_container import create_container_backend
from harnest.sandbox_types import SandboxContext, SandboxFile, SandboxRequest


def _executor():
    """Return native-shaped execution output without acquiring external resources."""
    executor = MagicMock()
    executor._guard = None
    executor._guard_poisoned = False
    executor.execute_code.return_value = SimpleNamespace(stdout="done", stderr="")
    return executor


class ContainerSandboxTests(unittest.TestCase):
    """Retain framework-owned lifecycle and provider configuration across calls."""

    def test_lazy_construction_reuses_native_executor_and_native_lifecycle(self):
        """Reuse successful guarded execution without adding session-specific lifecycle."""
        executor = _executor()
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=executor) as native:
            backend = create_container_backend(image="python:3.12-slim")
            native.assert_not_called()
            for user in ("one", "two", "two"):
                result = backend.execute(SandboxRequest("print('done')", context=SandboxContext(user_id=user, session_id="s")))
                self.assertEqual(result.stdout, "done")
        native.assert_called_once_with(dict(
            image="python:3.12-slim", docker_path=None, base_url=None,
            network_enabled=False, timeout_seconds=300,
        ), 1_048_576)
        self.assertEqual(executor.execute_code.call_count, 3)
        executor.close.assert_not_called()
        executor.remove.assert_not_called()
        context, source = executor.execute_code.call_args.args
        self.assertIsNone(context)
        self.assertEqual(source.code, "print('done')")

    def test_concurrent_calls_initialize_native_backend_once(self):
        """Serialize native ownership when multiple calls begin concurrently."""
        executor = _executor()
        backend = create_container_backend(image="python")
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=executor) as native:
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                futures = [pool.submit(backend.execute, SandboxRequest("print(1)")) for _ in range(8)]
                self.assertEqual([future.result(timeout=5).stdout for future in futures], ["done"] * 8)
        native.assert_called_once()

    def test_new_backend_preserves_configuration_without_sharing_native_executor(self):
        """Give adapters independent native ownership with identical provider settings."""
        provider = create_container_backend(image="python", options={"error_retry_attempts": 3})
        executors = [_executor(), _executor()]
        with patch("harnest.sandbox_container.create_guarded_executor", side_effect=executors) as native:
            first = provider.new_backend()
            native.assert_not_called()
            first.execute(SandboxRequest("print(1)"))
            second = first.new_backend()
            self.assertEqual(native.call_count, 1)
            second.execute(SandboxRequest("print(2)"))
            first.execute(SandboxRequest("print(3)"))
        self.assertEqual(native.call_count, 2)
        self.assertEqual(native.call_args_list[0], native.call_args_list[1])
        self.assertEqual(executors[0].execute_code.call_count, 2)
        self.assertEqual(executors[1].execute_code.call_count, 1)

    def test_guard_receives_output_cap_without_inventing_native_resource_options(self):
        """Keep host output policy separate from native Docker configuration."""
        executor = _executor()
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=executor) as guard:
            backend = create_container_backend(image="python", timeout_seconds=7, max_output_bytes=4096)
            self.assertEqual(backend.execute(SandboxRequest("print(1)")).stdout, "done")
        config, output_cap = guard.call_args.args
        self.assertEqual(output_cap, 4096)
        self.assertEqual(config["timeout_seconds"], 7)
        self.assertNotIn("max_output_bytes", config)
        self.assertNotIn("cpu_limit", config)
        self.assertNotIn("memory_limit", config)
        executor.close.assert_not_called()

    def test_native_provider_options_are_preserved_as_snapshot(self):
        """Pass frozen author configuration through the host guard to native ADK."""
        options = {"error_retry_attempts": 4, "code_block_delimiters": [("start", "end")]}
        backend = create_container_backend(
            docker_path="custom", base_url="unix:///test.sock", network=True,
            timeout_seconds=8, options=options,
        )
        options["error_retry_attempts"] = 99
        options["code_block_delimiters"].append(("other", "block"))
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=_executor()) as native:
            backend.execute(SandboxRequest("print(1)"))
        native.assert_called_once_with(dict(
            docker_path="custom", base_url="unix:///test.sock",
            network_enabled=True, timeout_seconds=8, error_retry_attempts=4,
            code_block_delimiters=[("start", "end")],
        ), 1_048_576)

    def test_native_dockerfile_build_preserves_framework_default_image_selection(self):
        """Omit an absent image instead of overriding ADK's Dockerfile image tag."""
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=_executor()) as native:
            result = create_container_backend(docker_path=".").execute(SandboxRequest("print(1)"))
        self.assertEqual(result.stdout, "done")
        self.assertEqual(native.call_args.args[0]["docker_path"], ".")
        self.assertNotIn("image", native.call_args.args[0])

    def test_native_deadline_is_lowered_and_restored_after_success_or_failure(self):
        """Do not leak a shorter request deadline into later successful executions."""
        executor = _executor()
        deadlines = []

        def execute(context, source):
            """Record the actual native deadline at execution time."""
            deadlines.append(executor.timeout_seconds)
            if source.code == "fail":
                raise RuntimeError("native failure")
            return SimpleNamespace(stdout="done", stderr="")

        executor.execute_code.side_effect = execute
        backend = create_container_backend(image="python", timeout_seconds=8)
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=executor) as native:
            backend.execute(SandboxRequest("print(1)", timeout_seconds=3))
            backend.execute(SandboxRequest("print(2)", timeout_seconds=80))
            with self.assertRaisesRegex(RuntimeError, "native failure"):
                backend.execute(SandboxRequest("fail", timeout_seconds=1))
            self.assertEqual(executor.timeout_seconds, 8)
            backend.execute(SandboxRequest("print(3)"))
        self.assertEqual(deadlines, [3, 8, 1, 8])
        native.assert_called_once()
        executor.close.assert_not_called()

    def test_failed_native_construction_can_retry_without_retaining_partial_backend(self):
        """Retry a factory failure when no owned resources need further cleanup."""
        backend = create_container_backend(image="python")
        executor = _executor()
        with patch("harnest.sandbox_container.create_guarded_executor", side_effect=[RuntimeError("start failed"), executor]) as native:
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                backend.execute(SandboxRequest("print(1)"))
            self.assertEqual(backend.execute(SandboxRequest("print(2)")).stdout, "done")
        self.assertEqual(native.call_count, 2)

    def test_unsupported_files_fail_before_native_construction(self):
        """Reject unsupported inputs before any Docker resources are acquired."""
        with patch("harnest.sandbox_container.create_guarded_executor") as native, self.assertRaisesRegex(ValueError, "input_files"):
            create_container_backend(image="python").execute(SandboxRequest("print(1)", input_files=(SandboxFile("a", b"one"),)))
        native.assert_not_called()

    def test_options_reject_ignored_native_fields_and_unsupported_state(self):
        for option in ("cpu_limit", "memory_limit", "pids_limit", "volumes", "network_enabled", "unknown"):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, "unsupported"):
                create_container_backend(image="python", options={option: True})
        for option in ("stateful", "optimize_data_file"):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, "remain False"):
                create_container_backend(image="python", options={option: True})

    def test_native_configuration_validation(self):
        for values in ({}, {"image": "one", "docker_path": "two"}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "exactly one"):
                create_container_backend(**values)
        for timeout in (None, 0, -1, True):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "timeout_seconds"):
                create_container_backend(image="python", timeout_seconds=timeout)
        with self.assertRaisesRegex(TypeError, "network"):
            create_container_backend(image="python", network="yes")

    def test_output_budget_rejects_unbounded_or_non_integer_values(self):
        """Require a finite positive integer for the streaming allocation budget."""
        for value in (None, 0, -1, True, 1.5, "1024"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_output_bytes"):
                create_container_backend(image="python", max_output_bytes=value)

    def test_aborted_executor_is_retired_before_successful_recovery(self):
        """Never reuse a container whose guard terminated an unsafe execution."""
        first, replacement = _executor(), _executor()

        def abort(context, source):
            """Represent the guard's poisoned-state result without using Docker."""
            first._guard_poisoned = True
            return SimpleNamespace(stdout="", stderr="Sandbox output exceeded max_output_bytes")

        first.execute_code.side_effect = abort
        backend = create_container_backend(image="python")
        with patch("harnest.sandbox_container.create_guarded_executor", side_effect=[first, replacement]) as factory:
            with patch("harnest.sandbox_container.close_guarded_executor") as cleanup:
                self.assertIn("output", backend.execute(SandboxRequest("large output")).stderr)
                self.assertIsNone(backend._executor)
                cleanup.assert_called_once_with(first)
                self.assertEqual(backend.execute(SandboxRequest("safe")).stdout, "done")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(first.execute_code.call_count, 1)

    def test_uncertain_cleanup_retains_poisoned_owner_until_retry_succeeds(self):
        """A failed removal must not lose ownership or start a replacement early."""
        first, replacement = _executor(), _executor()
        first._guard_poisoned = True
        backend = create_container_backend(image="python")
        backend._executor = first
        with patch("harnest.sandbox_container.create_guarded_executor", return_value=replacement) as factory:
            with patch("harnest.sandbox_container.close_guarded_executor", side_effect=[RuntimeError("cleanup failed"), None]):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    backend.execute(SandboxRequest("must not execute"))
                self.assertIs(backend._executor, first)
                factory.assert_not_called()
                self.assertEqual(backend.execute(SandboxRequest("safe")).stdout, "done")
        first.execute_code.assert_not_called()
        factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()

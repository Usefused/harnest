"""Exercise scoped Docker ownership and hard policy without requiring a daemon."""

import concurrent.futures
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from harnest.sandbox import Sandbox, SandboxBudget, SandboxContext, SandboxRequest, SandboxResult, SandboxStatus
from harnest.sandbox_container import create_container_backend


def executor():
    """Represent one exact owned container with explicit terminal status."""
    return SimpleNamespace(_guard=None, _guard_poisoned=False,
                           execute=Mock(return_value=SandboxResult(stdout="done", exit_code=0)))


def request(user="alice", session="first", agent="worker", invocation="turn-1"):
    """Make identity collisions visible by varying one scope component at a time."""
    return SandboxRequest("print(1)", context=SandboxContext(agent, invocation, user, session))


class ContainerSandboxTests(unittest.TestCase):
    def test_default_execution_scope_destroys_every_container(self):
        """Successful executions cannot leave files for a subsequent user or call."""
        owners = [executor() for _ in range(3)]
        with patch("harnest.sandbox_container.create_docker_executor", side_effect=owners) as create, patch("harnest.sandbox_container.close_guarded_executor") as close:
            backend = create_container_backend(image="python")
            create.assert_not_called()
            for _ in owners:
                self.assertEqual(backend.execute(request()).status, SandboxStatus.SUCCEEDED)
        self.assertEqual(create.call_count, 3)
        self.assertEqual([call.args[0] for call in close.call_args_list], owners)

    def test_session_scope_never_crosses_user_session_or_agent(self):
        """A session name alone cannot authorize reuse of another tenant's container."""
        owners = [executor() for _ in range(4)]
        with patch("harnest.sandbox_container.create_docker_executor", side_effect=owners) as create, patch("harnest.sandbox_container.close_guarded_executor") as close:
            backend = create_container_backend(image="python", scope="session")
            for value in (request(), request(), request(user="bob"), request(session="second"), request(agent="child")):
                backend.execute(value)
            self.assertEqual(create.call_count, 4)
            self.assertEqual(owners[0].execute.call_count, 2)
            backend.close()
            self.assertEqual(close.call_count, 4)

    def test_invocation_scope_and_missing_identity_fail_closed(self):
        """Retained execution scope includes turn identity and rejects anonymous access."""
        with patch("harnest.sandbox_container.create_docker_executor", side_effect=lambda *_: executor()) as create, patch("harnest.sandbox_container.close_guarded_executor"):
            backend = create_container_backend(image="python", scope="invocation")
            backend.execute(request())
            backend.execute(request(invocation="turn-2"))
            with self.assertRaisesRegex(ValueError, "identity"):
                backend.execute(SandboxRequest("print(1)"))
            self.assertEqual(create.call_count, 2)

    def test_retained_container_budget_evicts_only_after_cleanup(self):
        """LRU admission cannot exceed max_scopes or forget uncertain cleanup."""
        first, second = executor(), executor()
        with patch("harnest.sandbox_container.create_docker_executor", side_effect=[first, second]) as create, patch("harnest.sandbox_container.close_guarded_executor", side_effect=[RuntimeError("cleanup"), None]):
            backend = create_container_backend(image="python", scope="session", max_scopes=1)
            backend.execute(request())
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                backend.execute(request(user="bob"))
            self.assertEqual(create.call_count, 1)
            backend.execute(request(user="bob"))
            self.assertEqual(create.call_count, 2)

    def test_failed_cleanup_keeps_owner_and_blocks_replacement(self):
        """The default fresh-container policy cannot leak an old owner on failure."""
        first = executor()
        with patch("harnest.sandbox_container.create_docker_executor", return_value=first) as create, patch("harnest.sandbox_container.close_guarded_executor", side_effect=RuntimeError("cleanup")):
            backend = create_container_backend(image="python")
            # Real cleanup poisons the owner before attempting removal.
            first._guard_poisoned = True
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                backend.execute(request())
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                backend.execute(request())
            self.assertIs(backend._executor, first)
            self.assertEqual(create.call_count, 1)

    def test_concurrent_execution_scopes_do_not_reuse_containers(self):
        """Serial admission still grants each request an independent filesystem."""
        with patch("harnest.sandbox_container.create_docker_executor", side_effect=lambda *_: executor()) as create, patch("harnest.sandbox_container.close_guarded_executor"):
            backend = create_container_backend(image="python")
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                results = list(pool.map(backend.execute, [request()] * 8))
            self.assertEqual(len(results), 8)
            self.assertEqual(create.call_count, 8)

    def test_hard_budgets_reach_docker_without_framework_settings(self):
        """CPU, memory, process, and writable space limits are kernel-owned policy."""
        budget = SandboxBudget(cpu=0.5, memory_bytes=64 * 1024 * 1024, pids=16, scratch_bytes=4096)
        with patch("harnest.sandbox_container.create_docker_executor", return_value=executor()) as create, patch("harnest.sandbox_container.close_guarded_executor"):
            backend = create_container_backend(image="python", budget=budget, max_output_bytes=512)
            backend.execute(request())
        options, limit = create.call_args.args
        self.assertEqual(limit, 512)
        self.assertFalse(options["network_enabled"])
        limits = options["harnest_resource_limits"]
        self.assertEqual(limits["nano_cpus"], 500_000_000)
        self.assertEqual(limits["mem_limit"], limits["memswap_limit"])
        self.assertEqual(limits["pids_limit"], 16)
        self.assertIn("size=4096", limits["tmpfs"]["/tmp"])
        self.assertTrue(limits["read_only"])
        self.assertEqual(limits["user"], "65534:65534")

    def test_image_declared_volumes_are_rejected_before_container_creation(self):
        """Image metadata cannot bypass the budgeted writable scratch policy."""
        from unittest.mock import MagicMock

        client = MagicMock()
        client.images.get.return_value.attrs = {"Config": {"Volumes": {"/unbounded": {}}}}
        with patch("docker.from_env", return_value=client):
            backend = create_container_backend(image="unsafe-volume-image")
            with self.assertRaisesRegex(ValueError, "must not declare volumes"):
                backend.execute(request())
        client.containers.create.assert_not_called()
        client.close.assert_called_once()

    def test_status_is_independent_of_stderr(self):
        """Warnings are successful; a nonzero exit with no stderr is still failure."""
        self.assertEqual(SandboxResult(stderr="warning", exit_code=0).status, SandboxStatus.SUCCEEDED)
        with self.assertRaises(ValueError):
            SandboxResult(exit_code=2)
        self.assertEqual(SandboxResult(status="failed", exit_code=2).status, SandboxStatus.FAILED)

    def test_invalid_authoring_policy_is_rejected_before_startup(self):
        """Reject unlimited resources, anonymous scope policies, and option escapes."""
        for values in ({"cpu": 0}, {"cpu": True}, {"cpu": float("inf")}, {"memory_bytes": 0}, {"pids": True}, {"scratch_bytes": -1}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SandboxBudget(**values)
        for values in ({"scope": "global"}, {"max_scopes": 0}, {"max_output_bytes": 0}, {"timeout_seconds": None}, {"options": {"volumes": {"/": "/host"}}}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Sandbox.container(image="python", **values)

"""Regress caller capability isolation and private provider failure boundaries."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from harnest import context
from harnest.agent_principal import (
    AgentRuntimePrincipal, activate_agent_principal, active_agent_principal,
    create_agent_principal_binding, revoke_agent_principal,
)
from harnest.context import ContextUnavailableError, activate_context, revoke_context
from harnest.runtime_task import TaskRuntimeError
from harnest.runtime_task_store import ProviderTaskRuntimeManager
from harnest.task import MemoryTaskStore

from test_task_store_recovery import ControlledWorkerManager
from test_task_store_runtime import application_for, invocation


def observed_identity():
    """Read ambient execution identity without manufacturing a managed context."""

    try:
        owner = context.current().user_id
    except ContextUnavailableError:
        owner = None
    principal = active_agent_principal()
    return owner, None if principal is None else principal.id


class ObservingTaskStore(MemoryTaskStore):
    """Observe ambient worker authority at the persistence boundary."""

    def __init__(self):
        """Record the first real worker claim without changing claim semantics."""

        super().__init__()
        self.worker_identities = []

    async def claim_tasks(self, **options):
        """Capture the worker's inherited context before it constructs task scope."""

        self.worker_identities.append(observed_identity())
        return await super().claim_tasks(**options)


class ProviderIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authored_enqueue_get_and_cancel_failures_hide_provider_parameters(self):
        """Every authored TaskHandle operation translates raw database diagnostics."""

        async def deliver(value):
            """Provide a pending task whose handle can exercise read/cancel errors."""

            return value

        store = MemoryTaskStore()
        authored, application = application_for(deliver, store)
        manager = ControlledWorkerManager(application)
        await manager.start()
        self.addAsyncCleanup(manager.close)
        handle = await authored.defer(value="private", schedule_in=60)
        operations = (
            ("enqueue_task", lambda: authored.defer(value="private")),
            ("get_task", handle.status),
            ("cancel_task", handle.cancel),
        )
        for method, call in operations:
            with self.subTest(operation=method):
                with patch.object(store, method, new=AsyncMock(side_effect=RuntimeError("dsn=password private-parameter"))):
                    with self.assertRaises(TaskRuntimeError) as raised:
                        await call()
                self.assertEqual(str(raised.exception), "task storage operation failed")
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    async def test_worker_lazy_start_does_not_inherit_caller_or_principal(self):
        """A worker born in a tool cannot lend its caller identity to later jobs."""

        completed = asyncio.Event()
        authored_identities = []

        async def deliver(value):
            """Record the no-snapshot task's effective user and principal."""

            authored_identities.append(observed_identity())
            completed.set()
            return value

        store = ObservingTaskStore()
        authored, application = application_for(deliver, store)
        manager = ProviderTaskRuntimeManager(application)
        self.addAsyncCleanup(manager.close)
        active = invocation("privileged-caller")
        binding = create_agent_principal_binding(AgentRuntimePrincipal.create(permissions=("admin",)))
        self.addCleanup(revoke_context, active)
        self.addCleanup(revoke_agent_principal, binding)
        with activate_context(active), activate_agent_principal(binding):
            await manager.start()
        # Keep the caller's lifetimes live so a leaked ContextVar is observable,
        # rather than masking inheritance with an unrelated revocation error.
        await authored.defer(value="background")
        await asyncio.wait_for(completed.wait(), 2)
        self.assertEqual(authored_identities, [(None, None)])
        self.assertTrue(store.worker_identities)
        self.assertEqual(set(store.worker_identities), {(None, None)})


if __name__ == "__main__":
    unittest.main()

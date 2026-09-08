"""Run the custom-provider conformance contract against the memory reference."""

import unittest

from harnest.task import MemoryTaskStore, TaskStore
from harnest.cron import CronStore
from harnest.testing import TaskStoreConformanceMixin


class MemoryTaskStoreTests(TaskStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    async def make_store(self):
        """Construct one independent reference provider for each behavior check."""

        return MemoryTaskStore()

    async def test_public_protocols_recognize_reference_provider(self):
        """Feature namespaces expose usable runtime structural contracts."""

        self.assertIsInstance(self.store, TaskStore)
        self.assertIsInstance(self.store, CronStore)


if __name__ == "__main__":
    unittest.main()

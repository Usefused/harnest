"""Conformance tests for the in-process reference channel store."""

from __future__ import annotations

import unittest

from harnest.channel_store_memory import MemoryChannelStore
from harnest.testing_channels import ChannelStoreConformanceMixin


class MemoryChannelStoreTests(ChannelStoreConformanceMixin, unittest.IsolatedAsyncioTestCase):
    async def make_store(self):
        """Exercise the public contract against the process-local reference."""

        return MemoryChannelStore()

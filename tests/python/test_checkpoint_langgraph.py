import unittest

from harnest.checkpoint import MemoryStore
from harnest.checkpoint_langgraph import HarnestCheckpointSaver


class LangGraphCheckpointAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        await self.store.start()
        await self.store.begin_run(
            application_id="support",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            framework="langgraph",
        )
        self.saver = HarnestCheckpointSaver(self.store)

    async def asyncTearDown(self):
        await self.store.close()

    async def test_native_round_trip_preserves_parent_and_pending_writes(self):
        root_config = {"configurable": {"thread_id": "run-1"}}
        first = await self.saver.aput(
            root_config,
            {"id": "checkpoint-1", "channel_values": {"value": 1}},
            {"source": "input", "step": 0, "parents": {}},
            {"value": "1"},
        )
        await self.saver.aput_writes(
            first, (("value", 2),), "task-1", "node"
        )
        second = await self.saver.aput(
            first,
            {"id": "checkpoint-2", "channel_values": {"value": 2}},
            {"source": "loop", "step": 1, "parents": {}},
            {"value": "2"},
        )

        latest = await self.saver.aget_tuple(second)
        parent = await self.saver.aget_tuple(first)

        self.assertEqual(latest.checkpoint["id"], "checkpoint-2")
        self.assertEqual(
            latest.parent_config["configurable"]["checkpoint_id"],
            "checkpoint-1",
        )
        self.assertEqual(parent.pending_writes, [("task-1", "value", 2)])

    async def test_listing_is_newest_first_and_thread_delete_cleans_run(self):
        config = {"configurable": {"thread_id": "run-1"}}
        for index in range(3):
            config = await self.saver.aput(
                config,
                {"id": f"checkpoint-{index}", "channel_values": {}},
                {"source": "loop", "step": index, "parents": {}},
                {},
            )

        values = [item async for item in self.saver.alist(config, limit=2)]
        await self.saver.adelete_thread("run-1")

        self.assertEqual(
            [item.checkpoint["id"] for item in values],
            ["checkpoint-2", "checkpoint-1"],
        )
        self.assertIsNone(await self.store.get_run(run_id="run-1"))


if __name__ == "__main__":
    unittest.main()

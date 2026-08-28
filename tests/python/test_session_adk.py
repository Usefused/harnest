import unittest

from google.adk.events import Event, EventActions
from google.adk.sessions.base_session_service import GetSessionConfig

from harnest.session import InMemorySessionStore
from harnest.session_adk import (
    create_adk_session_service,
    register_adk_session_service,
)


class ADKSessionStoreAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemorySessionStore()
        await self.store.start()
        self.service = create_adk_session_service(self.store)

    async def asyncTearDown(self):
        await self.store.close()

    async def test_crud_and_events_use_the_neutral_store(self):
        session = await self.service.create_session(
            app_name="support",
            user_id="user-1",
            session_id="session-1",
            state={"count": 1},
        )
        event = Event(
            invocation_id="invoke-1",
            author="agent",
            actions=EventActions(state_delta={"count": 2}),
        )
        await self.service.append_event(session, event)

        stored = await self.service.get_session(
            app_name="support", user_id="user-1", session_id="session-1"
        )
        self.assertEqual(stored.state, {"count": 2})
        self.assertEqual([item.invocation_id for item in stored.events], ["invoke-1"])
        records = await self.store.list(user_id="user-1")
        self.assertIn("_harnest_adk_events", records[0].state)

        listed = await self.service.list_sessions(
            app_name="support", user_id="user-1"
        )
        self.assertEqual([item.id for item in listed.sessions], ["session-1"])
        await self.service.create_session(
            app_name="support", user_id="user-1", session_id="session-2"
        )
        page = await self.service.list_sessions_page(
            app_name="support",
            user_id="user-1",
            after="session-1",
            limit=1,
        )
        self.assertEqual([item.id for item in page], ["session-2"])
        await self.service.delete_session(
            app_name="support", user_id="user-1", session_id="session-1"
        )
        self.assertIsNone(
            await self.service.get_session(
                app_name="support", user_id="user-1", session_id="session-1"
            )
        )

    async def test_execution_lease_reuses_the_store_lease_for_event_writes(self):
        session = await self.service.create_session(
            app_name="support",
            user_id="user-1",
            session_id="session-1",
        )
        async with self.service.execution_lease(
            user_id="user-1", session_id="session-1"
        ):
            current = await self.service.get_session(
                app_name="support", user_id="user-1", session_id="session-1"
            )
            self.assertIsNotNone(current)
            await self.service.append_event(
                session,
                Event(invocation_id="invoke-1", author="agent"),
            )

        filtered = await self.service.get_session(
            app_name="support",
            user_id="user-1",
            session_id="session-1",
            config=GetSessionConfig(num_recent_events=1),
        )
        self.assertEqual(len(filtered.events), 1)

    async def test_service_can_be_shared_with_adks_native_fastapi_factory(self):
        from google.adk.cli.utils.service_factory import (
            create_session_service_from_options,
        )

        with register_adk_session_service(self.service) as uri:
            resolved = create_session_service_from_options(
                base_dir=".", session_service_uri=uri, use_local_storage=False
            )
        self.assertIs(resolved, self.service)


if __name__ == "__main__":
    unittest.main()

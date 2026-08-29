from __future__ import annotations

import asyncio
import base64
from typing import Annotated
import unittest

from pydantic import BaseModel

from harnest.approval import ApprovalRun
from harnest.client_tool import (
    ClientToolError,
    ClientToolExecution,
    InMemoryClientToolStore,
    client_tool_execution,
    current_transient_media,
)
from harnest.content import Image, ImageConstraints
from harnest.tool import client_tool, tool
from harnest.transient_media import (
    TransientMediaAccess,
    TransientMediaLeaseStore,
    TransientMediaScope,
    sanitize_transient_media,
    stage_transient_media,
    stage_transient_media_batch,
)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_ENCODED_PNG = base64.b64encode(_PNG).decode("ascii")


class _CaptureResult(BaseModel):
    screenshot: Annotated[
        Image,
        ImageConstraints(
            media_types=frozenset({"image/png"}),
            max_bytes=100,
            max_width=2,
            max_height=2,
        ),
    ]


class TransientMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = TransientMediaScope("user-a", "session-a", "call-a")
        self.store = TransientMediaLeaseStore(max_total_bytes=1_000)

    def result(self) -> _CaptureResult:
        return _CaptureResult(
            screenshot=Image(data=_ENCODED_PNG, mediaType="image/png")
        )

    def test_staging_replaces_base64_and_peek_is_retry_safe(self):
        staged, lease_ids = stage_transient_media_batch(
            self.result(), store=self.store, scope=self.scope
        )
        access = TransientMediaAccess(self.store, self.scope)
        access.bind(lease_ids)

        first = access.pending()[0]
        second = access.pending()[0]
        skeleton, legacy_ids = sanitize_transient_media(staged)

        self.assertIs(first, second)
        self.assertEqual(first.data, _PNG)
        self.assertNotIn(_ENCODED_PNG, repr(staged))
        self.assertEqual(skeleton["screenshot"]["content"], "attached")
        self.assertEqual(legacy_ids, ())
        self.assertNotIn(first.lease_id, repr(staged))
        access.commit()
        self.assertIsNone(access.peek(first.lease_id))
        self.assertEqual(self.store.total_bytes, 0)

    def test_scope_isolation_and_clear_remove_private_bytes(self):
        staged = stage_transient_media(
            self.result(), store=self.store, scope=self.scope
        )
        marker = staged["screenshot"]

        other = TransientMediaAccess(
            self.store, TransientMediaScope("user-b", "session-a", "call-a")
        )
        self.assertIsNone(other.peek(marker))
        TransientMediaAccess(self.store, self.scope).clear()
        self.assertEqual(self.store.total_bytes, 0)

    def test_authored_constraints_are_enforced_before_any_lease_is_staged(self):
        class TooSmall(BaseModel):
            screenshot: Annotated[Image, ImageConstraints(max_bytes=10)]

        with self.assertRaisesRegex(ValueError, "inline media"):
            stage_transient_media(
                TooSmall(
                    screenshot=Image(data=_ENCODED_PNG, mediaType="image/png")
                ),
                store=self.store,
                scope=self.scope,
            )

        self.assertEqual(self.store.total_bytes, 0)


class ClientToolTransientMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_tool_stages_before_returning_to_the_framework(self):
        @tool(output_schema=_CaptureResult)
        def capture() -> _CaptureResult:
            """Capture a server-owned viewport."""

            return _CaptureResult(
                screenshot=Image(data=_ENCODED_PNG, mediaType="image/png")
            )

        store = InMemoryClientToolStore()
        run = ApprovalRun("run-a", "user-a", "session-a", "call-a")

        with client_tool_execution(ClientToolExecution(store, run)):
            result = capture()
            access = current_transient_media()
            assert access is not None
            marker = result["screenshot"]
            lease = access.pending()[0]

        self.assertIsNotNone(lease)
        self.assertEqual(lease.data, _PNG)
        self.assertEqual(marker["content"], "attached")
        self.assertNotIn("harnestTransient", marker)
        self.assertNotIn(_ENCODED_PNG, repr(result))
        access.clear()

    async def test_submission_sanitizes_before_resume_and_terminally_clears(self):
        @client_tool(output_schema=_CaptureResult)
        def capture() -> _CaptureResult:
            """Capture the connected client viewport."""

            raise AssertionError("client tool declarations do not execute")

        store = InMemoryClientToolStore()
        run = ApprovalRun(
            id="run-a",
            user_id="user-a",
            session_id="session-a",
            call_id="call-a",
        )

        async def invoke() -> tuple[dict[str, object], bool]:
            with client_tool_execution(ClientToolExecution(store, run)):
                result = await capture()
                access = current_transient_media()
                assert access is not None
                lease = access.pending()[0]
                return result, lease is access.pending()[0]

        task = asyncio.create_task(invoke())
        run.task = task
        kind, pending = await run.notifications.get()
        self.assertEqual(kind, "client_tool")

        await store.submit(
            pending.id,
            user_id="user-a",
            output={
                "screenshot": {
                    "data": _ENCODED_PNG,
                    "mediaType": "image/png",
                }
            },
        )
        result, retry_safe = await task
        await asyncio.sleep(0)

        self.assertTrue(retry_safe)
        self.assertNotIn(_ENCODED_PNG, repr(result))
        self.assertEqual(result["screenshot"]["content"], "attached")
        self.assertNotIn("harnestTransient", repr(result))
        self.assertEqual(store.transient_media.total_bytes, 0)

    async def test_invalid_inline_result_can_be_resubmitted_without_leaking_value(self):
        @client_tool(output_schema=_CaptureResult)
        def capture() -> _CaptureResult:
            """Capture the connected client viewport."""

            raise AssertionError("client tool declarations do not execute")

        store = InMemoryClientToolStore()
        run = ApprovalRun("run-a", "user-a", "session-a", "call-a")

        async def invoke() -> object:
            with client_tool_execution(ClientToolExecution(store, run)):
                return await capture()

        task = asyncio.create_task(invoke())
        run.task = task
        _, pending = await run.notifications.get()
        with self.assertRaises(ClientToolError) as raised:
            await store.submit(
                pending.id,
                user_id="user-a",
                output={
                    "screenshot": {
                        "data": "private-not-base64",
                        "mediaType": "image/png",
                    }
                },
            )
        self.assertNotIn("private-not-base64", str(raised.exception))

        await store.submit(
            pending.id,
            user_id="user-a",
            output={
                "screenshot": {
                    "data": _ENCODED_PNG,
                    "mediaType": "image/png",
                }
            },
        )
        await task


if __name__ == "__main__":
    unittest.main()

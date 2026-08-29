from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Annotated, Any
import unittest

from pydantic import BaseModel

from harnest.assets import AssetMediaMetadata, AssetScope, MemoryAssetStore
from harnest.content import Image, ImageConstraints
from harnest.content_validation import ContentValidationError
from harnest.neutral_runtime import (
    AgentInfo,
    InvocationRequest,
    InvocationResult,
    RuntimeEvent,
    SessionMessage,
    SessionRecord,
)
from harnest.runtime_content import ContentRuntimeDriver


class _Output(BaseModel):
    image: Annotated[Image, ImageConstraints(max_width=4, max_height=4)]


class _Driver:
    def __init__(self, asset_id: str, store: str = "default") -> None:
        self.asset_id = asset_id
        self.store = store
        self.info = AgentInfo(
            id="content",
            name="content",
            description="fixture",
            card={},
            output_schema=_Output,
        )

    async def invoke(self, request: InvocationRequest) -> InvocationResult:
        result = {
            "image": {
                "assetId": self.asset_id,
                "store": self.store,
                "width": 999,
            }
        }
        return InvocationResult("ready", (), result, request.session_id, {})

    async def stream(self, request: InvocationRequest) -> AsyncIterator[RuntimeEvent]:
        yield {
            "type": "output",
            "value": {
                "image": {
                    "assetId": self.asset_id,
                    "store": self.store,
                    "width": 999,
                }
            },
        }
        yield {
            "type": "graph_output",
            "output": {
                "image": {
                    "assetId": self.asset_id,
                    "store": self.store,
                    "width": 999,
                }
            },
            "result": {"untrusted": "must be replaced"},
        }

    async def create_session(
        self, *, session_id: str, user_id: str, state: Mapping[str, Any]
    ) -> SessionRecord:
        return SessionRecord(session_id, user_id, state)

    async def get_session(self, **_kwargs: Any) -> SessionRecord | None:
        return None

    async def list_sessions(self, **_kwargs: Any) -> Sequence[SessionRecord]:
        return ()

    async def get_session_messages(
        self, **_kwargs: Any
    ) -> Sequence[SessionMessage] | None:
        return None

    async def update_session(self, **_kwargs: Any) -> SessionRecord | None:
        return None

    async def delete_session(self, **_kwargs: Any) -> bool:
        return False

    async def close(self) -> None:
        return None


async def _chunks(value: bytes):
    yield value


class ContentRuntimeDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_and_stream_outputs_use_authoritative_metadata(self):
        scope = AssetScope("user", "session")
        store = MemoryAssetStore(max_asset_bytes=16, max_total_bytes=32)
        record = await store.save(
            scope=scope,
            media_type="image/png",
            chunks=_chunks(b"image"),
            metadata=AssetMediaMetadata(width=3, height=4, frame_count=1),
        )
        driver = ContentRuntimeDriver(_Driver(record.asset_id), store)
        request = InvocationRequest({}, "user", "session", "invoke", {}, {})

        result = await driver.invoke(request)
        events = [event async for event in driver.stream(request)]

        self.assertEqual(result.result["image"]["width"], 3)
        self.assertEqual(result.result["image"]["mediaType"], "image/png")
        self.assertEqual(events[0]["value"]["image"]["height"], 4)
        self.assertEqual(events[1]["output"], events[1]["result"])
        self.assertEqual(events[1]["result"]["image"]["width"], 3)

    async def test_output_constraints_fail_before_publication(self):
        scope = AssetScope("user", "session")
        store = MemoryAssetStore(max_asset_bytes=16, max_total_bytes=32)
        record = await store.save(
            scope=scope,
            media_type="image/png",
            chunks=_chunks(b"image"),
            metadata=AssetMediaMetadata(width=5, height=4, frame_count=1),
        )
        driver = ContentRuntimeDriver(_Driver(record.asset_id), store)
        request = InvocationRequest({}, "user", "session", "invoke", {}, {})

        with self.assertRaises(ContentValidationError):
            await driver.invoke(request)

    async def test_named_output_reference_uses_its_declared_store(self):
        scope = AssetScope("user", "session")
        default = MemoryAssetStore()
        media = MemoryAssetStore()
        record = await media.save(
            scope=scope,
            media_type="image/png",
            chunks=_chunks(b"image"),
            metadata=AssetMediaMetadata(width=3, height=4),
        )
        driver = ContentRuntimeDriver(
            _Driver(record.asset_id, "media"),
            default,
            {"default": default, "media": media},
        )
        request = InvocationRequest({}, "user", "session", "invoke", {}, {})

        result = await driver.invoke(request)

        self.assertEqual(result.result["image"]["store"], "media")
        self.assertEqual(result.result["image"]["width"], 3)


if __name__ == "__main__":
    unittest.main()

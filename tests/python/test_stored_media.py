from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Annotated, Any
import unittest

from google.genai import types
from pydantic import BaseModel

from harnest import Stored
from harnest.approval import ApprovalRun
from harnest.application import CompiledApplication
from harnest.assets import AssetScope, AssetStoreError, MemoryAssetStore
from harnest.client_tool import (
    ClientToolError,
    ClientToolExecution,
    InMemoryClientToolStore,
    client_tool_execution,
)
from harnest.content import Image, ImageConstraints
from harnest.context import activate_context, create_agent_context
from harnest.runtime_adk import _adk_tool_response, _asset_content_plugin
from harnest.runtime_langgraph import (
    _graph_input,
    _materialize_model_message,
    _message_tool_events,
)
from harnest.tool import client_tool, tool


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_PNG64 = base64.b64encode(_PNG).decode("ascii")


class _URLStore(MemoryAssetStore):
    def __init__(self) -> None:
        super().__init__()
        self.saves: list[dict[str, Any]] = []
        self.urls: list[tuple[str, float]] = []

    async def save(self, **kwargs: Any):
        self.saves.append(dict(kwargs))
        return await super().save(**kwargs)

    async def signed_url(
        self, *, scope: AssetScope, asset_id: str, expires_in: float
    ) -> str:
        if await self.stat(scope=scope, asset_id=asset_id) is None:
            raise RuntimeError("missing")
        self.urls.append((asset_id, expires_in))
        return f"https://assets.example/{len(self.urls)}"


class _BlockingURLStore(_URLStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def save(self, **kwargs: Any):
        self.entered.set()
        await self.release.wait()
        return await super().save(**kwargs)


class _FailOnceURLStore(_URLStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def save(self, **kwargs: Any):
        if not self.failed:
            self.failed = True
            raise AssetStoreError("storage unavailable")
        return await super().save(**kwargs)


class _BlockingAfterSaveStore(_URLStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def save(self, **kwargs: Any):
        record = await super().save(**kwargs)
        self.entered.set()
        await self.release.wait()
        return record


_LIMIT = ImageConstraints(
    media_types=frozenset({"image/png"}), max_bytes=100
)


class _MixedResult(BaseModel):
    durable: Annotated[
        Image,
        _LIMIT,
        Stored(
            store="media",
            path="captures",
            expires_in=37,
            retention=timedelta(hours=1),
        ),
    ]
    transient: Annotated[Image, _LIMIT]


class _StoredOnly(BaseModel):
    image: Annotated[Image, _LIMIT, Stored(store="media", expires_in=23)]


class _InvalidMixedResult(BaseModel):
    durable: Annotated[Image, _LIMIT, Stored(store="media")]
    transient: Annotated[Image, ImageConstraints(max_bytes=1)]


class StoredClientToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_result_saves_named_store_and_keeps_sibling_transient(self):
        media = _URLStore()
        client_tools = InMemoryClientToolStore(asset_stores={"media": media})
        run = ApprovalRun("run", "user", "session", "call")

        @client_tool(output_schema=_MixedResult)
        def capture() -> _MixedResult:
            """Capture two bounded images."""

            raise AssertionError

        async def invoke():
            with client_tool_execution(ClientToolExecution(client_tools, run)):
                return await capture()

        task = asyncio.create_task(invoke())
        run.task = task
        _, pending = await run.notifications.get()
        await client_tools.submit(
            pending.id,
            user_id=run.user_id,
            output={
                "durable": {"data": _PNG64, "mediaType": "image/png"},
                "transient": {"data": _PNG64, "mediaType": "image/png"},
            },
        )
        result = await task

        durable = result["durable"]
        transient = result["transient"]
        self.assertEqual(durable["store"], "media")
        self.assertIn("assetId", durable)
        self.assertEqual(durable["harnestStored"], {"expiresIn": 37})
        self.assertNotIn("data", durable)
        self.assertEqual(transient["content"], "attached")
        self.assertNotIn("data", transient)
        self.assertEqual(media.saves[0]["path"], "captures")
        self.assertEqual(media.saves[0]["retention_seconds"], 3600)

    async def test_invalid_transient_sibling_prevents_durable_write(self):
        media = _URLStore()
        client_tools = InMemoryClientToolStore(asset_stores={"media": media})
        run = ApprovalRun("run", "user", "session", "call")

        @client_tool(output_schema=_InvalidMixedResult)
        def capture() -> _InvalidMixedResult:
            """Capture one durable and one transient image."""

            raise AssertionError

        async def invoke():
            with client_tool_execution(ClientToolExecution(client_tools, run)):
                return await capture()

        task = asyncio.create_task(invoke())
        run.task = task
        _, pending = await run.notifications.get()
        with self.assertRaises(ClientToolError):
            await client_tools.submit(
                pending.id,
                user_id=run.user_id,
                output={
                    "durable": {"data": _PNG64, "mediaType": "image/png"},
                    "transient": {"data": _PNG64, "mediaType": "image/png"},
                },
            )
        self.assertEqual(media.total_bytes, 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_top_level_input_and_ordinary_tool_use_the_same_durable_pass(self):
        media = _URLStore()
        client_tools = InMemoryClientToolStore(asset_stores={"media": media})
        run = ApprovalRun("run", "user", "session", "call")
        active = create_agent_context(
            framework="langgraph",
            agent_name="vision",
            invocation_id="run",
            user_id="user",
            session_id="session",
            metadata={},
            resources={},
            asset_stores={"media": media},
        )
        authored = _StoredOnly(
            image=Image(data=_PNG64, mediaType="image/png")
        )
        application = CompiledApplication(
            name="vision",
            framework="langgraph",
            mode="managed",
            target=object(),
            input_schema=_StoredOnly,
        )

        @tool
        async def capture() -> _StoredOnly:
            """Return one durable image."""

            return authored

        with (
            activate_context(active),
            client_tool_execution(ClientToolExecution(client_tools, run)),
        ):
            graph = await _graph_input(
                application,
                authored.model_dump(mode="json", by_alias=True),
                {},
                stores={"media": media},
                scope=AssetScope("user", "session"),
            )
            tool_result = await capture()

        input_block = graph["messages"][0].content[0]
        self.assertEqual(input_block["store"], "media")
        self.assertNotIn("data", input_block)
        self.assertEqual(input_block["harnestStored"], {"expiresIn": 23})
        self.assertEqual(tool_result["image"]["store"], "media")
        self.assertNotIn("data", tool_result["image"])
        self.assertEqual(tool_result["image"]["harnestStored"], {"expiresIn": 23})
        self.assertEqual(len(media.saves), 2)

    async def test_sync_tool_with_stored_output_is_rejected_without_api_mutation(self):
        with self.assertRaisesRegex(TypeError, "requires an async @tool"):

            @tool
            def capture() -> _StoredOnly:
                """Return one durable image."""

                raise AssertionError

    async def test_concurrent_submit_is_reserved_and_storage_failure_can_retry(self):
        blocking = _BlockingURLStore()
        client_tools, run, pending, task = await _pending_stored_tool(blocking)
        payload = {"image": {"data": _PNG64, "mediaType": "image/png"}}
        first = asyncio.create_task(
            client_tools.submit(pending.id, user_id=run.user_id, output=payload)
        )
        await blocking.entered.wait()
        with self.assertRaisesRegex(ClientToolError, "already submitted"):
            await client_tools.submit(
                pending.id, user_id=run.user_id, output=payload
            )
        blocking.release.set()
        await first
        await task
        self.assertEqual(len(blocking.saves), 1)

        failing = _FailOnceURLStore()
        client_tools, run, pending, task = await _pending_stored_tool(failing)
        with self.assertRaises(ClientToolError):
            await client_tools.submit(
                pending.id, user_id=run.user_id, output=payload
            )
        await client_tools.submit(
            pending.id, user_id=run.user_id, output=payload
        )
        await task
        self.assertEqual(len(failing.saves), 1)

    async def test_cancel_during_save_rolls_back_only_new_asset(self):
        storage = _BlockingAfterSaveStore()
        client_tools, run, pending, invocation = await _pending_stored_tool(storage)
        payload = {"image": {"data": _PNG64, "mediaType": "image/png"}}
        submission = asyncio.create_task(
            client_tools.submit(pending.id, user_id=run.user_id, output=payload)
        )
        await storage.entered.wait()
        invocation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invocation
        storage.release.set()
        with self.assertRaisesRegex(ClientToolError, "already submitted"):
            await submission
        self.assertEqual(storage.total_bytes, 0)


class StoredModelBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scope = AssetScope("user", "session")
        self.media = _URLStore()
        self.record = await self.media.save(
            scope=self.scope,
            media_type="image/png",
            chunks=_chunks(_PNG),
        )
        self.marker = {
            "type": "image",
            "store": "media",
            "assetId": self.record.asset_id,
            "mediaType": "image/png",
            "harnestStored": {"expiresIn": 37},
        }

    async def test_adk_generates_fresh_url_per_model_call_and_sanitizes_json(self):
        original = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="tool", name="capture", response={"image": self.marker}
                    )
                )
            ],
        )
        callback = SimpleNamespace(
            user_id="user",
            session=SimpleNamespace(id="session"),
            invocation_id="run",
            agent_name="child",
            branch="child",
            node_path="child",
        )
        plugin = _asset_content_plugin({"media": self.media})

        for _ in range(2):
            request = SimpleNamespace(contents=[original])
            await plugin.before_model_callback(
                callback_context=callback, llm_request=request
            )
            response = request.contents[0].parts[0].function_response.response
            self.assertNotIn("harnestStored", repr(response))
            self.assertEqual(request.contents[0].parts[1].file_data.mime_type, "image/png")

        self.assertEqual([ttl for _, ttl in self.media.urls], [37, 37])
        self.assertNotEqual(self.media.urls[0][0], "")

    async def test_langgraph_routes_named_store_and_generates_url_at_each_call(self):
        class Message:
            def __init__(self, content: Any) -> None:
                self.content = content

            def model_copy(self, *, update: dict[str, Any]):
                return Message(update["content"])

        original = Message(json.dumps({"image": self.marker}))
        for expected in (1, 2):
            projected, _ = await _materialize_model_message(
                original,
                stores={"media": self.media},
                scope=self.scope,
            )
            self.assertNotIn("harnestStored", projected.content[0]["text"])
            self.assertEqual(projected.content[1]["url"], f"https://assets.example/{expected}")
        self.assertEqual([ttl for _, ttl in self.media.urls], [37, 37])

    async def test_declared_store_without_url_capability_fails_without_fallback(self):
        plain = MemoryAssetStore()
        record = await plain.save(
            scope=self.scope,
            media_type="image/png",
            chunks=_chunks(_PNG),
        )
        marker = {**self.marker, "assetId": record.asset_id}

        class Message:
            content = json.dumps({"image": marker})

            def model_copy(self, *, update: dict[str, Any]):
                return SimpleNamespace(**update)

        with self.assertRaisesRegex(RuntimeError, "cannot generate model URLs"):
            await _materialize_model_message(
                Message(), stores={"media": plain}, scope=self.scope
            )

    async def test_public_tool_events_keep_reference_but_hide_delivery_policy(self):
        adk = _adk_tool_response(
            [{"functionResponse": {"response": {"image": self.marker}}}]
        )

        class ToolMessage:
            type = "tool"
            content = json.dumps({"image": self.marker})
            tool_call_id = "tool"
            name = "capture"

        langgraph = _message_tool_events(ToolMessage())[0]["result"]
        self.assertEqual(adk["image"]["store"], "media")
        self.assertEqual(adk["image"]["assetId"], self.record.asset_id)
        self.assertNotIn("harnestStored", repr(adk))
        self.assertIn(self.record.asset_id, langgraph)
        self.assertNotIn("harnestStored", langgraph)
        self.assertNotIn("expiresIn", langgraph)


async def _chunks(value: bytes):
    yield value


async def _pending_stored_tool(storage: _URLStore):
    client_tools = InMemoryClientToolStore(asset_stores={"media": storage})
    run = ApprovalRun("run", "user", "session", "call")

    @client_tool(output_schema=_StoredOnly)
    def capture() -> _StoredOnly:
        """Capture one stored image."""

        raise AssertionError

    async def invoke():
        with client_tool_execution(ClientToolExecution(client_tools, run)):
            return await capture()

    task = asyncio.create_task(invoke())
    run.task = task
    _, pending = await run.notifications.get()
    return client_tools, run, pending, task


if __name__ == "__main__":
    unittest.main()

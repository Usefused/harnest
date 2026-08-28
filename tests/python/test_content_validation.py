from __future__ import annotations

from typing import Annotated
import unittest

from pydantic import BaseModel, ConfigDict

from harnest.assets import AssetMediaMetadata, AssetScope, MemoryAssetStore
from harnest.content import Data, DataConstraints, Image, ImageConstraints
from harnest.content_validation import (
    ContentCapabilities,
    ContentValidationError,
    resolve_model_content,
)


async def _chunks(value: bytes):
    yield value


class _Request(BaseModel):
    model_config = ConfigDict(strict=True)

    prompt: str
    image: Annotated[
        Image,
        ImageConstraints(
            media_types=frozenset({"image/png"}),
            max_bytes=8,
            min_width=2,
            max_width=4,
            max_pixels=12,
        ),
    ]
    attachments: list[
        Annotated[Image, ImageConstraints(media_types=frozenset({"image/*"}))]
    ]
    context: Annotated[Data[dict[str, str]], DataConstraints(max_bytes=32)]


class ContentValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scope = AssetScope("user-a", "session-a")
        self.store = MemoryAssetStore(max_asset_bytes=128, max_total_bytes=512)
        self.record = await self.store.save(
            scope=self.scope,
            media_type="image/png",
            chunks=_chunks(b"pngdata"),
            metadata=AssetMediaMetadata(width=3, height=4, frame_count=1),
        )

    def request(self, asset_id: str | None = None) -> _Request:
        reference = asset_id or self.record.asset_id
        return _Request(
            prompt="inspect",
            image={
                "assetId": reference,
                "mediaType": "image/jpeg",
                "sizeBytes": 1,
                "width": 999,
                "height": 999,
            },
            attachments=[{"assetId": reference}],
            context={"value": {"ticket": "T-1"}},
        )

    async def test_resolves_nested_references_with_authoritative_metadata(self):
        resolved = await resolve_model_content(
            self.request(), store=self.store, scope=self.scope
        )

        self.assertEqual(resolved.image.media_type, "image/png")
        self.assertEqual(resolved.image.size_bytes, 7)
        self.assertEqual((resolved.image.width, resolved.image.height), (3, 4))
        self.assertEqual(resolved.attachments[0].media_type, "image/png")

    async def test_absent_and_cross_scope_assets_share_one_error(self):
        for scope, asset_id in (
            (self.scope, "asset_missing_reference"),
            (AssetScope("user-b", "session-a"), self.record.asset_id),
        ):
            with self.subTest(scope=repr(scope)):
                with self.assertRaisesRegex(
                    ContentValidationError, "content asset is unavailable"
                ):
                    await resolve_model_content(
                        self.request(asset_id), store=self.store, scope=scope
                    )

    async def test_authored_dimensions_and_capabilities_fail_closed(self):
        oversized = await self.store.save(
            scope=self.scope,
            media_type="image/png",
            chunks=_chunks(b"other"),
            metadata=AssetMediaMetadata(width=5, height=4, frame_count=1),
        )
        with self.assertRaisesRegex(ContentValidationError, "dimensions"):
            await resolve_model_content(
                self.request(oversized.asset_id), store=self.store, scope=self.scope
            )
        with self.assertRaisesRegex(ContentValidationError, "not supported"):
            await resolve_model_content(
                self.request(),
                store=self.store,
                scope=self.scope,
                capabilities=ContentCapabilities(kinds=frozenset({"text", "data"})),
            )

    async def test_custom_data_size_is_checked_after_structural_validation(self):
        value = self.request().model_copy(
            update={"context": Data[dict[str, str]](value={"large": "x" * 40})}
        )
        with self.assertRaisesRegex(ContentValidationError, "custom data"):
            await resolve_model_content(value, store=self.store, scope=self.scope)


if __name__ == "__main__":
    unittest.main()

import base64
import math
from typing import Annotated
import unittest

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from harnest.content import (
    AssetRef,
    Audio,
    AudioConstraints,
    ContentPart,
    Data,
    DataConstraints,
    File,
    FileConstraints,
    Image,
    ImageConstraints,
    Text,
    Video,
    VideoConstraints,
    constraint_schema_extension,
    content_constraints,
    content_schema_extension,
)


class PortableContentTests(unittest.TestCase):
    def test_content_parts_use_strict_immutable_wire_contracts(self):
        parts = (
            Text(text="Inspect these inputs."),
            Image(
                assetId="asset_image-1",
                mediaType="IMAGE/PNG",
                sizeBytes=512,
                width=32,
                height=24,
                animated=False,
                frameCount=1,
            ),
            Audio(
                assetId="asset_audio-1",
                durationSeconds=1.25,
                sampleRateHz=48_000,
                channels=2,
            ),
            Video(
                assetId="asset_video-1",
                durationSeconds=2.5,
                frameRate=30.0,
                hasAudio=True,
            ),
            File(assetId="asset_file-1", name="report.pdf", pageCount=3),
            AssetRef(assetId="asset_other-1"),
        )

        wire = [part.model_dump(by_alias=True, exclude_none=True) for part in parts]

        self.assertEqual(wire[0], {"type": "text", "text": "Inspect these inputs."})
        self.assertEqual(wire[1]["mediaType"], "image/png")
        self.assertEqual(wire[1]["frameCount"], 1)
        self.assertEqual(wire[2]["durationSeconds"], 1.25)
        self.assertEqual(wire[3]["hasAudio"], True)
        self.assertEqual(wire[4]["pageCount"], 3)
        self.assertEqual(
            wire[5],
            {"assetId": "asset_other-1", "store": "default", "type": "asset"},
        )
        with self.assertRaises(ValidationError):
            Image(assetId="asset-1", width=2.5)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            parts[1].width = 64  # type: ignore[misc]

    def test_persisted_media_rejects_inline_data_urls_and_unknown_fields(self):
        for unsafe_field, value in (
            ("url", "https://private.example/image.png"),
            ("base64", "c2VjcmV0"),
            ("bytes", b"secret"),
            ("providerFileId", "provider-secret"),
        ):
            with self.subTest(field=unsafe_field), self.assertRaises(ValidationError):
                Image.model_validate({"assetId": "asset-1", unsafe_field: value})

    def test_media_accepts_exactly_one_inline_or_stored_source(self):
        encoded = base64.b64encode(b"private-image").decode("ascii")

        inline = Image(data=encoded, mediaType="IMAGE/JPEG")
        stored = Image(assetId="asset-1", store="private_media")

        self.assertEqual(inline.media_type, "image/jpeg")
        self.assertNotIn(encoded, repr(inline))
        self.assertEqual(stored.store, "private_media")
        for invalid in (
            {},
            {"assetId": "asset-1", "data": encoded, "mediaType": "image/jpeg"},
            {"data": encoded},
            {"data": "not-base64", "mediaType": "image/jpeg"},
        ):
            with self.subTest(invalid=tuple(invalid)), self.assertRaises(ValidationError):
                Image.model_validate(invalid)

    def test_asset_metadata_requires_safe_identifiers_and_concrete_mime_types(self):
        invalid = (
            {"assetId": ""},
            {"assetId": "with spaces"},
            {"assetId": "asset-1", "mediaType": "image/*"},
            {"assetId": "asset-1", "mediaType": "not-a-mime"},
            {"assetId": "asset-1", "sizeBytes": -1},
        )

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AssetRef.model_validate(value)

    def test_asset_reference_keeps_optional_domain_label_out_of_repr(self):
        reference = AssetRef(
            assetId="asset-profile-1",
            store="uploads",
            label=" profile-photo ",
        )

        self.assertEqual(reference.store, "uploads")
        self.assertEqual(reference.label, "profile-photo")
        self.assertNotIn("profile-photo", repr(reference))
        self.assertEqual(
            reference.model_dump(by_alias=True, exclude_none=True)["label"],
            "profile-photo",
        )
        with self.assertRaises(ValidationError):
            AssetRef(assetId="asset-profile-1", label=" ")

    def test_typed_data_accepts_json_and_rejects_non_json_payloads(self):
        class Location(BaseModel):
            model_config = ConfigDict(frozen=True)
            latitude: float
            longitude: float

        value = Data[Location](
            value=Location(latitude=51.5, longitude=-0.1)
        )

        self.assertEqual(
            value.model_dump(mode="json"),
            {
                "type": "data",
                "value": {"latitude": 51.5, "longitude": -0.1},
            },
        )
        with self.assertRaisesRegex(ValidationError, "JSON-serializable"):
            Data[object](value=object())
        with self.assertRaisesRegex(ValidationError, "JSON-serializable"):
            Data[float](value=math.nan)

    def test_content_part_union_parses_by_literal_type(self):
        adapter = TypeAdapter(ContentPart)

        parsed = adapter.validate_python(
            {"type": "image", "assetId": "asset-1", "width": 100}
        )

        self.assertIsInstance(parsed, Image)
        self.assertEqual(parsed.width, 100)


class ConstraintTests(unittest.TestCase):
    def test_image_constraints_are_immutable_normalized_and_schema_visible(self):
        constraint = ImageConstraints(
            media_types={"IMAGE/PNG", "image/*"},
            max_bytes=5_000_000,
            min_width=256,
            max_width=2048,
            min_height=256,
            max_height=2048,
            max_pixels=4_000_000,
            min_aspect_ratio=0.5,
            max_aspect_ratio=2.0,
            animated=False,
            max_frames=1,
        )
        AuthoredImage = Annotated[Image, constraint]

        self.assertEqual(
            constraint.media_types, frozenset({"image/png", "image/*"})
        )
        with self.assertRaises(AttributeError):
            constraint.max_bytes = 1  # type: ignore[misc]
        self.assertEqual(content_constraints(AuthoredImage), (constraint,))
        self.assertEqual(
            TypeAdapter(AuthoredImage).json_schema()["x-harnest-media"],
            {
                "kind": "image",
                "mediaTypes": ["image/*", "image/png"],
                "maxBytes": 5_000_000,
                "minWidth": 256,
                "maxWidth": 2048,
                "minHeight": 256,
                "maxHeight": 2048,
                "maxPixels": 4_000_000,
                "minAspectRatio": 0.5,
                "maxAspectRatio": 2.0,
                "animated": False,
                "maxFrames": 1,
            },
        )

    def test_constraints_are_found_inside_authored_containers(self):
        image = ImageConstraints(max_bytes=10)
        audio = AudioConstraints(max_duration_seconds=2)
        annotation = list[Annotated[Image, image]] | Annotated[Audio, audio] | None

        self.assertEqual(content_constraints(annotation), (image, audio))
        with self.assertRaisesRegex(ValueError, "at most one"):
            content_schema_extension(annotation)
        self.assertEqual(content_schema_extension(str), {})

    def test_constraint_extension_is_deterministic_and_lower_camel_case(self):
        constraint = FileConstraints(
            media_types={"application/zip", "application/pdf"},
            max_bytes=20,
            max_pages=4,
            archives_allowed=True,
            max_expanded_bytes=100,
        )

        self.assertEqual(
            constraint_schema_extension(constraint),
            {
                "x-harnest-media": {
                    "kind": "file",
                    "mediaTypes": ["application/pdf", "application/zip"],
                    "maxBytes": 20,
                    "maxPages": 4,
                    "archivesAllowed": True,
                    "maxExpandedBytes": 100,
                }
            },
        )

    def test_mime_patterns_are_validated_at_construction(self):
        valid = ImageConstraints(media_types={"*/*", "image/*", "image/svg+xml"})
        self.assertEqual(
            valid.media_types, frozenset({"*/*", "image/*", "image/svg+xml"})
        )

        for media_types in (set(), {"image"}, {"*/png"}, {"image/png; charset=x"}):
            with self.subTest(media_types=media_types), self.assertRaisesRegex(
                ValueError, "media_types"
            ):
                ImageConstraints(media_types=media_types)

    def test_image_constraints_reject_invalid_counts_ranges_and_flags(self):
        invalid = (
            {"max_bytes": 0},
            {"min_width": 1.5},
            {"min_width": 20, "max_width": 10},
            {"min_height": 20, "max_height": 10},
            {"min_aspect_ratio": 2, "max_aspect_ratio": 1},
            {"animated": "false"},
            {"animated": False, "max_frames": 2},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ImageConstraints(**values)  # type: ignore[arg-type]

    def test_audio_constraints_reject_invalid_ranges(self):
        invalid = (
            {"min_duration_seconds": -1},
            {"min_duration_seconds": 4, "max_duration_seconds": 3},
            {"min_sample_rate_hz": 48_000, "max_sample_rate_hz": 44_100},
            {"min_channels": 2, "max_channels": 1},
            {"max_channels": 1.5},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                AudioConstraints(**values)  # type: ignore[arg-type]

    def test_video_constraints_reject_invalid_ranges_and_flags(self):
        invalid = (
            {"max_duration_seconds": 0},
            {"min_duration_seconds": 2, "max_duration_seconds": 1},
            {"min_width": 20, "max_width": 10},
            {"min_height": 20, "max_height": 10},
            {"min_frame_rate": 60, "max_frame_rate": 30},
            {"audio_allowed": 1},
        )

        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                VideoConstraints(**values)  # type: ignore[arg-type]

    def test_file_and_data_constraints_reject_invalid_limits(self):
        invalid = (
            lambda: FileConstraints(max_pages=0),
            lambda: FileConstraints(max_bytes=1.5),
            lambda: FileConstraints(archives_allowed="yes"),
            lambda: FileConstraints(
                archives_allowed=False, max_expanded_bytes=100
            ),
            lambda: DataConstraints(max_bytes=-1),
        )

        for build in invalid:
            with self.subTest(build=build), self.assertRaises(ValueError):
                build()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import struct
import unittest
from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel

from harnest.asset_policy import storage_policy
from harnest.asset_inspection import AssetInspectionError, inspect_asset
from harnest.assets import Stored
from harnest.content import Image


def _chunk(kind: bytes, payload: bytes, byteorder: str = "big") -> bytes:
    return len(payload).to_bytes(4, byteorder) + kind + payload + b"\0\0\0\0"


def _png(width: int, height: int, frames: int | None = None) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunks = [_chunk(b"IHDR", ihdr)]
    if frames is not None:
        chunks.append(_chunk(b"acTL", struct.pack(">II", frames, 0)))
    chunks.append(_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _jpeg(width: int, height: int) -> bytes:
    sof = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\0"
    return b"\xff\xd8\xff\xe0\0\x04xx\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof


def _gif(width: int, height: int, frames: int) -> bytes:
    header = b"GIF89a" + struct.pack("<HHBBB", width, height, 0, 0, 0)
    image = b"\x2c" + b"\0" * 4 + struct.pack("<HH", width, height) + b"\0\x02\x01\0\0"
    return header + image * frames + b"\x3b"


def _webp_lossless(width: int, height: int) -> bytes:
    packed = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + packed.to_bytes(4, "little")
    body = b"WEBP" + b"VP8L" + len(payload).to_bytes(4, "little") + payload + b"\0"
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _wav(seconds: int = 1, sample_rate: int = 8_000) -> bytes:
    channels, bits = 1, 8
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits)
    samples = b"\x80" * (seconds * byte_rate)
    body = b"WAVE" + _riff_chunk(b"fmt ", fmt) + _riff_chunk(b"data", samples)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _riff_chunk(kind: bytes, payload: bytes) -> bytes:
    padding = b"\0" if len(payload) & 1 else b""
    return kind + len(payload).to_bytes(4, "little") + payload + padding


def _mp4_box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def _mp4(
    *,
    duration: int = 2_000,
    timescale: int = 1_000,
    width: int = 320,
    height: int = 240,
    fragmented: bool = False,
    movie_header_version: int = 0,
) -> bytes:
    movie_header = bytearray(112 if movie_header_version == 1 else 100)
    movie_header[0] = movie_header_version
    if movie_header_version == 1:
        movie_header[20:24] = timescale.to_bytes(4, "big")
        movie_header[24:32] = duration.to_bytes(8, "big")
    else:
        movie_header[12:16] = timescale.to_bytes(4, "big")
        movie_header[16:20] = duration.to_bytes(4, "big")
    track_header = bytearray(84)
    track_header[76:80] = (width << 16).to_bytes(4, "big")
    track_header[80:84] = (height << 16).to_bytes(4, "big")
    track = _mp4_box(b"trak", _mp4_box(b"tkhd", bytes(track_header)))
    movie = _mp4_box(b"moov", _mp4_box(b"mvhd", bytes(movie_header)) + track)
    file_type = _mp4_box(b"ftyp", b"isom\0\0\0\0isom")
    fragment = _mp4_box(b"moof", _mp4_box(b"mfhd", b"\0" * 8)) if fragmented else b""
    return file_type + movie + fragment


class AssetInspectionTests(unittest.TestCase):
    def test_supported_formats_report_authoritative_metadata(self):
        cases = (
            ("png", _png(20, 10), "IMAGE/PNG", {"width": 20, "height": 10, "frame_count": 1}),
            ("apng", _png(20, 10, 3), "image/png", {"width": 20, "height": 10, "frame_count": 3}),
            ("jpeg", _jpeg(31, 17), "image/jpeg", {"width": 31, "height": 17, "frame_count": 1}),
            ("gif", _gif(12, 9, 2), "image/gif", {"width": 12, "height": 9, "frame_count": 2}),
            ("webp", _webp_lossless(24, 13), "image/webp", {"width": 24, "height": 13, "frame_count": 1}),
            ("pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n", "application/pdf", {"page_count": None}),
            ("wav", _wav(2), "audio/wav", {"duration_seconds": 2, "channel_count": 1, "sample_rate_hz": 8_000}),
            ("mp4-v0", _mp4(), "video/mp4", {"duration_seconds": 2, "width": 320, "height": 240}),
            ("mp4-v1", _mp4(movie_header_version=1), "video/mp4", {"duration_seconds": 2, "width": 320, "height": 240}),
        )
        for name, content, declared, expected in cases:
            with self.subTest(format=name):
                metadata, concrete = inspect_asset(content, declared)
                self.assertEqual(concrete, declared.lower())
                self.assertEqual({key: getattr(metadata, key) for key in expected}, expected)

    def test_media_limits_reject_oversized_content(self):
        cases = (
            (b"x" * (10 * 1024 * 1024 + 1), "image/png", "size limit"),
            (_png(4_001, 4_000), "image/png", "pixel limit"),
            (_wav(301, sample_rate=100), "audio/wav", "duration limit"),
            (_mp4(duration=301_000), "video/mp4", "duration limit"),
            (_mp4(width=4_001, height=4_000), "video/mp4", "pixel limit"),
        )
        for content, declared, error in cases:
            with self.subTest(declared=declared, error=error), self.assertRaisesRegex(AssetInspectionError, error):
                inspect_asset(content, declared)

    def test_malformed_or_uninspectable_media_fails_closed(self):
        duration_error = "^asset media duration could not be established$"
        mp4_error = "duration could not be established|malformed"
        cases = (
            ("fragmented", _mp4(fragmented=True), "video/mp4", mp4_error),
            ("zero-timescale", _mp4(timescale=0), "video/mp4", mp4_error),
            ("no-movie", _mp4_box(b"ftyp", b"isom\0\0\0\0isom"), "video/mp4", mp4_error),
            ("mp3-duration", b"ID3\x04\0\0\0\0\0\0", "audio/mpeg", duration_error),
            ("mp4-duration", b"\0\0\0\x18ftypisom" + b"\0" * 12, "video/mp4", duration_error),
            ("webm-duration", b"\x1aE\xdf\xa3\x81\0", "video/webm", duration_error),
            ("unknown", b"not an asset", "image/gif", "."),
            ("short-png", b"\x89PNG\r\n\x1a\nshort", "image/png", "."),
            ("empty-gif", b"GIF89a" + b"\0" * 7, "image/gif", "."),
            ("incomplete-pdf", b"%PDF-1.7\nmissing trailer", "application/pdf", "."),
        )
        for name, content, declared, error in cases:
            with self.subTest(case=name), self.assertRaisesRegex(AssetInspectionError, error):
                inspect_asset(content, declared)

    def test_declared_type_matches_bytes_without_disclosing_detected_type(self):
        metadata, concrete = inspect_asset(b"private custom format\0\x01", "application/octet-stream; charset=binary")
        self.assertEqual(concrete, "application/octet-stream")
        self.assertIsNone(metadata.width)
        for declared in ("application/octet-stream", "application/pdf"):
            with self.subTest(declared=declared), self.assertRaisesRegex(
                AssetInspectionError, "^asset type does not match declared media type$"
            ):
                inspect_asset(_png(2, 2), declared)


class AssetPolicyTests(unittest.TestCase):
    def test_stored_is_explicit_annotation_metadata(self):
        policy = Stored(
            store="media",
            path="screenshots",
            expires_in=30,
            retention=timedelta(days=7),
        )

        class Result(BaseModel):
            screenshot: Annotated[Image, policy]

        self.assertEqual(
            storage_policy(Result.model_fields["screenshot"].metadata), policy
        )
        self.assertEqual(storage_policy(Annotated[Image, policy]), policy)
        schema = Result.model_json_schema()["properties"]["screenshot"]
        self.assertEqual(schema["x-harnest-storage"]["store"], "media")
        self.assertEqual(schema["x-harnest-storage"]["expiresIn"], 30)
        self.assertEqual(policy.retention_seconds, 7 * 24 * 60 * 60)

    def test_stored_rejects_ambiguous_or_unsafe_configuration(self):
        for kwargs in (
            {"store": "not/valid"},
            {"path": "../private"},
            {"path": "/absolute"},
            {"expires_in": 0},
            {"retention": timedelta(0)},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                Stored(**kwargs)


if __name__ == "__main__":
    unittest.main()

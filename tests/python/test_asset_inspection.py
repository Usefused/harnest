from __future__ import annotations

import struct
import unittest

from harnest.asset_inspection import AssetInspectionError, inspect_asset


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
    def test_inspects_supported_images(self):
        cases = (
            (_png(20, 10), "IMAGE/PNG", "image/png", (20, 10), 1),
            (_png(20, 10, 3), "image/png", "image/png", (20, 10), 3),
            (_jpeg(31, 17), "image/jpeg", "image/jpeg", (31, 17), 1),
            (_gif(12, 9, 2), "image/gif", "image/gif", (12, 9), 2),
            (_webp_lossless(24, 13), "image/webp", "image/webp", (24, 13), 1),
        )
        for content, declared, expected, dimensions, frames in cases:
            with self.subTest(expected=expected, dimensions=dimensions):
                metadata, concrete = inspect_asset(content, declared)
                self.assertEqual(concrete, expected)
                self.assertEqual((metadata.width, metadata.height), dimensions)
                self.assertEqual(metadata.frame_count, frames)

    def test_recognizes_pdf_without_guessing_page_count(self):
        metadata, concrete = inspect_asset(
            b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n",
            "application/pdf",
        )

        self.assertEqual(concrete, "application/pdf")
        self.assertIsNone(metadata.page_count)

    def test_derives_wav_duration_and_channel_metadata(self):
        metadata, concrete = inspect_asset(_wav(2), "audio/wav")

        self.assertEqual(concrete, "audio/wav")
        self.assertEqual(metadata.duration_seconds, 2)
        self.assertEqual(metadata.channel_count, 1)
        self.assertEqual(metadata.sample_rate_hz, 8_000)

    def test_derives_mp4_duration_and_dimensions(self):
        for version in (0, 1):
            with self.subTest(version=version):
                metadata, concrete = inspect_asset(
                    _mp4(movie_header_version=version),
                    "video/mp4",
                )
                self.assertEqual(concrete, "video/mp4")
                self.assertEqual(metadata.duration_seconds, 2)
                self.assertEqual((metadata.width, metadata.height), (320, 240))

    def test_rejects_mp4_over_duration_and_pixel_ceilings(self):
        cases = (
            (_mp4(duration=301_000), "duration limit"),
            (_mp4(width=4_001, height=4_000), "pixel limit"),
        )
        for content, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(AssetInspectionError, error):
                    inspect_asset(content, "video/mp4")

    def test_fragmented_or_uninspectable_mp4_fails_closed(self):
        cases = (
            _mp4(fragmented=True),
            _mp4(timescale=0),
            _mp4_box(b"ftyp", b"isom\0\0\0\0isom"),
            b"\0\0\0\x18ftypisom" + b"\0" * 12,
        )
        for content in cases:
            with self.subTest(size=len(content)):
                with self.assertRaisesRegex(
                    AssetInspectionError,
                    "duration could not be established|malformed",
                ):
                    inspect_asset(content, "video/mp4")

    def test_accepts_unknown_content_only_as_generic_binary(self):
        metadata, concrete = inspect_asset(
            b"private custom format\0\x01",
            "application/octet-stream; charset=binary",
        )

        self.assertEqual(concrete, "application/octet-stream")
        self.assertIsNone(metadata.width)
        with self.assertRaisesRegex(AssetInspectionError, "does not match"):
            inspect_asset(_png(2, 2), "application/octet-stream")

    def test_rejects_magic_byte_mismatch_without_echoing_values(self):
        with self.assertRaisesRegex(
            AssetInspectionError,
            "^asset type does not match declared media type$",
        ) as captured:
            inspect_asset(_png(2, 2), "application/pdf")

        self.assertNotIn("png", str(captured.exception))
        self.assertNotIn("pdf", str(captured.exception))

    def test_enforces_size_before_format_parsing(self):
        with self.assertRaisesRegex(AssetInspectionError, "size limit"):
            inspect_asset(b"x" * (10 * 1024 * 1024 + 1), "image/png")

    def test_enforces_inspected_image_pixel_limit(self):
        with self.assertRaisesRegex(AssetInspectionError, "pixel limit"):
            inspect_asset(_png(4_001, 4_000), "image/png")

    def test_enforces_inspected_media_duration_limit(self):
        with self.assertRaisesRegex(AssetInspectionError, "duration limit"):
            inspect_asset(_wav(301, sample_rate=100), "audio/wav")

    def test_media_without_authoritative_duration_fails_closed(self):
        cases = (
            (b"ID3\x04\0\0\0\0\0\0", "audio/mpeg"),
            (b"\0\0\0\x18ftypisom" + b"\0" * 12, "video/mp4"),
            (b"\x1aE\xdf\xa3\x81\0", "video/webm"),
        )
        for content, declared in cases:
            with self.subTest(declared=declared):
                with self.assertRaisesRegex(
                    AssetInspectionError,
                    "^asset media duration could not be established$",
                ):
                    inspect_asset(content, declared)

    def test_rejects_unknown_or_malformed_content(self):
        cases = (
            (b"not an asset", "image/gif"),
            (b"\x89PNG\r\n\x1a\nshort", "image/png"),
            (b"GIF89a" + b"\0" * 7, "image/gif"),
            (b"%PDF-1.7\nmissing trailer", "application/pdf"),
        )
        for content, declared in cases:
            with self.subTest(declared=declared):
                with self.assertRaises(AssetInspectionError):
                    inspect_asset(content, declared)


if __name__ == "__main__":
    unittest.main()

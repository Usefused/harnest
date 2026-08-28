"""Bounded, dependency-free inspection of uploaded asset content."""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterator

from .assets import AssetMediaMetadata


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_MAX_ASSET_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_MEDIA_DURATION_SECONDS = 300
_ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/octet-stream",
        "audio/mpeg",
        "audio/wav",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/webm",
    }
)


class AssetInspectionError(ValueError):
    """Reject an asset without reflecting user-controlled content."""


def inspect_asset(
    content: bytes,
    declared_media_type: str,
) -> tuple[AssetMediaMetadata, str]:
    """Inspect bounded bytes and return trusted metadata plus concrete MIME."""

    data = _require_content(content)
    declared = _normalize_declared_media_type(declared_media_type)
    concrete, inspector = _detect_format(data, declared)
    if concrete != declared:
        raise AssetInspectionError("asset type does not match declared media type")
    if concrete not in _ALLOWED_MEDIA_TYPES:
        raise AssetInspectionError("asset media type is not allowed")
    metadata = inspector(data)
    _enforce_metadata_policy(metadata, concrete)
    return metadata, concrete


def _require_content(content: bytes) -> bytes:
    """Reject mutable, empty, or over-limit input before parsing."""

    if not isinstance(content, bytes) or not content:
        raise AssetInspectionError("asset content is invalid")
    if len(content) > _MAX_ASSET_BYTES:
        raise AssetInspectionError("asset exceeds server size limit")
    return content


def _normalize_declared_media_type(value: str) -> str:
    """Normalize a concrete Content-Type while excluding parameters."""

    if not isinstance(value, str):
        raise AssetInspectionError("asset media type is invalid")
    normalized = value.partition(";")[0].strip().lower()
    if not _MEDIA_TYPE_PATTERN.fullmatch(normalized):
        raise AssetInspectionError("asset media type is invalid")
    return normalized


def _detect_format(
    data: bytes,
    declared_media_type: str,
) -> tuple[str, Callable[[bytes], AssetMediaMetadata]]:
    """Choose an inspector solely from trusted file signatures."""

    detectors = (
        (_is_png, "image/png", _inspect_png),
        (_is_jpeg, "image/jpeg", _inspect_jpeg),
        (_is_gif, "image/gif", _inspect_gif),
        (_is_webp, "image/webp", _inspect_webp),
        (_is_pdf, "application/pdf", _inspect_pdf),
        (_is_wav, "audio/wav", _inspect_wav),
        (_is_mp3, "audio/mpeg", _duration_unavailable),
        (_is_mp4, "video/mp4", _inspect_mp4),
        (_is_webm, "video/webm", _duration_unavailable),
    )
    for detector, media_type, inspector in detectors:
        if detector(data):
            return media_type, inspector
    if declared_media_type == "application/octet-stream":
        return declared_media_type, _inspect_opaque
    raise AssetInspectionError("asset format is unsupported")


def _enforce_metadata_policy(
    metadata: AssetMediaMetadata,
    media_type: str,
) -> None:
    """Apply hard server ceilings only to independently inspected values."""

    if media_type.startswith("image/"):
        _enforce_pixel_policy(metadata, dimensions_required=True)
    elif media_type.startswith("video/"):
        _enforce_pixel_policy(metadata, dimensions_required=False)
    if media_type.startswith(("audio/", "video/")):
        if metadata.duration_seconds is None:
            raise AssetInspectionError(
                "asset media duration could not be established"
            )
        if metadata.duration_seconds > _MAX_MEDIA_DURATION_SECONDS:
            raise AssetInspectionError("asset media exceeds server duration limit")


def _enforce_pixel_policy(
    metadata: AssetMediaMetadata,
    *,
    dimensions_required: bool,
) -> None:
    """Enforce dimensions while allowing audio-only ISO-BMFF movies."""

    dimensions = (metadata.width, metadata.height)
    if dimensions == (None, None) and not dimensions_required:
        return
    if metadata.width is None or metadata.height is None:
        raise _malformed()
    if metadata.width * metadata.height > _MAX_IMAGE_PIXELS:
        raise AssetInspectionError("asset image exceeds server pixel limit")


def _is_png(data: bytes) -> bool:
    return data.startswith(_PNG_SIGNATURE)


def _is_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8")


def _is_gif(data: bytes) -> bool:
    return data.startswith((b"GIF87a", b"GIF89a"))


def _is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _is_mp3(data: bytes) -> bool:
    return data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    )


def _is_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _is_webm(data: bytes) -> bool:
    return data.startswith(b"\x1aE\xdf\xa3")


def _malformed() -> AssetInspectionError:
    """Keep all parser failures stable and free of content-derived values."""

    return AssetInspectionError("asset content is malformed")


def _inspect_png(data: bytes) -> AssetMediaMetadata:
    """Read PNG dimensions and the authoritative APNG frame declaration."""

    width, height = _png_dimensions(data)
    frame_count = _png_frame_count(data)
    return AssetMediaMetadata(
        width=width, height=height, frame_count=frame_count
    )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Validate the required first PNG chunk and return its canvas size."""

    if len(data) < 33 or data[12:16] != b"IHDR" or _u32be(data, 8) != 13:
        raise _malformed()
    width, height = struct.unpack_from(">II", data, 16)
    if width == 0 or height == 0:
        raise _malformed()
    return width, height


def _png_frame_count(data: bytes) -> int:
    """Walk bounded chunks to find APNG metadata and a required terminator."""

    frame_count = 1
    offset = 8
    saw_end = False
    while offset + 12 <= len(data):
        chunk_size = _u32be(data, offset)
        chunk_end = offset + 12 + chunk_size
        if chunk_end > len(data):
            raise _malformed()
        chunk_type = data[offset + 4 : offset + 8]
        if chunk_type == b"acTL":
            if chunk_size != 8:
                raise _malformed()
            frame_count = _u32be(data, offset + 8)
            if frame_count == 0:
                raise _malformed()
        if chunk_type == b"IEND":
            saw_end = chunk_size == 0
            break
        offset = chunk_end
    if not saw_end:
        raise _malformed()
    return frame_count


def _inspect_jpeg(data: bytes) -> AssetMediaMetadata:
    """Walk JPEG marker segments to a supported start-of-frame marker."""

    offset = 2
    while offset < len(data):
        marker, offset = _next_jpeg_marker(data, offset)
        if marker in _JPEG_SOF_MARKERS:
            return _jpeg_dimensions(data, offset)
        if marker in (0xD9, 0xDA):
            break
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        offset = _skip_jpeg_segment(data, offset)
    raise _malformed()


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD9)})


def _next_jpeg_marker(data: bytes, offset: int) -> tuple[int, int]:
    """Consume JPEG padding and return a marker plus its payload offset."""

    if offset >= len(data) or data[offset] != 0xFF:
        raise _malformed()
    while offset < len(data) and data[offset] == 0xFF:
        offset += 1
    if offset >= len(data) or data[offset] == 0:
        raise _malformed()
    return data[offset], offset + 1


def _skip_jpeg_segment(data: bytes, offset: int) -> int:
    """Skip a length-delimited JPEG segment without scanning its payload."""

    if offset + 2 > len(data):
        raise _malformed()
    size = _u16be(data, offset)
    if size < 2 or offset + size > len(data):
        raise _malformed()
    return offset + size


def _jpeg_dimensions(data: bytes, offset: int) -> AssetMediaMetadata:
    """Decode a JPEG SOF segment after checking its fixed prefix."""

    if offset + 7 > len(data):
        raise _malformed()
    segment_size = _u16be(data, offset)
    if segment_size < 7 or offset + segment_size > len(data):
        raise _malformed()
    height = _u16be(data, offset + 3)
    width = _u16be(data, offset + 5)
    if width == 0 or height == 0:
        raise _malformed()
    return AssetMediaMetadata(width=width, height=height, frame_count=1)


def _inspect_gif(data: bytes) -> AssetMediaMetadata:
    """Parse GIF blocks to count frames without inspecting compressed pixels."""

    if len(data) < 13:
        raise _malformed()
    width, height = struct.unpack_from("<HH", data, 6)
    if width == 0 or height == 0:
        raise _malformed()
    offset = 13 + _gif_color_table_size(data[10])
    frames = 0
    while offset < len(data):
        introducer = data[offset]
        offset += 1
        if introducer == 0x3B:
            if frames == 0:
                raise _malformed()
            return AssetMediaMetadata(
                width=width, height=height, frame_count=frames
            )
        if introducer == 0x21:
            offset = _skip_gif_extension(data, offset)
        elif introducer == 0x2C:
            offset = _skip_gif_image(data, offset, width, height)
            frames += 1
        else:
            raise _malformed()
    raise _malformed()


def _gif_color_table_size(packed: int) -> int:
    """Return the optional color table byte count encoded by GIF flags."""

    return 3 * (1 << ((packed & 0x07) + 1)) if packed & 0x80 else 0


def _skip_gif_extension(data: bytes, offset: int) -> int:
    """Skip an extension label and its length-prefixed data sub-blocks."""

    if offset >= len(data):
        raise _malformed()
    return _skip_gif_sub_blocks(data, offset + 1)


def _skip_gif_image(
    data: bytes,
    offset: int,
    canvas_width: int,
    canvas_height: int,
) -> int:
    """Skip an image descriptor, local palette, and compressed data."""

    if offset + 9 > len(data):
        raise _malformed()
    left, top, width, height = struct.unpack_from("<HHHH", data, offset)
    if width == 0 or height == 0:
        raise _malformed()
    # Reject frames that could force allocation beyond the inspected canvas.
    if left + width > canvas_width or top + height > canvas_height:
        raise _malformed()
    packed = data[offset + 8]
    offset += 9 + _gif_color_table_size(packed)
    if offset >= len(data):
        raise _malformed()
    return _skip_gif_sub_blocks(data, offset + 1)


def _skip_gif_sub_blocks(data: bytes, offset: int) -> int:
    """Walk GIF sub-blocks until their zero terminator."""

    while offset < len(data):
        size = data[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
        if offset > len(data):
            raise _malformed()
    raise _malformed()


def _inspect_webp(data: bytes) -> AssetMediaMetadata:
    """Decode dimensions from a bounded WebP framing chunk."""

    riff_size = _u32le(data, 4)
    if riff_size + 8 > len(data):
        raise _malformed()
    chunks = list(_webp_chunks(data, min(len(data), riff_size + 8)))
    for kind, payload in chunks:
        if kind == b"VP8X":
            return _inspect_webp_extended(payload, chunks)
        if kind == b"VP8 ":
            return _inspect_webp_lossy(payload)
        if kind == b"VP8L":
            return _inspect_webp_lossless(payload)
    raise _malformed()


def _webp_chunks(data: bytes, end: int) -> Iterator[tuple[bytes, bytes]]:
    """Yield checked RIFF chunks without copying their payloads."""

    offset = 12
    while offset + 8 <= end:
        size = _u32le(data, offset + 4)
        payload_start = offset + 8
        payload_end = payload_start + size
        if payload_end > end:
            raise _malformed()
        yield data[offset : offset + 4], data[payload_start:payload_end]
        offset = payload_end + (size & 1)
    if offset != end:
        raise _malformed()


def _inspect_webp_extended(
    payload: bytes,
    chunks: list[tuple[bytes, bytes]],
) -> AssetMediaMetadata:
    """Read a VP8X canvas and count animation frame chunks."""

    if len(payload) != 10:
        raise _malformed()
    width = int.from_bytes(payload[4:7], "little") + 1
    height = int.from_bytes(payload[7:10], "little") + 1
    frame_payloads = [body for kind, body in chunks if kind == b"ANMF"]
    for frame_payload in frame_payloads:
        _validate_webp_frame(frame_payload, width, height)
    animated = bool(payload[0] & 0x02)
    if animated != bool(frame_payloads):
        raise _malformed()
    return AssetMediaMetadata(
        width=width, height=height, frame_count=max(1, len(frame_payloads))
    )


def _validate_webp_frame(
    payload: bytes,
    canvas_width: int,
    canvas_height: int,
) -> None:
    """Ensure an animated frame cannot exceed its trusted VP8X canvas."""

    if len(payload) < 16:
        raise _malformed()
    left = 2 * int.from_bytes(payload[0:3], "little")
    top = 2 * int.from_bytes(payload[3:6], "little")
    width = int.from_bytes(payload[6:9], "little") + 1
    height = int.from_bytes(payload[9:12], "little") + 1
    if left + width > canvas_width or top + height > canvas_height:
        raise _malformed()


def _inspect_webp_lossy(payload: bytes) -> AssetMediaMetadata:
    """Read dimensions from a lossy VP8 key-frame header."""

    if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
        raise _malformed()
    width = _u16le(payload, 6) & 0x3FFF
    height = _u16le(payload, 8) & 0x3FFF
    if width == 0 or height == 0:
        raise _malformed()
    return AssetMediaMetadata(width=width, height=height, frame_count=1)


def _inspect_webp_lossless(payload: bytes) -> AssetMediaMetadata:
    """Read packed dimensions from a lossless VP8L header."""

    if len(payload) < 5 or payload[0] != 0x2F:
        raise _malformed()
    packed = int.from_bytes(payload[1:5], "little")
    width = (packed & 0x3FFF) + 1
    height = ((packed >> 14) & 0x3FFF) + 1
    return AssetMediaMetadata(width=width, height=height, frame_count=1)


def _inspect_pdf(data: bytes) -> AssetMediaMetadata:
    """Recognize a bounded PDF while leaving non-authoritative counts unset."""

    if b"%%EOF" not in data[-1024:]:
        raise _malformed()
    return AssetMediaMetadata()


def _inspect_wav(data: bytes) -> AssetMediaMetadata:
    """Derive PCM WAV duration from checked format and data chunks."""

    if _u32le(data, 4) + 8 != len(data):
        raise _malformed()
    fmt: tuple[int, int, int, int] | None = None
    data_bytes = 0
    for kind, payload in _riff_chunks(data):
        if kind == b"fmt ":
            fmt = _parse_wav_format(payload)
        elif kind == b"data":
            data_bytes += len(payload)
    if fmt is None or data_bytes == 0:
        raise _malformed()
    channels, sample_rate, byte_rate, block_align = fmt
    if data_bytes % block_align:
        raise _malformed()
    return AssetMediaMetadata(
        duration_seconds=data_bytes / byte_rate,
        channel_count=channels,
        sample_rate_hz=sample_rate,
    )


def _inspect_mp4(data: bytes) -> AssetMediaMetadata:
    """Inspect a complete, non-fragmented ISO-BMFF movie."""

    boxes = list(_mp4_boxes(data, 0, len(data)))
    if not boxes or boxes[0][0] != b"ftyp":
        raise _malformed()
    if boxes[0][2] - boxes[0][1] < 8:
        raise _malformed()
    if any(kind == b"moof" for kind, _, _ in boxes):
        raise AssetInspectionError(
            "asset media duration could not be established"
        )
    movies = [(start, end) for kind, start, end in boxes if kind == b"moov"]
    if len(movies) != 1:
        raise AssetInspectionError(
            "asset media duration could not be established"
        )
    start, end = movies[0]
    return _inspect_mp4_movie(data, start, end)


def _inspect_mp4_movie(
    data: bytes,
    start: int,
    end: int,
) -> AssetMediaMetadata:
    """Read movie duration and the largest checked track dimensions."""

    children = list(_mp4_boxes(data, start, end))
    if any(kind == b"mvex" for kind, _, _ in children):
        raise AssetInspectionError(
            "asset media duration could not be established"
        )
    headers = [
        (box_start, box_end)
        for kind, box_start, box_end in children
        if kind == b"mvhd"
    ]
    if len(headers) != 1:
        raise AssetInspectionError(
            "asset media duration could not be established"
        )
    duration = _parse_mp4_duration(data, *headers[0])
    dimensions = _mp4_track_dimensions(data, children)
    width, height = dimensions if dimensions is not None else (None, None)
    return AssetMediaMetadata(
        width=width,
        height=height,
        duration_seconds=duration,
    )


def _mp4_boxes(
    data: bytes,
    start: int,
    end: int,
) -> Iterator[tuple[bytes, int, int]]:
    """Yield checked ISO-BMFF box payload bounds without copying content."""

    offset = start
    while offset < end:
        if end - offset < 8:
            raise _malformed()
        size = _u32be(data, offset)
        kind = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if end - offset < 16:
                raise _malformed()
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            raise _malformed()
        yield kind, offset + header_size, offset + size
        offset += size


def _parse_mp4_duration(data: bytes, start: int, end: int) -> float:
    """Decode an mvhd timescale and duration for supported full-box versions."""

    if start >= end:
        raise _malformed()
    version = data[start]
    if version == 0:
        return _mp4_duration_fields(data, start, end, 12, 16, 100, 0xFFFFFFFF)
    if version == 1:
        return _mp4_duration_fields(
            data,
            start,
            end,
            20,
            24,
            112,
            0xFFFFFFFFFFFFFFFF,
        )
    raise AssetInspectionError("asset media duration could not be established")


def _mp4_duration_fields(
    data: bytes,
    start: int,
    end: int,
    timescale_offset: int,
    duration_offset: int,
    minimum_size: int,
    unknown_duration: int,
) -> float:
    """Read checked mvhd fields shared by version-specific layouts."""

    if end - start < minimum_size:
        raise _malformed()
    duration_size = 8 if unknown_duration > 0xFFFFFFFF else 4
    timescale = _u32be(data, start + timescale_offset)
    duration = int.from_bytes(
        data[start + duration_offset : start + duration_offset + duration_size],
        "big",
    )
    if timescale == 0 or duration == unknown_duration:
        raise AssetInspectionError(
            "asset media duration could not be established"
        )
    return duration / timescale


def _mp4_track_dimensions(
    data: bytes,
    movie_children: list[tuple[bytes, int, int]],
) -> tuple[int, int] | None:
    """Return dimensions from the largest valid visual track, when present."""

    dimensions: list[tuple[int, int]] = []
    for kind, start, end in movie_children:
        if kind != b"trak":
            continue
        track_dimensions = _inspect_mp4_track(data, start, end)
        if track_dimensions is not None:
            dimensions.append(track_dimensions)
    if not dimensions:
        return None
    return max(dimensions, key=lambda value: value[0] * value[1])


def _inspect_mp4_track(
    data: bytes,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    """Decode one required tkhd and return non-zero fixed-point dimensions."""

    children = list(_mp4_boxes(data, start, end))
    headers = [
        (box_start, box_end)
        for kind, box_start, box_end in children
        if kind == b"tkhd"
    ]
    if len(headers) != 1:
        raise _malformed()
    header_start, header_end = headers[0]
    return _parse_mp4_track_dimensions(data, header_start, header_end)


def _parse_mp4_track_dimensions(
    data: bytes,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    """Decode tkhd 16.16 dimensions, rounding fractions up safely."""

    if start >= end:
        raise _malformed()
    expected_size = {0: 84, 1: 96}.get(data[start])
    if expected_size is None or end - start < expected_size:
        raise _malformed()
    width_fixed = _u32be(data, start + expected_size - 8)
    height_fixed = _u32be(data, start + expected_size - 4)
    if width_fixed == 0 and height_fixed == 0:
        return None
    if width_fixed == 0 or height_fixed == 0:
        raise _malformed()
    width = (width_fixed + 0xFFFF) >> 16
    height = (height_fixed + 0xFFFF) >> 16
    return width, height


def _inspect_opaque(_data: bytes) -> AssetMediaMetadata:
    """Accept unknown bounded content only under its generic binary type."""

    return AssetMediaMetadata()


def _riff_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield checked chunks from a RIFF container."""

    offset = 12
    while offset + 8 <= len(data):
        size = _u32le(data, offset + 4)
        start = offset + 8
        end = start + size
        if end > len(data):
            raise _malformed()
        yield data[offset : offset + 4], data[start:end]
        offset = end + (size & 1)
    if offset != len(data):
        raise _malformed()


def _parse_wav_format(payload: bytes) -> tuple[int, int, int, int]:
    """Accept only uncompressed WAV formats with internally consistent rates."""

    if len(payload) < 16:
        raise _malformed()
    format_tag, channels, sample_rate, byte_rate, block_align = struct.unpack_from(
        "<HHIIH", payload
    )
    values = (channels, sample_rate, byte_rate, block_align)
    if format_tag not in (1, 3) or min(values) <= 0:
        raise AssetInspectionError(
            "asset media duration could not be established"
        )
    if byte_rate != sample_rate * block_align:
        raise _malformed()
    return channels, sample_rate, byte_rate, block_align


def _duration_unavailable(_data: bytes) -> AssetMediaMetadata:
    """Fail closed where bounded inspection cannot establish duration."""

    raise AssetInspectionError("asset media duration could not be established")


def _u16be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _u16le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _u32le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")

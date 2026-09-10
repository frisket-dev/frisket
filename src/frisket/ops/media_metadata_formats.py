"""Persisted media format registry and byte-only signature recognition."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

MEDIA_METADATA_FORMAT_REGISTRY_VERSION = 1


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    # Defined before the full canonical-envelope helpers because these two
    # registry fingerprints are module constants.  Registry inputs contain
    # only ASCII JSON scalars, so this is byte-equivalent to
    # ``canonical_json_bytes`` without an import-time forward reference.
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


# This registry is a persisted normalization contract. Parser strings are
# evidence, never normalized output: only an exact alias below may produce a
# canonical format. Additions require a registry-version bump and a contract
# artifact update so cache compatibility changes with them.
MEDIA_METADATA_FORMAT_REGISTRY: dict[str, Any] = {
    "version": MEDIA_METADATA_FORMAT_REGISTRY_VERSION,
    "canonical_formats": (
        "pdf",
        "jpeg",
        "png",
        "gif",
        "webp",
        "tiff",
        "bmp",
        "heic",
        "avif",
        "flac",
        "wav",
        "aiff",
        "mp3",
        "aac",
        "ogg",
        "opus",
        "mp4",
        "quicktime",
        "matroska",
        "webm",
        "avi",
        "mpeg",
        "mpegts",
        "mxf",
    ),
    "extension_aliases": {
        "aac": "aac",
        "aif": "aiff",
        "aiff": "aiff",
        "avi": "avi",
        "avif": "avif",
        "bmp": "bmp",
        "flac": "flac",
        "gif": "gif",
        "heic": "heic",
        "jpe": "jpeg",
        "jpeg": "jpeg",
        "jpg": "jpeg",
        "m4a": "mp4",
        "m4v": "mp4",
        "mka": "matroska",
        "mkv": "matroska",
        "mov": "quicktime",
        "mp3": "mp3",
        "mp4": "mp4",
        "mpeg": "mpeg",
        "mpg": "mpeg",
        "m2ts": "mpegts",
        "mts": "mpegts",
        "mxf": "mxf",
        "oga": "ogg",
        "ogg": "ogg",
        "ogv": "ogg",
        "opus": "opus",
        "pdf": "pdf",
        "png": "png",
        "qt": "quicktime",
        "tif": "tiff",
        "tiff": "tiff",
        "ts": "mpegts",
        "wav": "wav",
        "wave": "wav",
        "webm": "webm",
        "webp": "webp",
    },
    "tool_aliases": {
        "pdf": ("pdf", "pdf", "application/pdf"),
        "jpeg": ("image", "jpeg", "image/jpeg"),
        "jpg": ("image", "jpeg", "image/jpeg"),
        "jpe": ("image", "jpeg", "image/jpeg"),
        "png": ("image", "png", "image/png"),
        "gif": ("image", "gif", "image/gif"),
        "webp": ("image", "webp", "image/webp"),
        "tiff": ("image", "tiff", "image/tiff"),
        "tif": ("image", "tiff", "image/tiff"),
        "bmp": ("image", "bmp", "image/bmp"),
        "heic": ("image", "heic", "image/heic"),
        "avif": ("image", "avif", "image/avif"),
        "flac": ("audio", "flac", "audio/flac"),
        "wav": ("audio", "wav", "audio/wav"),
        "wave": ("audio", "wav", "audio/wav"),
        "aiff": ("audio", "aiff", "audio/aiff"),
        "aif": ("audio", "aiff", "audio/aiff"),
        "mp3": ("audio", "mp3", "audio/mpeg"),
        "aac": ("audio", "aac", "audio/aac"),
        "ogg": ("audio", "ogg", "audio/ogg"),
        "opus": ("audio", "opus", "audio/ogg"),
        "m4a": ("audio", "mp4", "audio/mp4"),
        "mp4": ("video", "mp4", "video/mp4"),
        "mov": ("video", "quicktime", "video/quicktime"),
        "qt": ("video", "quicktime", "video/quicktime"),
        "mkv": ("video", "matroska", "video/x-matroska"),
        "matroska": ("video", "matroska", "video/x-matroska"),
        "webm": ("video", "webm", "video/webm"),
        "avi": ("video", "avi", "video/x-msvideo"),
        "mpeg": ("video", "mpeg", "video/mpeg"),
        "mpg": ("video", "mpeg", "video/mpeg"),
        "mpegts": ("video", "mpegts", "video/mp2t"),
        "m2ts": ("video", "mpegts", "video/mp2t"),
        "mts": ("video", "mpegts", "video/mp2t"),
        "mxf": ("video", "mxf", "application/mxf"),
    },
    "mime_aliases": {
        "application/pdf": ("pdf", "pdf", "application/pdf"),
        "image/jpeg": ("image", "jpeg", "image/jpeg"),
        "image/png": ("image", "png", "image/png"),
        "image/gif": ("image", "gif", "image/gif"),
        "image/webp": ("image", "webp", "image/webp"),
        "image/tiff": ("image", "tiff", "image/tiff"),
        "image/bmp": ("image", "bmp", "image/bmp"),
        "image/heic": ("image", "heic", "image/heic"),
        "image/avif": ("image", "avif", "image/avif"),
        "audio/flac": ("audio", "flac", "audio/flac"),
        "audio/wav": ("audio", "wav", "audio/wav"),
        "audio/x-wav": ("audio", "wav", "audio/wav"),
        "audio/aiff": ("audio", "aiff", "audio/aiff"),
        "audio/x-aiff": ("audio", "aiff", "audio/aiff"),
        "audio/mpeg": ("audio", "mp3", "audio/mpeg"),
        "audio/aac": ("audio", "aac", "audio/aac"),
        "audio/ogg": ("audio", "ogg", "audio/ogg"),
        "audio/opus": ("audio", "opus", "audio/ogg"),
        "audio/mp4": ("audio", "mp4", "audio/mp4"),
        "audio/x-m4a": ("audio", "mp4", "audio/mp4"),
        "audio/quicktime": ("audio", "quicktime", "audio/quicktime"),
        "audio/webm": ("audio", "webm", "audio/webm"),
        "audio/x-matroska": ("audio", "matroska", "audio/x-matroska"),
        "video/mp4": ("video", "mp4", "video/mp4"),
        "video/quicktime": ("video", "quicktime", "video/quicktime"),
        "video/x-matroska": ("video", "matroska", "video/x-matroska"),
        "video/webm": ("video", "webm", "video/webm"),
        "video/x-msvideo": ("video", "avi", "video/x-msvideo"),
        "video/mpeg": ("video", "mpeg", "video/mpeg"),
        "video/mp2t": ("video", "mpegts", "video/mp2t"),
        "video/m2ts": ("video", "mpegts", "video/mp2t"),
        "video/ogg": ("video", "ogg", "video/ogg"),
        "application/mxf": ("video", "mxf", "application/mxf"),
    },
    "ffprobe_families": {
        "matroska": ("matroska", "webm"),
        "iso_bmff": ("mov", "mp4", "m4a", "3gp", "3g2", "mj2"),
        "aliases": (
            "aac",
            "mp3",
            "flac",
            "wav",
            "ogg",
            "avi",
            "mpeg",
            "mpegts",
            "mxf",
        ),
    },
    "ambiguous_tool_aliases": (
        "mp4",
        "mov",
        "qt",
        "mkv",
        "matroska",
        "webm",
        "ogg",
    ),
    "unsupported_tool_mime_pairs": {
        "mpeg": ("audio/mpeg",),
        "mpg": ("audio/mpeg",),
    },
    "artwork_codec_mimes": {
        "bmp": "image/bmp",
        "gif": "image/gif",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "mjpeg": "image/jpeg",
        "png": "image/png",
        "tiff": "image/tiff",
        "webp": "image/webp",
    },
}
MEDIA_METADATA_FORMAT_REGISTRY_FINGERPRINT = _fingerprint(
    MEDIA_METADATA_FORMAT_REGISTRY
)


CONTENT_RECOGNIZER_FINGERPRINT = _fingerprint(
    {
        "version": 1,
        "formats": MEDIA_METADATA_FORMAT_REGISTRY["canonical_formats"],
        "format_registry_version": MEDIA_METADATA_FORMAT_REGISTRY_VERSION,
        "format_registry_fingerprint": MEDIA_METADATA_FORMAT_REGISTRY_FINGERPRINT,
        "kind_order": ("pdf", "image", "video", "audio", "other"),
    }
)


def _recognition(
    kind: str,
    fmt: str | None,
    mime: str | None,
    *,
    offset: int,
    confidence: int = 100,
    source: str = "content_signature",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "format": fmt,
        "mime": mime,
        "offset": max(0, int(offset)),
        "confidence": max(0, min(100, int(confidence))),
        "source": source,
    }


_BMFF_IMAGE_BRANDS = {
    b"avif": ("avif", "image/avif"),
    b"avis": ("avif", "image/avif"),
    b"heic": ("heic", "image/heic"),
    b"heim": ("heic", "image/heic"),
    b"heis": ("heic", "image/heic"),
    b"heix": ("heic", "image/heic"),
    b"hevc": ("heic", "image/heic"),
    b"hevm": ("heic", "image/heic"),
    b"hevs": ("heic", "image/heic"),
    b"hevx": ("heic", "image/heic"),
}
_BMFF_GENERIC_IMAGE_BRANDS = {b"mif1", b"msf1"}
_BMFF_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A "}
_BMFF_MP4_BRANDS = {
    b"3ge6",
    b"3ge7",
    b"3gg6",
    b"3gp4",
    b"3gp5",
    b"3gp6",
    b"3gp7",
    b"3gr6",
    b"3gs6",
    b"3gs7",
    b"M4V ",
    b"MSNV",
    b"avc1",
    b"iso2",
    b"isom",
    b"mj2s",
    b"mjp2",
    b"mp41",
    b"mp42",
}


def _bmff_ftyp_brands(data: bytes) -> tuple[bytes, tuple[bytes, ...]] | None:
    """Return bounded major/compatible brands from one leading ``ftyp`` box."""

    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    box_size = int.from_bytes(data[:4], "big")
    brand_offset = 8
    if box_size == 1:
        if len(data) < 24:
            return None
        box_size = int.from_bytes(data[8:16], "big")
        brand_offset = 16
    elif box_size == 0:
        box_size = len(data)
    minimum_size = brand_offset + 8
    if box_size < minimum_size:
        return None
    bounded_end = min(len(data), box_size)
    major = data[brand_offset : brand_offset + 4]
    compatible = tuple(
        data[offset : offset + 4]
        for offset in range(brand_offset + 8, bounded_end - 3, 4)
    )
    return major, compatible


def _bmff_image_identity(
    major: bytes, compatible: tuple[bytes, ...]
) -> tuple[str, str] | None:
    if major in _BMFF_IMAGE_BRANDS:
        return _BMFF_IMAGE_BRANDS[major]
    # mif1/msf1 are generic HEIF brands, not proof of HEIC. Prefer an exact
    # compatible image brand and leave a generic-only file to native parsers.
    if major in _BMFF_GENERIC_IMAGE_BRANDS or any(
        brand in _BMFF_IMAGE_BRANDS for brand in compatible
    ):
        identities = {
            identity
            for brand in compatible
            if (identity := _BMFF_IMAGE_BRANDS.get(brand)) is not None
        }
        if len(identities) == 1:
            return next(iter(identities))
    return None


def _is_unresolved_bmff_image(data: bytes) -> bool:
    ftyp = _bmff_ftyp_brands(data)
    if ftyp is None:
        return False
    major, compatible = ftyp
    return _bmff_image_identity(major, compatible) is None and (
        major in _BMFF_GENERIC_IMAGE_BRANDS
        or any(brand in _BMFF_GENERIC_IMAGE_BRANDS for brand in compatible)
        or any(brand in _BMFF_IMAGE_BRANDS for brand in compatible)
    )


def _ebml_vint(data: bytes, offset: int) -> tuple[int, int] | None:
    if offset >= len(data):
        return None
    first = data[offset]
    marker = 0x80
    length = 1
    while length <= 8 and not first & marker:
        marker >>= 1
        length += 1
    if length > 8 or offset + length > len(data):
        return None
    value = first & (marker - 1)
    for byte in data[offset + 1 : offset + length]:
        value = (value << 8) | byte
    if value == (1 << (7 * length)) - 1:
        return None
    return value, length


def _ebml_doctype(data: bytes) -> str | None:
    """Read the direct DocType child from a bounded leading EBML header."""

    if not data.startswith(b"\x1aE\xdf\xa3"):
        return None
    header_size = _ebml_vint(data, 4)
    if header_size is None:
        return None
    payload_size, size_length = header_size
    cursor = 4 + size_length
    header_end = min(len(data), cursor + payload_size, 4096)
    while cursor < header_end:
        first = data[cursor]
        marker = 0x80
        id_length = 1
        while id_length <= 4 and not first & marker:
            marker >>= 1
            id_length += 1
        if id_length > 4 or cursor + id_length > header_end:
            return None
        element_id = data[cursor : cursor + id_length]
        cursor += id_length
        element_size = _ebml_vint(data, cursor)
        if element_size is None:
            return None
        value_size, value_size_length = element_size
        cursor += value_size_length
        value_end = cursor + value_size
        if value_end > header_end:
            return None
        if element_id == b"B\x82":
            try:
                doctype = data[cursor:value_end].decode("ascii").strip().lower()
            except UnicodeDecodeError:
                return None
            return doctype or None
        cursor = value_end
    return None


def _is_mpeg_audio_frame(data: bytes) -> bool:
    if len(data) < 3 or data[0] != 0xFF or data[1] & 0xE0 != 0xE0:
        return False
    version = (data[1] >> 3) & 0x03
    layer = (data[1] >> 1) & 0x03
    bitrate_index = (data[2] >> 4) & 0x0F
    sample_rate_index = (data[2] >> 2) & 0x03
    return (
        version != 0x01
        and layer == 0x01
        and bitrate_index != 0x0F
        and sample_rate_index != 0x03
    )


def _is_adts_aac_frame(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0


def _id3v2_payload_offset(data: bytes) -> int | None:
    """Return the bounded first byte after one leading ID3v2 tag."""

    if not data.startswith(b"ID3") or len(data) < 10:
        return None
    size_bytes = data[6:10]
    if any(byte & 0x80 for byte in size_bytes):
        return None
    tag_size = 0
    for byte in size_bytes:
        tag_size = (tag_size << 7) | byte
    footer_size = 10 if data[5] & 0x10 else 0
    offset = 10 + tag_size + footer_size
    return offset if offset <= len(data) else None


def _recognize_prefix(data: bytes) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def add(
        kind: str, fmt: str, mime: str, offset: int = 0, confidence: int = 100
    ) -> None:
        found.append(
            _recognition(kind, fmt, mime, offset=offset, confidence=confidence)
        )

    if data.startswith(b"\xff\xd8\xff"):
        add("image", "jpeg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        add("image", "png", "image/png")
    if data.startswith((b"GIF87a", b"GIF89a")):
        add("image", "gif", "image/gif")
    if data.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        add("image", "tiff", "image/tiff")
    if data.startswith(b"BM"):
        add("image", "bmp", "image/bmp", confidence=95)
    if data.startswith(b"RIFF") and len(data) >= 12:
        form = data[8:12]
        if form == b"WEBP":
            add("image", "webp", "image/webp")
        elif form == b"WAVE":
            add("audio", "wav", "audio/wav")
        elif form == b"AVI ":
            # RIFF proves the AVI container, not stream topology. Leave room
            # for ffprobe's non-attached stream inventory to correct the kind.
            add("video", "avi", "video/x-msvideo", confidence=95)
    if data.startswith(b"FORM") and data[8:12] in {b"AIFF", b"AIFC"}:
        add("audio", "aiff", "audio/aiff")
    if data.startswith(b"fLaC"):
        add("audio", "flac", "audio/flac")
    if data.startswith(b"OggS"):
        sample = data[: 64 * 1024]
        if b"\x80theora" in sample:
            add("video", "ogg", "video/ogg")
        elif b"OpusHead" in sample:
            add("audio", "opus", "audio/ogg")
        else:
            add("audio", "ogg", "audio/ogg", confidence=90)
    audio_offset = _id3v2_payload_offset(data) if data.startswith(b"ID3") else 0
    if audio_offset is not None:
        audio_payload = data[audio_offset:]
        if _is_mpeg_audio_frame(audio_payload):
            add("audio", "mp3", "audio/mpeg", offset=audio_offset, confidence=90)
        if _is_adts_aac_frame(audio_payload):
            add("audio", "aac", "audio/aac", offset=audio_offset, confidence=90)
    if data.startswith(b"\x1aE\xdf\xa3"):
        ebml_doctype = _ebml_doctype(data)
        if ebml_doctype == "webm":
            add("video", "webm", "video/webm", confidence=95)
        elif ebml_doctype == "matroska":
            add("video", "matroska", "video/x-matroska", confidence=95)
    if (ftyp := _bmff_ftyp_brands(data)) is not None:
        brand, compatible_brands = ftyp
        if image_identity := _bmff_image_identity(brand, compatible_brands):
            fmt, mime = image_identity
            add("image", fmt, mime)
        elif brand == b"qt  ":
            add("video", "quicktime", "video/quicktime", confidence=95)
        elif brand in _BMFF_AUDIO_BRANDS:
            add("audio", "mp4", "audio/mp4")
        elif (
            brand in _BMFF_GENERIC_IMAGE_BRANDS
            or any(
                compatible in _BMFF_GENERIC_IMAGE_BRANDS
                for compatible in compatible_brands
            )
            or any(compatible in _BMFF_IMAGE_BRANDS for compatible in compatible_brands)
        ):
            # Generic HEIF brands do not identify the encoded image family.
            # ExifTool/ffprobe can provide an exact registry identity later.
            pass
        elif brand in _BMFF_MP4_BRANDS:
            add("video", "mp4", "video/mp4", confidence=95)
    # MPEG program streams have no prefix sniffer: their 4-byte start codes
    # are weak evidence, and exiftool/ffprobe identity (MPEG file type, the
    # "mpeg" demuxer alias) decides that container.
    # Require three packet sync bytes at a standard TS or M2TS stride. This is
    # bounded to the already-read prefix and avoids treating a lone 0x47 byte
    # as container evidence.
    if (
        len(data) >= 377 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47
    ) or (
        len(data) >= 389 and data[4] == 0x47 and data[196] == 0x47 and data[388] == 0x47
    ):
        add("video", "mpegts", "video/mp2t", confidence=95)
    pdf_offset = data.find(b"%PDF-", 0, min(len(data), 1024))
    if pdf_offset >= 0:
        add("pdf", "pdf", "application/pdf", pdf_offset)
    # Deterministic de-duplication; polyglot candidates are retained.
    unique = {canonical_json_bytes(item): item for item in found}
    return sorted(
        unique.values(),
        key=lambda item: (
            -int(item["confidence"]),
            int(item["offset"]),
            ("pdf", "image", "video", "audio", "other").index(item["kind"]),
            item["format"],
        ),
    )


def _select_recognition(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not candidates:
        return None, []
    ordered = sorted(
        candidates,
        key=lambda item: (
            -int(item.get("confidence") or 0),
            int(item.get("offset") or 0),
            ("pdf", "image", "video", "audio", "other").index(
                item.get("kind")
                if item.get("kind") in {"pdf", "image", "video", "audio", "other"}
                else "other"
            ),
            str(item.get("format") or ""),
        ),
    )
    winner = ordered[0]
    conflicts: list[dict[str, Any]] = []
    for candidate in ordered[1:]:
        winner_kind = winner.get("kind")
        candidate_kind = candidate.get("kind")
        winner_format = winner.get("format")
        candidate_format = candidate.get("format")
        if candidate_kind == winner_kind and (
            candidate_format == winner_format
            or candidate_format is None
            or winner_format is None
        ):
            # A kind-only recognition is deliberately noncommittal about the
            # canonical container; it cannot conflict with an exact format
            # recognition of the same media kind.
            continue
        conflicts.append(
            {
                "field": "format",
                "candidate_paths": [
                    f"recognition.{winner.get('source')}.{winner.get('format')}",
                    f"recognition.{candidate.get('source')}.{candidate.get('format')}",
                ],
                "selected_path": f"recognition.{winner.get('source')}.{winner.get('format')}",
                "reason": "polyglot_precedence",
            }
        )
    return winner, conflicts


def _same_container_family(left: str, right: str) -> bool:
    return bool(left and right) and (
        left == right
        or {left, right} <= {"mp4", "quicktime"}
        or {left, right} <= {"matroska", "webm"}
        or {left, right} <= {"ogg", "opus"}
    )


def _container_mime_for_kind(fmt: str, kind: str, fallback: Any) -> Any:
    mime_by_format = {
        "mp4": "audio/mp4" if kind == "audio" else "video/mp4",
        "quicktime": ("audio/quicktime" if kind == "audio" else "video/quicktime"),
        "matroska": ("audio/x-matroska" if kind == "audio" else "video/x-matroska"),
        "webm": "audio/webm" if kind == "audio" else "video/webm",
        "ogg": "audio/ogg" if kind == "audio" else "video/ogg",
        "opus": "audio/ogg",
    }
    return mime_by_format.get(fmt, fallback)


def _stream_kind_with_signature_container(
    signature: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep exact signature format while taking kind from stream topology."""

    result = dict(candidate)
    if not isinstance(signature, Mapping):
        return result
    signature_format = str(signature.get("format") or "")
    candidate_format = str(candidate.get("format") or "")
    kind = str(candidate.get("kind") or "other")
    if not signature_format and not candidate_format and kind in {"audio", "video"}:
        signature_mime = str(signature.get("mime") or "")
        if signature_mime.startswith(f"{kind}/") and not result.get("mime"):
            result["mime"] = signature_mime
        return result
    if not _same_container_family(signature_format, candidate_format):
        return result
    if signature_format == "opus" and candidate_format == "ogg" and kind == "video":
        # OpusHead proves an audio stream exists, not that the Ogg container is
        # audio-only. A non-attached video stream makes ffprobe authoritative.
        return result
    result["format"] = signature_format
    result["mime"] = _container_mime_for_kind(
        signature_format, kind, result.get("mime")
    )
    return result


def _signature_with_stream_topology(
    signature: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Correct container-only signature kind using ffprobe stream topology."""

    result = dict(signature)
    signature_format = str(signature.get("format") or "")
    candidate_format = str(candidate.get("format") or "")
    candidate_kind = str(candidate.get("kind") or "other")
    if not signature_format and candidate_kind in {"audio", "video"}:
        result["kind"] = candidate_kind
        signature_mime = str(signature.get("mime") or "")
        if not signature_mime.startswith(f"{candidate_kind}/"):
            result["mime"] = candidate.get("mime")
        return result
    if candidate_kind not in {"audio", "video"} or not _same_container_family(
        signature_format, candidate_format
    ):
        return result
    if (
        signature_format == "opus"
        and candidate_format == "ogg"
        and candidate_kind == "video"
    ):
        result["format"] = candidate_format
    result["kind"] = candidate_kind
    result["mime"] = _container_mime_for_kind(
        str(result.get("format") or ""), candidate_kind, candidate.get("mime")
    )
    return result


def _parser_kind_corrected_signature_container(
    signature: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Correct a generic container signature with exact parser MIME evidence."""

    result = dict(signature)
    signature_format = str(signature.get("format") or "")
    candidate_format = str(candidate.get("format") or "")
    candidate_kind = str(candidate.get("kind") or "other")
    if (
        signature_format not in {"mp4", "quicktime", "matroska", "webm"}
        or candidate_kind not in {"audio", "video"}
        or not _same_container_family(signature_format, candidate_format)
    ):
        return result
    result["kind"] = candidate_kind
    result["mime"] = _container_mime_for_kind(
        signature_format, candidate_kind, result.get("mime")
    )
    return result

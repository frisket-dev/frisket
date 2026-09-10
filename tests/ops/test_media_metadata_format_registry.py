from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import frisket.ops.media_metadata as metadata


_REQUIRED_EXTENSION_ALIASES = {
    "mka": "matroska",
    "m4v": "mp4",
    "oga": "ogg",
    "ogv": "ogg",
    "aif": "aiff",
    "wave": "wav",
    "mpg": "mpeg",
    "qt": "quicktime",
    "jpe": "jpeg",
}


def _overlay_template(canonical_format: str) -> dict[str, Any]:
    normalized = metadata._empty_normalized()
    normalized["format"] = canonical_format
    return {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": normalized,
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }


def test_schema_constructors_preserve_values_order_and_identity() -> None:
    nullable = metadata._nullable_string_schema(256)
    assert nullable == {"type": ["string", "null"], "maxLength": 256}
    assert list(nullable) == ["type", "maxLength"]
    assert nullable is not metadata._nullable_string_schema(256)

    first = metadata._bounded_string_array_schema(1024, 2, min_items=1, unique=True)
    second = metadata._bounded_string_array_schema(1024, 2, min_items=1, unique=True)
    assert first == {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": True,
        "items": {"type": "string", "maxLength": 1024},
    }
    assert list(first) == ["type", "minItems", "maxItems", "uniqueItems", "items"]
    assert first is not second
    assert first["items"] is not second["items"]


def test_extension_registry_covers_every_canonical_format_and_required_aliases() -> (
    None
):
    registry = metadata.MEDIA_METADATA_FORMAT_REGISTRY
    aliases = registry["extension_aliases"]

    assert registry["version"] == metadata.MEDIA_METADATA_FORMAT_REGISTRY_VERSION
    assert set(registry["canonical_formats"]) <= set(aliases.values())
    assert {key: aliases[key] for key in _REQUIRED_EXTENSION_ALIASES} == (
        _REQUIRED_EXTENSION_ALIASES
    )


@pytest.mark.parametrize(
    ("extension", "canonical_format"),
    sorted(_REQUIRED_EXTENSION_ALIASES.items()),
)
def test_reference_overlay_uses_extension_alias_registry_without_false_conflicts(
    extension: str, canonical_format: str
) -> None:
    envelope = metadata.overlay_reference_evidence(
        _overlay_template(canonical_format),
        filename=f"asset.{extension}",
        claimed_mime=None,
    )

    assert not any(
        conflict.get("reason") == "reference_extension_conflicts_with_content"
        for conflict in envelope["conflicts"]
    )


def test_reference_overlay_ignores_unregistered_extensions() -> None:
    envelope = metadata.overlay_reference_evidence(
        _overlay_template("mxf"),
        filename="asset.private-extension",
        claimed_mime=None,
    )

    assert envelope["conflicts"] == []

    generic_heif = metadata.overlay_reference_evidence(
        _overlay_template("avif"),
        filename="asset.heif",
        claimed_mime=None,
    )
    assert generic_heif["conflicts"] == []


def test_mxf_has_exact_exiftool_mime_and_ffprobe_identities() -> None:
    assert metadata._format_from_tool("MXF", "application/octet-stream") == (
        "video",
        "mxf",
        "application/mxf",
    )
    assert metadata._format_from_tool("", "application/mxf") == (
        "video",
        "mxf",
        "application/mxf",
    )
    assert metadata._format_from_ffprobe(
        "mxf",
        [{"codec_type": "video", "disposition": {}}],
    ) == ("video", "mxf", "application/mxf")


def test_generic_heif_tool_and_mime_evidence_are_not_claimed_as_heic() -> None:
    assert metadata._format_from_tool("HEIF", "image/heif") is None
    assert metadata._format_from_tool("", "image/heif") is None
    assert metadata._format_from_tool("HEIC", "image/heic") == (
        "image",
        "heic",
        "image/heic",
    )


@pytest.mark.parametrize(
    ("major_brand", "compatible_brand", "expected_format"),
    [
        (b"mif1", b"avif", "avif"),
        (b"mif1", b"heic", "heic"),
        (b"mp42", b"avif", "avif"),
        (b"hevs", b"isom", "heic"),
    ],
)
def test_bmff_compatible_image_brand_prevents_false_video_or_heic_identity(
    major_brand: bytes, compatible_brand: bytes, expected_format: str
) -> None:
    payload = major_brand + b"\x00\x00\x00\x00" + compatible_brand
    data = (8 + len(payload)).to_bytes(4, "big") + b"ftyp" + payload

    recognitions = metadata._recognize_prefix(data)

    assert [(item["kind"], item["format"]) for item in recognitions] == [
        ("image", expected_format)
    ]


def test_generic_heif_brand_without_specific_compatibility_is_not_guessed() -> None:
    payload = b"mif1" + b"\x00\x00\x00\x00" + b"mif1"
    data = (8 + len(payload)).to_bytes(4, "big") + b"ftyp" + payload

    assert metadata._recognize_prefix(data) == []

    isom_payload = b"isom" + b"\x00\x00\x00\x00" + b"mif1"
    isom_data = (8 + len(isom_payload)).to_bytes(4, "big") + b"ftyp" + isom_payload
    assert metadata._recognize_prefix(isom_data) == []

    cr3_payload = b"crx " + b"\x00\x00\x00\x00" + b"crx " + b"isom"
    cr3_data = (8 + len(cr3_payload)).to_bytes(4, "big") + b"ftyp" + cr3_payload
    assert metadata._recognize_prefix(cr3_data) == []


def test_ffprobe_video_topology_overrides_ogg_opus_audio_prefix() -> None:
    signature = metadata._recognition(
        "audio", "opus", "audio/ogg", offset=0, source="content_signature"
    )
    ffprobe = metadata._recognition(
        "video", "ogg", "video/ogg", offset=0, source="ffprobe"
    )

    corrected = metadata._stream_kind_with_signature_container(signature, ffprobe)
    corrected_signature = metadata._signature_with_stream_topology(signature, ffprobe)

    assert corrected["kind"] == "video"
    assert corrected["format"] == "ogg"
    assert corrected["mime"] == "video/ogg"
    selected, conflicts = metadata._select_recognition([corrected_signature, corrected])
    assert selected is not None and selected["format"] == "ogg"
    assert conflicts == []


def test_mpeg_ts_has_bounded_signature_registry_and_parser_support() -> None:
    data = bytearray(377)
    data[0] = data[188] = data[376] = 0x47

    assert metadata._recognize_prefix(bytes(data)) == [
        metadata._recognition(
            "video",
            "mpegts",
            "video/mp2t",
            offset=0,
            confidence=95,
        )
    ]
    assert metadata._format_from_tool("M2TS", "video/m2ts") == (
        "video",
        "mpegts",
        "video/mp2t",
    )
    assert metadata._format_from_ffprobe(
        "mpegts", [{"codec_type": "video", "disposition": {}}]
    ) == ("video", "mpegts", "video/mp2t")
    extension_aliases = metadata.MEDIA_METADATA_FORMAT_REGISTRY["extension_aliases"]
    assert {key: extension_aliases[key] for key in ("ts", "m2ts", "mts")} == {
        "ts": "mpegts",
        "m2ts": "mpegts",
        "mts": "mpegts",
    }


def test_adts_aac_does_not_also_emit_an_mp3_polyglot_candidate() -> None:
    # MPEG-4 AAC LC, 44.1 kHz, stereo ADTS header.
    recognitions = metadata._recognize_prefix(b"\xff\xf1\x50\x80\x00\x1f\xfc")

    assert [(item["format"], item["mime"]) for item in recognitions] == [
        ("aac", "audio/aac")
    ]
    selected, conflicts = metadata._select_recognition(recognitions)
    assert selected == recognitions[0]
    assert conflicts == []
    assert metadata._recognize_prefix(b"\xff\xfb\x90\x64")[0]["format"] == "mp3"

    id3_prefixed = b"ID3" + b"\x00" * 7 + b"\xff\xf1\x50\x80\x00\x1f\xfc"
    id3_recognitions = metadata._recognize_prefix(id3_prefixed)
    assert [(item["format"], item["offset"]) for item in id3_recognitions] == [
        ("aac", 10)
    ]
    assert metadata._format_from_ffprobe(
        "aac", [{"codec_type": "audio", "disposition": {}}]
    ) == ("audio", "aac", "audio/aac")


def test_mpeg_layer_two_is_not_claimed_as_mp3() -> None:
    assert metadata._recognize_prefix(b"\xff\xfd\xe0\xc4") == []
    assert metadata._format_from_tool("MP2", "audio/mpeg") == (
        "audio",
        None,
        "audio/mpeg",
    )
    assert metadata._format_from_tool("MPEG", "audio/mpeg") == (
        "audio",
        None,
        "audio/mpeg",
    )
    assert metadata._format_from_tool("MPEG", "video/mpeg") == (
        "video",
        "mpeg",
        "video/mpeg",
    )
    assert metadata._format_from_ffprobe(
        "mp3",
        [{"codec_type": "audio", "codec_name": "mp2", "disposition": {}}],
    ) == ("audio", None, None)
    assert metadata._format_from_ffprobe(
        "mp3",
        [{"codec_type": "audio", "codec_name": "mp3", "disposition": {}}],
    ) == ("audio", "mp3", "audio/mpeg")


def _ebml_header(*children: bytes) -> bytes:
    payload = b"".join(children)
    assert len(payload) < 127
    return b"\x1aE\xdf\xa3" + bytes([0x80 | len(payload)]) + payload


def _ebml_element(element_id: bytes, value: bytes) -> bytes:
    assert len(value) < 127
    return element_id + bytes([0x80 | len(value)]) + value


def test_webm_recognition_reads_ebml_doctype_instead_of_raw_substring() -> None:
    actual_doctype = _ebml_header(_ebml_element(b"B\x82", b"webm"))
    matroska_doctype = _ebml_header(_ebml_element(b"B\x82", b"matroska"))
    unknown_doctype = _ebml_header(_ebml_element(b"B\x82", b"notmatroska"))
    unrelated_text = _ebml_header(_ebml_element(b"B\x86", b"webm"))

    assert metadata._recognize_prefix(actual_doctype)[0]["format"] == "webm"
    assert metadata._recognize_prefix(matroska_doctype)[0]["format"] == "matroska"
    assert metadata._recognize_prefix(unknown_doctype) == []
    assert metadata._recognize_prefix(unrelated_text) == []
    assert metadata._recognize_prefix(b"\x1aE\xdf\xa3") == []


@pytest.mark.parametrize(
    ("format_name", "canonical_format", "mime"),
    [
        ("avi", "avi", "video/x-msvideo"),
        ("mpeg", "mpeg", "audio/mpeg"),
        ("mpegts", "mpegts", "video/mp2t"),
    ],
)
def test_ffprobe_audio_only_container_identity_uses_stream_topology(
    format_name: str, canonical_format: str, mime: str
) -> None:
    assert metadata._format_from_ffprobe(
        format_name,
        [{"codec_type": "audio", "disposition": {}}],
    ) == ("audio", canonical_format, mime)


@pytest.mark.parametrize(
    ("label", "format_name", "streams", "expected_kind"),
    [
        (
            "asf-wmv",
            "asf",
            [
                {"codec_type": "video", "codec_name": "wmv3", "disposition": {}},
                {"codec_type": "audio", "codec_name": "wmav2", "disposition": {}},
            ],
            "video",
        ),
        (
            "asf-wma",
            "asf",
            [{"codec_type": "audio", "codec_name": "wmav2", "disposition": {}}],
            "audio",
        ),
        (
            "flv",
            "flv",
            [{"codec_type": "video", "codec_name": "flv1", "disposition": {}}],
            "video",
        ),
    ],
)
def test_ffprobe_common_unregistered_containers_keep_stream_media_kind(
    label: str,
    format_name: str,
    streams: list[dict[str, object]],
    expected_kind: str,
) -> None:
    del label
    identity = metadata._format_from_ffprobe(format_name, streams)

    assert identity == (expected_kind, None, None)
    assert identity is not None
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["ffprobe"]["format_name"] = format_name
    normalized, _, _ = metadata._normalize(
        metadata._recognition(
            *identity,
            offset=0,
            confidence=80,
            source="ffprobe",
        ),
        sources,
        {"streams": streams, "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="a" * 64,
        size_bytes=1,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["kind"] == expected_kind
    assert normalized["format"] is None
    assert normalized["mime"] is None
    assert normalized["probe_status"] == "ok"


@pytest.mark.parametrize(
    ("file_type", "mime", "format_name", "codec_type", "expected_kind"),
    [
        ("ASF", "video/x-ms-asf", "asf", "audio", "audio"),
        ("WMA", "audio/x-ms-wma", "asf", "audio", "audio"),
        ("WMV", "video/x-ms-wmv", "asf", "video", "video"),
        ("FLV", "video/x-flv", "flv", "video", "video"),
    ],
)
def test_unregistered_common_av_containers_keep_kind_without_claiming_format(
    file_type: str,
    mime: str,
    format_name: str,
    codec_type: str,
    expected_kind: str,
) -> None:
    tool_identity = metadata._format_from_tool(file_type, mime)
    assert tool_identity is not None
    assert tool_identity[1] is None

    probe_identity = metadata._format_from_ffprobe(
        format_name,
        [{"codec_type": codec_type, "disposition": {}}],
    )
    assert probe_identity == (expected_kind, None, None)

    tool = metadata._recognition(
        *tool_identity, offset=0, confidence=85, source="exiftool"
    )
    probe = metadata._recognition(
        *probe_identity, offset=0, confidence=80, source="ffprobe"
    )
    selected, conflicts = metadata._select_recognition(
        [
            metadata._signature_with_stream_topology(tool, probe),
            metadata._stream_kind_with_signature_container(tool, probe),
        ]
    )

    assert selected is not None
    assert selected["kind"] == expected_kind
    assert selected["format"] is None
    assert conflicts == []


def test_audio_only_avi_topology_corrects_signature_without_polyglot_conflict() -> None:
    signature = metadata._recognize_prefix(b"RIFF\x00\x00\x00\x00AVI ")[0]
    identity = metadata._format_from_ffprobe(
        "avi", [{"codec_type": "audio", "disposition": {}}]
    )
    assert identity is not None
    candidate = metadata._recognition(
        *identity,
        offset=0,
        confidence=100,
        source="ffprobe",
    )
    corrected_signature = metadata._signature_with_stream_topology(signature, candidate)
    corrected_candidate = metadata._stream_kind_with_signature_container(
        signature, candidate
    )

    selected, conflicts = metadata._select_recognition(
        [corrected_signature, corrected_candidate]
    )

    assert selected is not None
    assert (selected["kind"], selected["format"], selected["mime"]) == (
        "audio",
        "avi",
        "video/x-msvideo",
    )
    assert conflicts == []


def test_unknown_signature_can_reach_ffprobe_and_become_mxf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "opaque.bin"
    media.write_bytes(b"no bounded signature for mxf")
    ffprobe_called = False

    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool", outcome="success", available=True, version="test"
        ),
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)

    def fake_ffprobe(*_args: Any, **_kwargs: Any) -> metadata._AdapterResult:
        nonlocal ffprobe_called
        ffprobe_called = True
        return metadata._AdapterResult(
            name="ffprobe",
            outcome="success",
            available=True,
            version="test",
            sources={"ffprobe": {"format_name": "mxf"}},
            structure={"streams": [], "chapters": [], "artwork": []},
            recognitions=[
                metadata._recognition(
                    "video",
                    "mxf",
                    "application/mxf",
                    offset=0,
                    confidence=100,
                    source="ffprobe",
                )
            ],
        )

    monkeypatch.setattr(metadata, "_ffprobe_adapter", fake_ffprobe)

    envelope = metadata.extract_media_metadata(
        media,
        digest="0" * 64,
        size_bytes=media.stat().st_size,
        filename="opaque.mxf",
        claimed_mime="application/mxf",
    )

    assert ffprobe_called is True
    assert envelope["normalized"]["kind"] == "video"
    assert envelope["normalized"]["format"] == "mxf"
    assert envelope["normalized"]["mime"] == "application/mxf"


def test_unresolved_generic_heif_is_not_silently_reclassified_by_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"isom" + b"\x00\x00\x00\x00" + b"mif1"
    media = tmp_path / "generic-heif.bin"
    media.write_bytes((8 + len(payload)).to_bytes(4, "big") + b"ftyp" + payload)
    ffprobe_called = False

    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
        ),
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)

    def fake_ffprobe(*_args: Any, **_kwargs: Any) -> metadata._AdapterResult:
        nonlocal ffprobe_called
        ffprobe_called = True
        raise AssertionError("generic HEIF must not be passed to ffprobe identity")

    monkeypatch.setattr(metadata, "_ffprobe_adapter", fake_ffprobe)

    envelope = metadata.extract_media_metadata(
        media,
        digest="1" * 64,
        size_bytes=media.stat().st_size,
        filename="generic.heif",
        claimed_mime="image/heif",
    )

    assert ffprobe_called is False
    assert envelope["normalized"]["kind"] == "other"
    assert envelope["normalized"]["format"] is None
    assert envelope["extraction"]["completeness"] == "terminal_unsupported"
    assert metadata.media_metadata_cache_compatible(
        metadata.cache_entry_from_envelope(envelope)
    )


def test_ffprobe_only_combined_mov_mp4_identity_is_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "ftyp-less-quicktime.bin"
    media.write_bytes(b"unbranded quicktime payload")
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool", outcome="missing_dependency", available=False
        ),
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    monkeypatch.setattr(
        metadata,
        "_ffprobe_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="ffprobe",
            outcome="success",
            available=True,
            version="test",
            sources={
                "ffprobe": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "format_long_name": "QuickTime / MOV",
                }
            },
            structure={"streams": [], "chapters": [], "artwork": []},
            recognitions=[
                metadata._recognition(
                    "video", "mp4", "video/mp4", offset=0, source="ffprobe"
                )
            ],
        ),
    )

    envelope = metadata.extract_media_metadata(
        media,
        digest="2" * 64,
        size_bytes=media.stat().st_size,
        filename="unbranded.mov",
        claimed_mime="video/quicktime",
    )

    assert envelope["normalized"]["format"] is None
    assert envelope["normalized"]["probe_status"] == "unsupported"

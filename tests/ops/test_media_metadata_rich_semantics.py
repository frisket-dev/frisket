from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import frisket.ops.media_metadata as metadata


def _run_ffprobe_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: dict[str, Any],
) -> metadata._AdapterResult:
    executable = tmp_path / "ffprobe"
    executable.write_bytes(b"fixture executable")
    media = tmp_path / "fixture.bin"
    media.write_bytes(b"fixture media")
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args, **_kwargs: "8.1.1",
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            0, json.dumps(body, sort_keys=True).encode(), b""
        ),
    )
    return metadata._ffprobe_adapter(media)


def _normalize_fixture(
    *,
    kind: str,
    sources: dict[str, dict[str, Any]],
    structure: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    return metadata._normalize(
        {
            "kind": kind,
            "format": "mp4" if kind == "video" else "flac",
            "mime": "video/mp4" if kind == "video" else "audio/flac",
            "source": "ffprobe",
        },
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="a" * 64,
        size_bytes=1_000,
        omitted=metadata._empty_omitted(),
    )


def test_video_rates_color_hdr_and_vfr_evidence_are_closed_and_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "10.0",
            "bit_rate": "12000000",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "duration": "10.0",
                "bit_rate": "11500000",
                "width": 3840,
                "height": 2160,
                "avg_frame_rate": "24000/1001",
                "r_frame_rate": "30000/1001",
                "pix_fmt": "yuv420p10le",
                "color_range": "tv",
                "color_space": "bt2020nc",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "chroma_location": "topleft",
                "field_order": "progressive",
                "tags": {"language": "x" * 1_000},
                "disposition": {"default": 1, "attached_pic": 0, "forced": 0},
                "side_data_list": [
                    {
                        "side_data_type": "Mastering display metadata",
                        "red_x": "34000/50000",
                        "red_y": "16000/50000",
                        "max_luminance": "10000000/10000",
                    },
                    {
                        "side_data_type": "Content light level metadata",
                        "max_content": 1000,
                        "max_average": 400,
                    },
                    {"side_data_type": "Display Matrix", "rotation": -90},
                ],
            }
        ],
    }

    result = _run_ffprobe_fixture(monkeypatch, tmp_path, body)
    assert result.outcome == "success"
    assert result.warnings == []
    stream = result.structure["streams"][0]
    assert stream["avg_frame_rate"] == "24000/1001"
    assert stream["r_frame_rate"] == "30000/1001"
    assert stream["vfr_evidence"] is True
    assert stream["rotation_degrees"] == -90
    assert stream["language"] == "x" * 256
    assert {key: stream[key] for key in metadata._COLOR_FIELDS} == {
        "pixel_format": "yuv420p10le",
        "color_range": "tv",
        "color_space": "bt2020nc",
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
        "chroma_location": "topleft",
        "field_order": "progressive",
    }
    assert stream["hdr_side_data"] == [
        {
            "type": "content_light_level",
            "values": {"max_average": "400", "max_content": "1000"},
        },
        {
            "type": "mastering_display",
            "values": {
                "max_luminance": "10000000/10000",
                "red_x": "34000/50000",
                "red_y": "16000/50000",
            },
        },
    ]
    assert stream["raw_technical"] == {}

    Draft202012Validator(
        {
            "$ref": "#/$defs/stream",
            "$defs": metadata.MEDIA_METADATA_ENVELOPE_SCHEMA["$defs"],
        }
    ).validate(stream)

    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["ffprobe"].update(result.sources["ffprobe"])
    structure = {**result.structure, "pages": {}}
    normalized, _, warnings = _normalize_fixture(
        kind="video", sources=sources, structure=structure
    )
    assert warnings == []
    assert normalized["frame_rate_fps"] == 23.976024
    assert normalized["width_pixels"] == 2160
    assert normalized["height_pixels"] == 3840


def test_malformed_technical_candidates_keep_raw_evidence_and_typed_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = {
        "format": {
            "format_name": "mov,mp4",
            "duration": "bad-duration",
            "bit_rate": "0",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "duration": "-2",
                "bit_rate": "12.5",
                "width": "1.5",
                "height": "-1",
                "sample_rate": "NaN",
                "channels": "two",
                "avg_frame_rate": "0/0",
                "r_frame_rate": "bogus",
                "tags": {"rotate": "Infinity"},
            }
        ],
    }

    result = _run_ffprobe_fixture(monkeypatch, tmp_path, body)
    stream = result.structure["streams"][0]
    assert stream["duration_seconds"] is None
    assert stream["bit_rate_bps"] is None
    assert stream["width_pixels"] is None
    assert stream["height_pixels"] is None
    assert stream["sample_rate_hz"] is None
    assert stream["channel_count"] is None
    assert stream["vfr_evidence"] is None
    assert stream["rotation_degrees"] is None
    assert stream["raw_technical"] == {
        "avg_frame_rate": "0/0",
        "bit_rate": "12.5",
        "channels": "two",
        "duration": "-2",
        "height": "-1",
        "r_frame_rate": "bogus",
        "rotation": "Infinity",
        "sample_rate": "NaN",
        "width": "1.5",
    }
    assert result.sources["ffprobe"]["raw_technical"] == {
        "bit_rate": "0",
        "duration": "bad-duration",
    }
    warning_subtypes = [item["subtype"] for item in result.warnings]
    assert warning_subtypes == [
        "avg_frame_rate",
        "r_frame_rate",
        "rotation",
        "duration",
        "bit_rate",
        "width",
        "height",
        "sample_rate",
        "channels",
        "duration",
        "bit_rate",
    ]
    assert {item["code"] for item in result.warnings} == {"invalid_value"}
    assert all(item["code"] in metadata.WARNING_CODES for item in result.warnings)


def test_music_positions_merge_split_vorbis_totals_without_losing_raw_values() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["vorbis"] = {
        "vorbis.disc_number": "01",
        "vorbis.total_discs": "02",
        "vorbis.track_number": "03",
        "vorbis.total_tracks": "12",
    }
    source_snapshot = copy.deepcopy(sources)
    structure = {
        "streams": [
            {
                "index": 0,
                "type": "audio",
                "codec": "flac",
                "disposition": {"default": True, "attached_pic": False},
                "tags": {},
            }
        ],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }

    _, _, warnings = _normalize_fixture(
        kind="audio", sources=sources, structure=structure
    )

    assert warnings == []
    assert sources["vorbis"] == source_snapshot["vorbis"]
    assert structure["music"] == {
        "track": {
            "raw": "03",
            "number": 3,
            "total": 12,
            "source_path": "sources.vorbis.vorbis.track_number",
            "total_raw": "12",
            "total_source_path": "sources.vorbis.vorbis.total_tracks",
        },
        "disc": {
            "raw": "01",
            "number": 1,
            "total": 2,
            "source_path": "sources.vorbis.vorbis.disc_number",
            "total_raw": "02",
            "total_source_path": "sources.vorbis.vorbis.total_discs",
        },
    }
    Draft202012Validator(
        {
            "$ref": "#/$defs/music",
            "$defs": metadata.MEDIA_METADATA_ENVELOPE_SCHEMA["$defs"],
        }
    ).validate(structure["music"])


def test_music_position_bounds_match_the_closed_schema_and_keep_overflow_raw() -> None:
    maximum_warnings: list[dict[str, Any]] = []
    maximum = metadata._music_position(
        str(metadata.MAX_MUSIC_POSITION),
        "sources.vorbis.track_number",
        subtype="track",
        warnings=maximum_warnings,
    )
    overflow_warnings: list[dict[str, Any]] = []
    overflow = metadata._music_position(
        str(metadata.MAX_MUSIC_POSITION + 1),
        "sources.vorbis.track_number",
        subtype="track",
        warnings=overflow_warnings,
    )
    total_overflow_warnings: list[dict[str, Any]] = []
    total_overflow = metadata._music_position(
        "3",
        "sources.vorbis.track_number",
        total_value=str(metadata.MAX_MUSIC_POSITION + 1),
        total_path="sources.vorbis.total_tracks",
        subtype="track",
        warnings=total_overflow_warnings,
    )

    assert maximum_warnings == []
    assert maximum is not None and maximum["number"] == metadata.MAX_MUSIC_POSITION
    assert overflow is not None
    assert overflow["raw"] == str(metadata.MAX_MUSIC_POSITION + 1)
    assert overflow["number"] is None
    assert [item["subtype"] for item in overflow_warnings] == ["track"]
    assert total_overflow is not None
    assert total_overflow["number"] == 3
    assert total_overflow["total"] is None
    assert total_overflow["total_raw"] == str(metadata.MAX_MUSIC_POSITION + 1)
    assert [item["subtype"] for item in total_overflow_warnings] == ["track_total"]
    validator = Draft202012Validator(
        {
            "$ref": "#/$defs/music_position",
            "$defs": metadata.MEDIA_METADATA_ENVELOPE_SCHEMA["$defs"],
        }
    )
    validator.validate(maximum)
    validator.validate(overflow)
    validator.validate(total_overflow)


def test_exiftool_numeric_mode_keeps_identifier_tags_as_strings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_bytes(b"fixture executable")
    media = tmp_path / "fixture.flac"
    media.write_bytes(b"fixture media")
    body = [
        {
            "[File]FileType": "FLAC",
            "[File]MIMEType": "audio/flac",
            "[Vorbis]TrackNumber": 3,
            "[Vorbis]TrackTotal": 12,
            "[Vorbis]DiscNumber": 1,
            "[Vorbis]DiscTotal": 2,
        }
    ]
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args, **_kwargs: "13.59",
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            0, json.dumps(body, sort_keys=True).encode(), b""
        ),
    )

    result = metadata._exiftool_adapter(media)

    assert result.sources["vorbis"] == {
        "vorbis.disc_number": "1",
        "vorbis.disc_total": "2",
        "vorbis.track_number": "3",
        "vorbis.track_total": "12",
    }


def test_partial_creation_dates_record_precision_without_false_invalid_warning() -> (
    None
):
    assert metadata._date_precision("D:2024") == "year"
    assert metadata._date_precision("D:202406") == "month"
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["exif"] = {"exif.date_time_original": "2024-06"}
    structure = {"streams": [], "chapters": [], "artwork": [], "pages": {}}

    normalized, _, warnings = metadata._normalize(
        {"kind": "image", "format": "jpeg", "mime": "image/jpeg"},
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="b" * 64,
        size_bytes=10,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["created_at"] is None
    assert warnings == []
    assert structure["dates"] == {
        "creation": {
            "raw": "2024-06",
            "precision": "month",
            "source_paths": ["sources.exif.exif.date_time_original"],
        }
    }


def test_overlong_source_candidate_paths_are_skipped_not_truncated() -> None:
    long_key = "a" * 256
    deep: dict[str, Any] = {"create_date": "1999-01"}
    for _ in range(4):
        deep = {long_key: deep}
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["xmp"] = {**deep, "z": {"create_date": "2024-06"}}
    structure = {"streams": [], "chapters": [], "artwork": [], "pages": {}}

    normalized, _, warnings = metadata._normalize(
        {"kind": "image", "format": "jpeg", "mime": "image/jpeg"},
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="c" * 64,
        size_bytes=10,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["created_at"] is None
    assert warnings == []
    assert structure["dates"]["creation"] == {
        "raw": "2024-06",
        "precision": "month",
        "source_paths": ["sources.xmp.z.create_date"],
    }
    Draft202012Validator(
        {
            "$ref": "#/$defs/dates",
            "$defs": metadata.MEDIA_METADATA_ENVELOPE_SCHEMA["$defs"],
        }
    ).validate(structure["dates"])


def test_exclusion_policy_omits_byte_values_and_keeps_all_scalars() -> None:
    payload = {
        "Attachments": {"FileName": "brief.pdf", "Data": b"secret bytes"},
        "Certificate": {"Type": "X.509", "Data": bytearray(b"certificate")},
        "PDF": {
            "HasJavaScript": True,
            "JavaScriptCode": "app.alert('secret')",
            "JS": "ordinary custom info scalar",
            "SignatureValue": "signature identifier",
            "AcroForm": {"FieldValue": "secret form value"},
        },
        "Thumbnail": {"Width": 320, "Height": 200, "Data": memoryview(b"image")},
        "XFAData": b"xfa payload",
    }

    omitted = metadata._empty_omitted()
    result = metadata._bounded_tag_value(
        payload,
        omitted,
        path="sources.pdf_info",
    )

    assert result == {
        "Attachments": {"FileName": "brief.pdf"},
        "Certificate": {"Type": "X.509"},
        "PDF": {
            "AcroForm": {"FieldValue": "secret form value"},
            "HasJavaScript": True,
            "JS": "ordinary custom info scalar",
            "JavaScriptCode": "app.alert('secret')",
            "SignatureValue": "signature identifier",
        },
        "Thumbnail": {"Height": 200, "Width": 320},
    }
    assert omitted["binary_values"] == 4
    assert metadata._tag_disposition(
        "source_file",
        adapter="exiftool",
        adapter_group="File",
    ) == ("always_transport_omit")
    assert (
        metadata._tag_disposition(
            "file_modify_date",
            adapter="exiftool",
            adapter_group="File",
        )
        == "safe"
    )
    for group, tag in (
        ("System", "file_access_date"),
        ("System", "file_inode_change_date"),
        ("System", "file_permissions"),
    ):
        assert (
            metadata._tag_disposition(
                tag,
                adapter="exiftool",
                adapter_group=group,
            )
            == "always_transport_omit"
        )
    assert (
        metadata._tag_disposition(
            "file_name",
            adapter="exiftool",
            adapter_group="PDF",
        )
        == "safe"
    )
    assert (
        metadata._tag_disposition(
            "thumbnail_width",
            adapter="exiftool",
            adapter_group="PDF",
        )
        == "safe"
    )


def test_nested_group_names_are_ordinary_metadata_outside_adapter_root_groups() -> None:
    payload = {
        "File": {
            "FilePath": "/Users/operator/private/original.png",
            "URL": "https://example.test/file",
        },
        "System": {"Directory": "/private/tmp/ordinary-system-value"},
        "PDF": {
            "Value": "ordinary vendor value",
            "URIAction": "https://example.test/pdf",
        },
        "RawData": "ordinary vendor text",
        "JavaScript": "descriptive vendor scalar",
        "SignatureValue": "vendor signature identifier",
        "Attachments": {"Data": "nested vendor scalar"},
        "Certificate": {"Data": "nested certificate scalar"},
        "Thumbnail": {"Data": "nested thumbnail scalar"},
    }
    omitted = metadata._empty_omitted()
    result = metadata._bounded_tag_value(
        payload,
        omitted,
        path="sources.xmp.description",
    )

    assert result == payload
    assert omitted["binary_values"] == 0
    assert omitted["source_values"] == 0


def test_path_and_url_shaped_scalar_content_is_metadata() -> None:
    payload = {
        "OriginalPath": "archive/original.png",
        "FilePath": "/Users/operator/private/original.png",
        "Description": "https://example.test/item?access_token=secret",
    }
    omitted = metadata._empty_omitted()
    result = metadata._bounded_tag_value(
        payload,
        omitted,
        path="sources.xmp",
    )

    assert result == payload
    assert omitted["source_values"] == 0
    assert "/Users/operator" in json.dumps(result)
    assert "access_token" in json.dumps(result)


@pytest.mark.parametrize(
    ("kind", "recognition", "required_adapter"),
    [
        (
            "audio",
            metadata._recognition(
                "audio", "mp3", "audio/mpeg", offset=0, confidence=100, source="test"
            ),
            "ffprobe",
        ),
        (
            "pdf",
            metadata._recognition(
                "pdf",
                "pdf",
                "application/pdf",
                offset=0,
                confidence=100,
                source="test",
            ),
            "pypdf",
        ),
    ],
)
def test_required_adapter_skipped_at_total_deadline_is_reported_as_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    recognition: dict[str, Any],
    required_adapter: str,
) -> None:
    media = tmp_path / "fixture.bin"
    media.write_bytes(b"fixture")
    monkeypatch.setattr(metadata, "MAX_TOTAL_PROBE_SECONDS", 0.0)
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            recognitions=[recognition],
        ),
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    monkeypatch.setattr(
        metadata,
        "_pypdf_version",
        lambda: "6.1.0",
    )
    monkeypatch.setattr(
        metadata,
        "_ffprobe_adapter",
        lambda *_args, **_kwargs: pytest.fail("ffprobe must not start after deadline"),
    )
    monkeypatch.setattr(
        metadata,
        "_pypdf_adapter",
        lambda *_args, **_kwargs: pytest.fail("pypdf must not start after deadline"),
    )

    envelope = metadata.extract_media_metadata(
        media,
        digest="d" * 64,
        size_bytes=7,
        filename=None,
        claimed_mime=None,
    )

    assert envelope["normalized"]["kind"] == kind
    assert envelope["extraction"]["adapters"][required_adapter]["outcome"] == (
        "timeout"
    )
    assert any(
        item["code"] == "timeout" and item.get("source") == required_adapter
        for item in envelope["warnings"]
    )
    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(envelope)

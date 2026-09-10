from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

import frisket.ops.media_metadata as metadata
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MEDIA_ACQUISITION_NAMESPACE,
    MEDIA_METADATA_CACHE_NAMESPACE,
    MediaBlobStore,
    owned_media_metadata_document,
)


EXPECTED_ROLES = (
    "kind",
    "format",
    "mime",
    "filename",
    "size_bytes",
    "blob_hash",
    "probe_status",
    "title",
    "creator",
    "created_at",
    "width_pixels",
    "height_pixels",
    "capture_device",
    "duration_seconds",
    "bitrate_bps",
    "audio_codec",
    "sample_rate_hz",
    "channel_count",
    "album",
    "video_codec",
    "frame_rate_fps",
    "page_count",
    "pdf_encrypted",
)


def _fake_tool_version(version: str) -> str:
    return version


def _rehash_cache_entry(entry: dict[str, Any]) -> None:
    encoded = metadata.canonical_json_bytes(entry["content_facts"])
    entry["content_facts_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_exiftool_resolution_requires_managed_or_explicit_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("FRISKET_EXIFTOOL_PATH", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: self == executable)

    assert metadata._resolve_exiftool_path() is None

    monkeypatch.setenv("FRISKET_EXIFTOOL_PATH", str(executable))
    assert metadata._resolve_exiftool_path() == executable.resolve()

    executable.chmod(0o644)
    assert metadata._resolve_exiftool_path() is None


def test_tool_version_cache_invalidates_when_executable_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_bytes(b"first executable")
    calls = 0
    metadata._VERSION_CACHE.clear()

    def fake_run(_argv, **_kwargs):
        nonlocal calls
        calls += 1
        version = (
            b"13.59\n" if executable.read_bytes().startswith(b"first") else b"13.60\n"
        )
        return metadata._CommandResult(0, version, b"")

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)
    assert metadata._tool_version(executable, "exiftool") == "13.59"
    assert metadata._tool_version(executable, "exiftool") == "13.59"
    assert calls == 1

    executable.write_bytes(b"different second executable")
    assert metadata._tool_version(executable, "exiftool") == "13.60"
    assert calls == 2


def test_tool_version_does_not_cache_a_transient_negative_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_bytes(b"stable executable")
    calls = 0
    metadata._VERSION_CACHE.clear()
    metadata._FFPROBE_FD_INPUT_CACHE.clear()

    def fake_run(argv, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return metadata._CommandResult(1, b"", b"temporary failure")
        if "-protocols" in argv:
            return metadata._CommandResult(
                0,
                b"Supported file protocols:\nInput:\n  fd\n  pipe\nOutput:\n  fd\n",
                b"",
            )
        return metadata._CommandResult(0, b"ffprobe version 8.1.1\n", b"")

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)

    assert metadata._tool_version(executable, "ffprobe") is None
    assert metadata._tool_version(executable, "ffprobe") == "8.1.1"
    assert metadata._tool_version(executable, "ffprobe") == "8.1.1"
    assert calls == 3


def test_ffprobe_fd_capability_probe_transient_failure_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_bytes(b"stable executable")
    calls: list[list[str]] = []
    metadata._VERSION_CACHE.clear()
    metadata._FFPROBE_FD_INPUT_CACHE.clear()

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if "-protocols" not in argv:
            return metadata._CommandResult(0, b"ffprobe version 8.1.1\n", b"")
        if sum("-protocols" in call for call in calls) == 1:
            return metadata._CommandResult(1, b"", b"temporary failure")
        return metadata._CommandResult(
            0,
            b"Supported file protocols:\nInput:\n  fd\n  pipe\nOutput:\n  fd\n",
            b"",
        )

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)

    assert metadata._tool_version(executable, "ffprobe") is None
    assert metadata._cached_ffprobe_fd_input_support(executable) is None
    assert metadata._tool_version(executable, "ffprobe") == "8.1.1"
    assert metadata._tool_version(executable, "ffprobe") == "8.1.1"
    assert ["-protocols" in call for call in calls] == [False, True, False, True]


def _png_header(width: int = 13, height: int = 7) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("e\u0301", "é"),
        ("first\r\nsecond\rthird", "first\nsecond\nthird"),
        ("left\x00right", "leftright"),
    ],
)
def test_normalization_byte_changes_do_not_count_as_value_truncation(
    raw: str,
    normalized: str,
) -> None:
    omitted = metadata._empty_omitted()

    assert (
        metadata._bounded_json_value(raw, omitted, path="sources.xmp.value")
        == normalized
    )
    assert omitted["truncated_values"] == 0
    assert "truncated_value" not in omitted["classes"]


def test_source_value_over_limit_still_counts_as_truncation() -> None:
    omitted = metadata._empty_omitted()

    bounded = metadata._bounded_json_value(
        "x" * (metadata.MAX_SOURCE_STRING_BYTES + 1),
        omitted,
        path="sources.xmp.value",
    )

    assert len(bounded.encode("utf-8")) == metadata.MAX_SOURCE_STRING_BYTES
    assert omitted["truncated_values"] == 1
    assert "truncated_value" in omitted["classes"]


def _extract_png(
    tmp_path: Path,
    monkeypatch,
    *,
    filename: str | None = r"C:\incoming\wrong.jpg",
    claimed_mime: str | None = "video/mp4; charset=binary",
) -> tuple[Path, str, dict]:
    payload = _png_header(17, 11)
    path = tmp_path / "materialized-blob"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            recognitions=[
                metadata._recognition(
                    "image", "png", "image/png", offset=0, source="exiftool"
                )
            ],
        ),
    )
    envelope = metadata.extract_media_metadata(
        path,
        digest=digest,
        size_bytes=len(payload),
        filename=filename,
        claimed_mime=claimed_mime,
    )
    return path, digest, envelope


def test_projection_registry_and_envelope_are_closed_and_cold_warm_equal(
    tmp_path: Path, monkeypatch
) -> None:
    path, digest, cold = _extract_png(tmp_path, monkeypatch)

    assert metadata.MEDIA_METADATA_ROLES == EXPECTED_ROLES
    assert len(metadata.MEDIA_METADATA_PROJECTION) == 23
    normalized = cold["normalized"]
    assert tuple(normalized) == EXPECTED_ROLES
    assert normalized == {
        **{role: None for role in EXPECTED_ROLES},
        "kind": "image",
        "format": "png",
        "mime": "image/png",
        "filename": "wrong.jpg",
        "size_bytes": len(path.read_bytes()),
        "blob_hash": f"sha256:{digest}",
        "probe_status": "ok",
        "width_pixels": 17,
        "height_pixels": 11,
    }
    assert {item["reason"] for item in cold["conflicts"]} == {
        "reference_claim_conflicts_with_content",
        "reference_extension_conflicts_with_content",
    }
    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(cold)
    assert len(metadata.canonical_json_bytes(cold)) <= metadata.MAX_DETAILS_BYTES
    for entry in cold["lineage"]["fields"].values():
        selected = entry.get("selected")
        assert selected is None or metadata._metadata_path_resolves(cold, selected)
        assert all(
            metadata._metadata_path_resolves(cold, candidate)
            for candidate in entry.get("candidates") or []
        )

    projected = metadata.projection_values(cold)
    assert tuple(projected) == (*EXPECTED_ROLES, "details")
    assert projected["details"] == cold
    assert projected["details"] is not cold

    entry = metadata.cache_entry_from_envelope(cold)
    template = entry["content_facts"]["envelope"]
    assert template["normalized"]["filename"] is None
    assert "filename" not in template["sources"]["blob"]
    assert "claimed_mime" not in template["sources"]["blob"]
    assert not any(
        item["reason"].startswith("reference_") for item in template["conflicts"]
    )
    assert metadata.media_metadata_cache_compatible(entry)
    different_reported_tools = copy.deepcopy(entry)
    different_reported_tools["content_facts"]["envelope"]["extraction"]["tools"] = {
        "exiftool": "99.0",
        "ffprobe": "100.0",
        "pypdf": "101.0",
    }
    _rehash_cache_entry(different_reported_tools)
    assert metadata.media_metadata_cache_compatible(different_reported_tools)
    stale_extractor = copy.deepcopy(entry)
    stale_extractor["extractor_version"] += 1
    assert metadata.media_metadata_cache_compatible(stale_extractor) is False
    stale_adapters = copy.deepcopy(entry)
    stale_adapters["content_facts"]["envelope"]["extraction"]["adapter_fingerprint"] = (
        "sha256:" + ("0" * 64)
    )
    _rehash_cache_entry(stale_adapters)
    assert metadata.media_metadata_cache_compatible(stale_adapters) is False
    dishonest = copy.deepcopy(entry)
    dishonest["content_facts"]["envelope"]["extraction"]["adapters"]["exiftool"][
        "outcome"
    ] = "timeout"
    _rehash_cache_entry(dishonest)
    assert metadata.media_metadata_cache_compatible(dishonest) is False
    forged_degraded = copy.deepcopy(entry)
    degraded_envelope = forged_degraded["content_facts"]["envelope"]
    degraded_envelope["normalized"]["probe_status"] = "partial"
    degraded_envelope["extraction"]["completeness"] = "degraded_missing_dependency"
    degraded_adapter = degraded_envelope["extraction"]["adapters"]["exiftool"]
    degraded_adapter.update(
        available=False,
        outcome="missing_dependency",
        version=None,
    )
    forged_degraded["completeness"] = "complete"
    forged_degraded["retryable"] = False
    _rehash_cache_entry(forged_degraded)
    assert metadata.media_metadata_cache_compatible(forged_degraded) is False
    malformed_generation = copy.deepcopy(entry)
    malformed_generation["generation"] = "not-an-integer"
    assert metadata.media_metadata_cache_compatible(malformed_generation) is False
    stale_cache_schema = copy.deepcopy(entry)
    stale_cache_schema["cache_schema_version"] -= 1
    assert metadata.media_metadata_cache_compatible(stale_cache_schema) is False
    stale_limits = copy.deepcopy(entry)
    stale_limits["content_facts"]["envelope"]["limits_version"] += 1
    _rehash_cache_entry(stale_limits)
    assert metadata.media_metadata_cache_compatible(stale_limits) is False

    other_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    with pytest.raises(ValueError, match="does not match the target blob"):
        metadata.envelope_from_cache(
            entry, digest=other_digest, size_bytes=len(path.read_bytes())
        )
    with pytest.raises(ValueError, match="does not match the target blob"):
        metadata.envelope_from_cache(
            entry, digest=digest, size_bytes=len(path.read_bytes()) + 1
        )

    warm_template = metadata.envelope_from_cache(
        entry, digest=digest, size_bytes=len(path.read_bytes())
    )
    warm = metadata.overlay_reference_evidence(
        warm_template,
        filename=r"C:\incoming\wrong.jpg",
        claimed_mime="video/mp4; charset=binary",
    )
    assert metadata.canonical_envelope_json(warm) == (
        metadata.canonical_envelope_json(cold)
    )

    repeated = metadata.extract_media_metadata(
        path,
        digest=digest,
        size_bytes=len(path.read_bytes()),
        filename=r"C:\incoming\wrong.jpg",
        claimed_mime="video/mp4; charset=binary",
    )
    assert metadata.canonical_envelope_json(repeated) == (
        metadata.canonical_envelope_json(cold)
    )


def test_cache_requires_exact_integer_limits_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _digest, envelope = _extract_png(tmp_path, monkeypatch)
    entry = metadata.cache_entry_from_envelope(envelope)
    assert metadata.media_metadata_cache_compatible(entry)

    float_limits = copy.deepcopy(entry)
    template = float_limits["content_facts"]["envelope"]
    template["limits_version"] = float(metadata.MEDIA_METADATA_LIMITS_VERSION)
    _rehash_cache_entry(float_limits)
    assert metadata._MEDIA_METADATA_ENVELOPE_VALIDATOR.is_valid(template)
    assert metadata.media_metadata_cache_compatible(float_limits) is False


def test_prefix_read_failure_is_transient_and_not_cacheable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unreadable-materialization"
    path.write_bytes(b"content")
    digest = hashlib.sha256(b"content").hexdigest()
    monkeypatch.setattr(
        metadata,
        "_read_prefix",
        lambda _path: (
            b"",
            [
                metadata._warning(
                    "internal_error",
                    source="content_recognizer",
                    message="blob could not be read",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool",
            outcome="not_applicable",
            available=True,
            version="13.59",
        ),
    )

    envelope = metadata.extract_media_metadata(
        path,
        digest=digest,
        size_bytes=len(b"content"),
        filename=None,
        claimed_mime=None,
    )

    assert envelope["extraction"]["adapters"]["content_recognizer"]["outcome"] == (
        "internal_error"
    )
    assert envelope["extraction"]["completeness"] == "retryable_failure"
    assert envelope["extraction"]["retryable"] is True
    assert (
        metadata.media_metadata_cache_compatible(
            metadata.cache_entry_from_envelope(envelope)
        )
        is False
    )


def test_nested_envelope_records_are_closed_and_source_depth_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, valid = _extract_png(tmp_path, monkeypatch)
    validator = Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA)

    invalid_values: list[dict[str, Any]] = []
    bad_warning = copy.deepcopy(valid)
    bad_warning["warnings"] = [42]
    invalid_values.append(bad_warning)

    bad_stream = copy.deepcopy(valid)
    bad_stream["structure"]["streams"] = [{"host_path": "/private/input"}]
    invalid_values.append(bad_stream)

    bad_extraction = copy.deepcopy(valid)
    bad_extraction["extraction"]["host_path"] = "/private/input"
    invalid_values.append(bad_extraction)

    bad_lineage = copy.deepcopy(valid)
    bad_lineage["lineage"]["fields"]["title"] = {
        "selected": "sources.xmp.title",
        "candidates": [],
        "transforms": [],
        "unexpected": True,
    }
    invalid_values.append(bad_lineage)

    bad_conflict = copy.deepcopy(valid)
    bad_conflict["conflicts"] = [
        {
            "field": "format",
            "candidate_paths": [],
            "reason": "fixture",
            "unexpected": True,
        }
    ]
    invalid_values.append(bad_conflict)

    bad_depth = copy.deepcopy(valid)
    nested: dict[str, Any] = {"leaf": "value"}
    for depth in range(10):
        nested = {f"level_{depth}": nested}
    bad_depth["sources"]["xmp"] = {"deep": nested}
    invalid_values.append(bad_depth)

    bad_string = copy.deepcopy(valid)
    bad_string["sources"]["xmp"] = {
        "oversized": "x" * (metadata.MAX_SOURCE_STRING_BYTES + 1)
    }
    invalid_values.append(bad_string)

    bad_array = copy.deepcopy(valid)
    bad_array["sources"]["xmp"] = {
        "oversized": list(range(metadata.MAX_REPEATED_VALUES + 1))
    }
    invalid_values.append(bad_array)

    for invalid in invalid_values:
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_envelope_pruning_preserves_promoted_source_or_clears_it_atomically() -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }
    envelope["normalized"]["title"] = "Selected title"
    envelope["sources"]["xmp"]["xmp"] = {"title": "Selected title"}
    envelope["lineage"]["fields"]["title"] = metadata._lineage_entry(
        "sources.xmp.xmp.title"
    )
    for index in range(64):
        envelope["sources"]["xmp"][f"zzz_{index:03d}"] = (
            "x" * metadata.MAX_SOURCE_STRING_BYTES
        )

    bounded = metadata._finalize_bounds(envelope)

    assert len(metadata.canonical_json_bytes(bounded)) <= (metadata.MAX_DETAILS_BYTES)
    if bounded["normalized"]["title"] is not None:
        selected = bounded["lineage"]["fields"]["title"]["selected"]
        assert selected == "sources.xmp.xmp.title"
        assert bounded["sources"]["xmp"]["xmp"]["title"] == "Selected title"
        assert metadata._metadata_path_resolves(bounded, selected)
    else:
        assert "title" not in bounded["lineage"]["fields"]


def test_envelope_pruning_counts_nested_source_scalar_leaves() -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }
    envelope["sources"]["xmp"]["xmp.nested"] = {
        "group": [
            {
                "left": "x" * metadata.MAX_SOURCE_STRING_BYTES,
                "right": "y" * metadata.MAX_SOURCE_STRING_BYTES,
            }
            for _ in range(4)
        ]
    }

    bounded = metadata._finalize_bounds(envelope)

    assert bounded["sources"]["xmp"] == {}
    assert bounded["omitted"]["source_values"] == 8
    assert set(bounded["omitted"]["paths"]) == {
        f"sources.xmp.xmp.nested.group[{index}].{side}"
        for index in range(4)
        for side in ("left", "right")
    }
    assert len(metadata.canonical_json_bytes(bounded)) <= (metadata.MAX_DETAILS_BYTES)


def test_envelope_pruning_clears_structured_music_and_date_provenance() -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {
            "streams": [],
            "chapters": [],
            "artwork": [],
            "pages": {},
            "music": {
                "track": {
                    "raw": "03",
                    "number": 3,
                    "total": None,
                    "source_path": "sources.vorbis.zzzz.track_number",
                    "total_raw": None,
                    "total_source_path": None,
                },
                "disc": None,
            },
            "dates": {
                "creation": {
                    "raw": "2024-07",
                    "precision": "month",
                    "source_paths": ["sources.vorbis.zzzy.create_date"],
                }
            },
        },
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }
    envelope["sources"]["vorbis"] = {
        "zzzz.track_number": "03",
        "zzzy.create_date": "2024-07",
        **{
            f"yyyy.large_{index:02d}": "x" * metadata.MAX_SOURCE_STRING_BYTES
            for index in range(12)
        },
    }

    bounded = metadata._finalize_bounds(envelope)

    assert bounded["structure"]["music"]["track"] is None
    assert bounded["structure"]["dates"]["creation"] is None
    assert bounded["omitted"]["lineage_items"] == 2
    assert bounded["omitted"]["structure_items"] == 2
    assert {
        "sources.vorbis.zzzz.track_number",
        "sources.vorbis.zzzy.create_date",
        "structure.music.track",
        "structure.dates.creation",
    } <= set(bounded["omitted"]["paths"])
    assert len(metadata.canonical_json_bytes(bounded)) <= (metadata.MAX_DETAILS_BYTES)


def test_minimal_envelope_fallback_counts_removed_selected_lineage() -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }
    selected_roles = (
        "mime",
        "filename",
        "title",
        "creator",
        "created_at",
        "capture_device",
        "audio_codec",
        "album",
        "video_codec",
    )
    for index, role in enumerate(selected_roles):
        source_key = f"xmp.selected_{index:02d}"
        source_path = f"sources.xmp.{source_key}"
        value = chr(ord("a") + index) * metadata.MAX_SOURCE_STRING_BYTES
        envelope["sources"]["xmp"][source_key] = value
        envelope["normalized"][role] = value
        envelope["lineage"]["fields"][role] = metadata._lineage_entry(source_path)

    bounded = metadata._finalize_bounds(envelope)

    assert bounded["sources"]["xmp"] == {}
    assert all(bounded["normalized"][role] is None for role in selected_roles)
    assert not set(selected_roles) & set(bounded["lineage"]["fields"])
    assert bounded["omitted"]["source_values"] == len(selected_roles)
    assert bounded["omitted"]["lineage_items"] == len(selected_roles)
    assert "lineage" in bounded["omitted"]["classes"]
    assert len(metadata.canonical_json_bytes(bounded)) <= (metadata.MAX_DETAILS_BYTES)


def test_minimal_envelope_hides_hostile_omission_paths_but_caches_exact_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest, envelope = _extract_png(
        tmp_path, monkeypatch, filename=None, claimed_mime=None
    )
    hostile_paths = {f"{index:02d}" + '"' * 510 for index in range(64)}
    for hostile_path in hostile_paths:
        metadata._record_omission(
            envelope["omitted"],
            "source_values",
            path=hostile_path,
        )

    bounded = metadata._finalize_bounds(envelope)
    exact_paths = metadata._omitted_labels(bounded["omitted"], "paths")

    assert len(metadata.canonical_json_bytes(bounded)) <= 48 * 1024
    assert bounded["omitted"]["paths"] == []
    assert bounded["omitted"]["paths_overflow"] == len(exact_paths)
    assert hostile_paths <= exact_paths
    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(bounded)

    entry = metadata.cache_entry_from_envelope(bounded)
    persisted = json.loads(metadata.canonical_json_bytes(entry))
    restored = metadata.envelope_from_cache(
        persisted,
        digest=digest,
        size_bytes=len(path.read_bytes()),
    )
    assert metadata._omitted_labels(restored["omitted"], "paths") == exact_paths


def test_global_source_value_limit_is_shared_across_adapters() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["exif"] = {
        f"exif.value_{index:04d}": index
        for index in range(metadata.MAX_SOURCE_VALUES - 1)
    }
    sources["ffprobe"] = {
        "tags": {"format_a": "a", "format_b": "b"},
        "extras": {"format_c": "c", "format_d": "d"},
    }
    structure = {
        "streams": [
            {
                "tags": {"stream_a": "a", "stream_b": "b"},
                "extras": {"stream_c": "c", "stream_d": "d"},
            }
        ],
        "chapters": [
            {
                "tags": {},
                "extras": {"chapter_a": "a", "chapter_b": "b"},
            }
        ],
        "artwork": [],
        "pages": {},
    }
    omitted = metadata._empty_omitted()

    removed = metadata._enforce_global_source_value_limit(sources, structure, omitted)

    retained = (
        len(sources["exif"])
        + len(sources["ffprobe"]["tags"])
        + len(sources["ffprobe"]["extras"])
        + len(structure["streams"][0]["tags"])
        + len(structure["streams"][0]["extras"])
        + len(structure["chapters"][0]["extras"])
    )
    assert retained == metadata.MAX_SOURCE_VALUES
    assert removed == 9
    assert omitted["source_values"] == 9
    assert "source_value_limit" in omitted["classes"]


def test_global_source_value_limit_includes_dynamic_blob_vendor_facts() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["blob"] = {
        f"vendor.tag_{index:02d}": list(range(metadata.MAX_REPEATED_VALUES))
        for index in range(9)
    }
    structure = {"streams": [], "chapters": [], "artwork": [], "pages": {}}
    omitted = metadata._empty_omitted()

    removed = metadata._enforce_global_source_value_limit(sources, structure, omitted)
    retained = sum(
        1
        for key, value in sources["blob"].items()
        for _path in metadata._source_value_leaf_paths(
            value, path=f"sources.blob.{key}"
        )
    )

    assert retained == metadata.MAX_SOURCE_VALUES
    assert removed == metadata.MAX_REPEATED_VALUES
    assert omitted["source_values"] == metadata.MAX_REPEATED_VALUES
    assert "source_value_limit" in omitted["classes"]


def test_global_source_limit_reserves_header_and_identity_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _png_header(41, 23)
    path = tmp_path / "vendor-heavy.png"
    path.write_bytes(payload)
    vendor = {
        f"aaa.vendor_{index:04d}": index for index in range(metadata.MAX_SOURCE_VALUES)
    }
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            sources={"blob": vendor},
        ),
    )

    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=None,
        claimed_mime=None,
    )

    assert envelope["normalized"]["width_pixels"] == 41
    assert envelope["normalized"]["height_pixels"] == 23
    blob = envelope["sources"]["blob"]
    assert blob["header_width_pixels"] == 41
    assert blob["header_height_pixels"] == 23
    assert (
        sum(
            1
            for namespace, values in envelope["sources"].items()
            for key, value in values.items()
            if not (namespace == "blob" and key in {"filename", "claimed_mime"})
            for _ in metadata._source_value_leaf_paths(
                value, path=f"sources.{namespace}.{key}"
            )
        )
        == metadata.MAX_SOURCE_VALUES
    )
    assert envelope["omitted"]["source_values"] == 7
    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(envelope)


def test_global_source_value_limit_counts_nested_leaves_and_warns_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _png_header(11, 7)
    path = tmp_path / "nested-source-budget.png"
    path.write_bytes(payload)
    forward = {
        f"bucket_{index:02d}": list(range(metadata.MAX_REPEATED_VALUES))
        for index in range(9)
    }
    reverse = dict(reversed(list(forward.items())))
    fixture_order = [forward, reverse]

    def nested_exiftool(_path, **_kwargs):
        return metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            sources={
                "xmp": {
                    "xmp.description": copy.deepcopy(fixture_order.pop(0)),
                }
            },
        )

    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_exiftool_adapter", nested_exiftool)

    envelopes = [
        metadata.extract_media_metadata(
            path,
            digest=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            filename=path.name,
            claimed_mime="image/png",
        )
        for _ in range(2)
    ]

    assert metadata.canonical_envelope_json(envelopes[0]) == (
        metadata.canonical_envelope_json(envelopes[1])
    )
    envelope = envelopes[0]
    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(envelope)
    retained = envelope["sources"]["xmp"]["xmp.description"]
    assert list(retained) == [f"bucket_{index:02d}" for index in range(8)]
    charged_blob_values = sum(
        key not in {"filename", "claimed_mime"} for key in envelope["sources"]["blob"]
    )
    retained_xmp_values = sum(len(values) for values in retained.values())
    assert retained_xmp_values + charged_blob_values == metadata.MAX_SOURCE_VALUES
    expected_removed = metadata.MAX_REPEATED_VALUES + charged_blob_values
    assert envelope["omitted"]["source_values"] == expected_removed
    assert len(envelope["omitted"]["paths"]) == 64
    assert envelope["omitted"]["paths_overflow"] == expected_removed - 64
    assert "source_value_limit" in envelope["omitted"]["classes"]
    assert envelope["normalized"]["probe_status"] == "partial"
    assert [
        warning
        for warning in envelope["warnings"]
        if warning.get("subtype") == "source_value_limit"
    ] == [
        {
            "code": "resource_limit",
            "subtype": "source_value_limit",
            "source": "normalizer",
            "path": "sources",
            "message": (
                f"Source metadata exceeded the global "
                f"{metadata.MAX_SOURCE_VALUES}-value limit"
            ),
        }
    ]


def test_resource_warning_uses_a_slot_and_counts_the_displaced_warning() -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [
            metadata._warning(f"warning_{index:03d}")
            for index in range(metadata.MAX_WARNINGS)
        ],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }
    envelope["sources"]["xmp"] = {
        f"xmp.large_{index:02d}": "x" * metadata.MAX_SOURCE_STRING_BYTES
        for index in range(12)
    }

    bounded = metadata._finalize_bounds(envelope)

    assert len(bounded["warnings"]) == metadata.MAX_WARNINGS
    assert any(item["code"] == "resource_limit" for item in bounded["warnings"])
    assert bounded["omitted"]["warnings_overflow"] == 1
    assert len(metadata.canonical_json_bytes(bounded)) <= metadata.MAX_DETAILS_BYTES


def test_conflict_overflow_count_is_exact_after_deduplication() -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [
            {
                "field": "format",
                "candidate_paths": [f"sources.blob.candidate_{index:03d}"],
                "reason": f"reason_{index:03d}",
            }
            for index in range(metadata.MAX_CONFLICTS + 2)
        ],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }

    bounded = metadata._finalize_bounds(envelope)

    assert len(bounded["conflicts"]) == metadata.MAX_CONFLICTS
    assert bounded["omitted"]["conflicts_overflow"] == 2


def test_envelope_pruning_serializes_the_full_envelope_by_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {
        "schema_version": metadata.MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": metadata.MEDIA_METADATA_LIMITS_VERSION,
        "normalized": metadata._empty_normalized(),
        "sources": {name: {} for name in metadata.SOURCE_NAMESPACES},
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": {"fields": {}},
        "conflicts": [],
        "warnings": [],
        "omitted": metadata._empty_omitted(),
        "extraction": {"completeness": "complete", "retryable": False},
    }
    envelope["sources"]["xmp"] = {
        f"xmp.large_{index:03d}": "x" * metadata.MAX_SOURCE_STRING_BYTES
        for index in range(128)
    }
    original = metadata.canonical_json_bytes
    full_envelope_serializations = 0

    def counted(value: Any) -> bytes:
        nonlocal full_envelope_serializations
        if value is envelope:
            full_envelope_serializations += 1
        return original(value)

    monkeypatch.setattr(metadata, "canonical_json_bytes", counted)

    bounded = metadata._finalize_bounds(envelope)

    assert full_envelope_serializations <= 4
    assert len(original(bounded)) <= metadata.MAX_DETAILS_BYTES


def test_reference_evidence_is_per_cell_and_does_not_mutate_template(
    tmp_path: Path, monkeypatch
) -> None:
    _, _, first = _extract_png(
        tmp_path, monkeypatch, filename="first.png", claimed_mime="image/png"
    )
    template = metadata.overlay_reference_evidence(
        first, filename=None, claimed_mime=None
    )
    original = copy.deepcopy(template)

    second = metadata.overlay_reference_evidence(
        template, filename="../renamed.bin", claimed_mime="application/octet-stream"
    )

    assert template == original
    assert second["normalized"]["filename"] == "renamed.bin"
    assert second["sources"]["blob"]["claimed_mime"] == "application/octet-stream"
    assert first["normalized"]["filename"] == "first.png"
    assert second != first


def test_exiftool_argv_suppresses_binary_and_retains_file_content_facts(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("test executable placeholder")
    blob = tmp_path / "-hostile-materialized-name"
    blob.write_bytes(_png_header())
    captured: list[list[str]] = []
    body = [
        {
            "File:FileType": "PNG",
            "File:FileTypeExtension": "png",
            "File:MIMEType": "image/png",
            "File:ImageWidth": "640",
            "File:ImageHeight": "480",
            "File:ExifByteOrder": "II",
            "File:BitsPerSample": ["8", "8", "8"],
            "System:FileSize": "1234",
            "EXIF:Title": "  Safe title  ",
            "EXIF:GPSLatitude": "40.7",
            "EXIF:SerialNumber": "001234",
            "MakerNotes:BodySerialNumber": "000042",
            "MakerNotes:LensSerialNumber2": "000007",
            "XMP:UnknownCustom": "custom value",
            "XMP:ProductCode": "00123",
            "XMP:Description": "(Binary data 123 bytes, use -b option to extract)",
            "SourceFile": "/private/tmp/host-secret/materialized",
            "System:Directory": "/private/tmp/host-secret",
            "System:FileName": "materialized-host-secret",
            "System:FilePath": "/private/tmp/host-secret/materialized-host-secret",
            "System:FileModifyDate": "2026:07:18 10:11:12-04:00",
            "System:FileAccessDate": "2026:07:18 10:11:13-04:00",
            "System:FileInodeChangeDate": "2026:07:18 10:11:14-04:00",
            "System:FilePermissions": "-rw-------",
            "XMP:OriginalPath": "/Users/operator/private/original.png",
        }
    ]

    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("13.59"),
    )

    def fake_run(argv, **_kwargs):
        captured.append(list(argv))
        return metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)
    result = metadata._exiftool_adapter(blob)

    argv = captured[-1]
    assert argv[0] == str(executable)
    assert argv[argv.index("-config") + 1] == ""
    assert "StructFormat=JSONQ" in argv
    assert "--b" in argv
    assert argv[-2:] == ["--", str(blob.resolve())]
    assert result.sources["blob"] == {
        "file.bits_per_sample": [8, 8, 8],
        "file.file_type": "PNG",
        "file.file_type_extension": "png",
        "file.image_height": 480,
        "file.image_width": 640,
        "file.mime_type": "image/png",
    }
    assert result.sources["exif"] == {
        "exif.serial_number": "001234",
        "exif.gps_latitude": 40.7,
        "exif.title": "  Safe title  ",
        "file.exif_byte_order": "II",
        "maker_notes.body_serial_number": "000042",
        "maker_notes.lens_serial_number2": "000007",
    }
    assert result.sources["xmp"] == {
        "xmp.description": "(Binary data 123 bytes, use -b option to extract)",
        "xmp.original_path": "/Users/operator/private/original.png",
        "xmp.product_code": "00123",
        "xmp.unknown_custom": "custom value",
    }
    assert result.omitted["source_values"] == 9
    assert result.omitted["binary_values"] == 0
    serialized = metadata.canonical_json_bytes(result.sources).decode()
    for forbidden in (
        "/private/tmp/host-secret",
        "materialized-host-secret",
    ):
        assert forbidden not in serialized
    assert "/Users/operator/private/original.png" in serialized
    assert "(Binary data 123 bytes, use -b option to extract)" in serialized


def test_exiftool_jsonq_only_restores_explicit_numeric_measurements() -> None:
    value = {
        "ProductCode": "00123",
        "VendorNumber": "42",
        "ImageWidth": "0640",
        "GPSLatitude": "40.7",
        "SerialNumber": "001234",
        "FilePath": "/private/assets/00123",
        "URL": "https://example.test/items/00123",
        "Values": ["00123", "42"],
        "ImageHeight": ["0480", "0720"],
        "Nested": {
            "ProductCode": "00007",
            "PageCount": "12",
            "Entries": [
                {"ImageWidth": "0320", "ProductCode": "00008"},
            ],
        },
    }

    restored = metadata._restore_exiftool_jsonq_value(value, tag="description")

    assert restored == {
        "ProductCode": "00123",
        "VendorNumber": "42",
        "ImageWidth": 640,
        "GPSLatitude": 40.7,
        "SerialNumber": "001234",
        "FilePath": "/private/assets/00123",
        "URL": "https://example.test/items/00123",
        "Values": ["00123", "42"],
        "ImageHeight": [480, 720],
        "Nested": {
            "ProductCode": "00007",
            "PageCount": 12,
            "Entries": [
                {"ImageWidth": 320, "ProductCode": "00008"},
            ],
        },
    }


def test_exiftool_recursively_keeps_scalar_metadata_without_content_heuristics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("placeholder", encoding="utf-8")
    blob = tmp_path / "blob"
    blob.write_bytes(_png_header())
    body = [
        {
            "File:FileType": "PNG",
            "File:MIMEType": "image/png",
            "XMP:Description": {
                "Title": "Nested safe title",
                "GPSLatitude": 40.7,
                "UnknownCustom": "custom value",
                "FilePath": "/private/tmp/materialized-secret",
                "Artist": r"C:\Users\operator\private.jpg",
                "Description": "https://example.test/object?X-Amz-Credential=secret",
                "File": {
                    "FilePath": "/private/tmp/nested-file-key",
                    "URL": "https://example.test/file-group-name",
                },
                "System": {
                    "Directory": "/private/tmp/nested-system-key",
                    "FileAccessDate": "2024:01:02 03:04:05Z",
                    "FilePermissions": "rw-r--r--",
                },
                "PDF": {
                    "Value": "ordinary vendor value",
                    "URIAction": "https://example.test/pdf-group-name",
                },
            },
        }
    ]
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("13.59"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    result = metadata._exiftool_adapter(blob)
    nested = result.sources["xmp"]["xmp.description"]
    assert nested == {
        "Artist": r"C:\Users\operator\private.jpg",
        "Description": "https://example.test/object?X-Amz-Credential=secret",
        "File": {
            "FilePath": "/private/tmp/nested-file-key",
            "URL": "https://example.test/file-group-name",
        },
        "FilePath": "/private/tmp/materialized-secret",
        "GPSLatitude": 40.7,
        "PDF": {
            "URIAction": "https://example.test/pdf-group-name",
            "Value": "ordinary vendor value",
        },
        "System": {
            "Directory": "/private/tmp/nested-system-key",
            "FileAccessDate": "2024:01:02 03:04:05Z",
            "FilePermissions": "rw-r--r--",
        },
        "Title": "Nested safe title",
        "UnknownCustom": "custom value",
    }
    assert result.omitted["source_values"] == 0
    serialized = metadata.canonical_json_bytes(result.sources).decode()
    assert "materialized-secret" in serialized
    assert "operator" in serialized
    assert "X-Amz-Credential" in serialized
    assert "nested-file-key" in serialized
    assert "nested-system-key" in serialized
    assert "ordinary vendor value" in serialized
    assert "pdf-group-name" in serialized


def test_exiftool_multi_family_groups_preserve_scalar_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("placeholder", encoding="utf-8")
    blob = tmp_path / "blob"
    blob.write_bytes(_png_header())
    body = [
        {
            "[Copy1:File]FileType": "PNG",
            "[Copy1:File]MIMEType": "image/png",
            "SourceFile": "/private/tmp/adapter-transport",
            "[Copy1:System]Directory": "/private/tmp/adapter-system-transport",
            "[Copy1:ExifTool]ExifToolVersion": "13.59",
            "[Copy1:ExifTool]Warning": "/private/tmp/adapter-warning/blob is unusual",
            "[Copy1:XMP]Owner": "ordinary owner metadata",
            "[Copy1:XMP]Warning": "ordinary retained warning",
            "[Copy1:XMP]ThumbnailImage": "(Binary data 17 bytes, use -b option to extract)",
            "[Copy1:XMP]JavaScript": "descriptive vendor scalar",
            "[Copy1:XMP]SignatureValue": "vendor signature identifier",
            "[Copy1:XMP]RawData": "ordinary vendor text",
            "[Copy1:XMP]AttachmentData": "attachment identifier",
            "[Copy1:PDF]URIAction": "https://example.test/action?token=keep-me",
            "[Copy1:PDF]FormValue": "ordinary submitted value",
            "[Copy1:PDF]LaunchAction": "/Applications/useful-target",
            "[Copy1:PDF]JavaScriptCode": "app.alert('active content')",
        }
    ]
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("13.59"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    result = metadata._exiftool_adapter(blob)

    assert result.sources["xmp"] == {
        "copy1.xmp.attachment_data": "attachment identifier",
        "copy1.xmp.java_script": "descriptive vendor scalar",
        "copy1.xmp.owner": "ordinary owner metadata",
        "copy1.xmp.raw_data": "ordinary vendor text",
        "copy1.xmp.signature_value": "vendor signature identifier",
        "copy1.xmp.thumbnail_image": (
            "(Binary data 17 bytes, use -b option to extract)"
        ),
        "copy1.xmp.warning": "ordinary retained warning",
    }
    assert result.sources["pdf_info"] == {
        "copy1.pdf.form_value": "ordinary submitted value",
        "copy1.pdf.java_script_code": "app.alert('active content')",
        "copy1.pdf.launch_action": "/Applications/useful-target",
        "copy1.pdf.uri_action": "https://example.test/action?token=keep-me",
    }
    serialized = metadata.canonical_json_bytes(result.sources).decode()
    assert "adapter-transport" not in serialized
    assert "adapter-system-transport" not in serialized
    assert "adapter-warning" not in serialized
    assert "13.59" not in serialized
    assert "active content" in serialized
    assert "Binary data 17 bytes" in serialized
    assert "keep-me" in serialized
    assert "ordinary submitted value" in serialized
    assert "useful-target" in serialized
    assert result.omitted["source_values"] == 4
    assert result.omitted["binary_values"] == 0
    assert result.omitted["classes"] == ["adapter_transport"]


def test_adversarial_exiftool_extraction_is_bounded_deterministic_and_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("placeholder", encoding="utf-8")
    payload = _png_header(19, 12)
    blob = tmp_path / "adversarial.png"
    blob.write_bytes(payload)

    too_deep: Any = "unreachable leaf"
    for _ in range(10):
        too_deep = {"Description": too_deep}
    body = [
        {
            "File:FileType": "PNG",
            "File:MIMEType": "image/png",
            # These distinct raw keys both sanitize to ``xmp.title``.
            "XMP:Ti\x00tle": "deterministic first value",
            "XMP:Title": "must not overwrite",
            "XMP:Description": {
                "Title": "Nested safe title",
                "FilePath": "/private/tmp/materialized-secret",
                "Description": "https://user:password@example.test/private",
                # NFC normalization collapses these two raw keys.
                "Cafe\u0301": "deterministic Unicode value",
                "Café": "must not overwrite Unicode value",
            },
            "XMP:Subject": too_deep,
        }
    ]
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("13.59"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    envelope = metadata.extract_media_metadata(
        blob,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=blob.name,
        claimed_mime="image/png",
    )

    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(envelope)
    xmp = envelope["sources"]["xmp"]
    assert xmp["xmp.title"] == "deterministic first value"
    assert xmp["xmp.description"]["Café"] == "deterministic Unicode value"
    assert xmp["xmp.description"]["FilePath"] == ("/private/tmp/materialized-secret")
    assert xmp["xmp.description"]["Description"] == (
        "https://user:password@example.test/private"
    )
    serialized = metadata.canonical_envelope_json(envelope)
    assert "must not overwrite" not in serialized
    assert "materialized-secret" in serialized
    assert "user:password" in serialized
    assert "excessive_nesting" in envelope["omitted"]["classes"]
    assert "mapping_values" in envelope["omitted"]["classes"]


def test_nested_scalar_text_is_not_inspected_for_binary_placeholders() -> None:
    omitted = metadata._empty_omitted()
    value = {
        "description": {
            "title": [
                "  (  BINARY DATA 4096 bytes, use -b option to extract )\t",
                "An ordinary caption mentioning (Binary data 3 bytes)",
                b"actual binary value",
            ]
        }
    }

    bounded = metadata._bounded_tag_value(
        value,
        omitted,
        path="sources.xmp.xmp.description",
    )

    assert bounded == {
        "description": {
            "title": [
                "  (  BINARY DATA 4096 bytes, use -b option to extract )\t",
                "An ordinary caption mentioning (Binary data 3 bytes)",
            ]
        }
    }
    assert omitted["binary_values"] == 1
    assert "binary" in omitted["classes"]
    assert any(path.endswith(".description.title[2]") for path in omitted["paths"])


def test_runtime_source_depth_guard_exactly_matches_closed_schema() -> None:
    source_value_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": metadata._source_value_schema_defs(),
        "$ref": "#/$defs/source_value_0",
    }
    validator = Draft202012Validator(source_value_schema)

    allowed_arrays: Any = "leaf"
    for _ in range(9):
        allowed_arrays = [allowed_arrays]
    allowed_objects: Any = "leaf"
    for index in range(8):
        allowed_objects = {f"level_{index}": allowed_objects}

    for value in (allowed_arrays, allowed_objects):
        omitted = metadata._empty_omitted()
        bounded = metadata._bounded_tag_value(
            value,
            omitted,
            path="sources.xmp.depth_fixture",
        )
        assert bounded == value
        validator.validate(bounded)
        assert "excessive_nesting" not in omitted["classes"]

    excessive_arrays: Any = "leaf"
    for _ in range(10):
        excessive_arrays = [excessive_arrays]
    excessive_objects: Any = "leaf"
    for index in range(9):
        excessive_objects = {f"level_{index}": excessive_objects}

    for value in (excessive_arrays, excessive_objects):
        omitted = metadata._empty_omitted()
        bounded = metadata._bounded_tag_value(
            value,
            omitted,
            path="sources.xmp.depth_fixture",
        )
        validator.validate(bounded)
        assert "excessive_nesting" in omitted["classes"]


def test_ffprobe_recursively_keeps_scalar_metadata_without_content_heuristics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / "media.wav"
    media.write_bytes(b"RIFF")
    body = {
        "format": {
            "format_name": "wav",
            "duration": "1",
            "tags": {
                "title": {
                    "title": "Nested safe title",
                    "owner": "owner value",
                    "vendor_custom": "custom value",
                    "file_path": "/private/tmp/ffprobe-secret",
                    "description": "https://user:password@example.test/private",
                }
            },
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "tags": {"language": "https://user:password@example.test/private"},
            }
        ],
    }
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("8.1.1"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    result = metadata._ffprobe_adapter(media)
    nested = result.sources["ffprobe"]["tags"]["title"]
    assert nested == {
        "description": "https://user:password@example.test/private",
        "file_path": "/private/tmp/ffprobe-secret",
        "owner": "owner value",
        "title": "Nested safe title",
        "vendor_custom": "custom value",
    }
    assert result.structure["streams"][0]["language"] == (
        "https://user:password@example.test/private"
    )
    serialized = metadata.canonical_json_bytes(
        {"sources": result.sources, "structure": result.structure}
    ).decode()
    assert "ffprobe-secret" in serialized
    assert "user:password" in serialized


def test_ffprobe_preserves_unconsumed_scalar_extras_and_omits_payload_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / "media.mov"
    media.write_bytes(b"media")
    payload_markers = {
        "FORMAT-DATA",
        "FORMAT-PACKETS",
        "STREAM-CODEC-PRIVATE",
        "STREAM-CODEC-PRIVATE-DATA",
        "STREAM-DATA",
        "STREAM-EXTRADATA",
        "STREAM-FRAME",
        "STREAM-FRAME-DATA",
        "STREAM-FRAMES",
        "STREAM-PACKET",
        "STREAM-PACKET-DATA",
        "STREAM-PACKETS",
        "STREAM-PAYLOAD",
        "CHAPTER-PACKET",
        "CHAPTER-FRAME-DATA",
    }
    body = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "duration": "1.000000",
            "bit_rate": "128000",
            "start_time": "0.125000",
            "probe_score": 100,
            "nb_streams": 1,
            "filename": "https://user:password@example.test/media.mov",
            "vendor_scalar": "e\u0301\r\nformat\x00",
            "vendor_mapping": {"not": "a root scalar"},
            "data": "FORMAT-DATA",
            "packets": "FORMAT-PACKETS",
            "tags": {"title": "Example"},
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "1.000000",
                "bit_rate": "128000",
                "sample_rate": "48000",
                "channels": 2,
                "profile": "LC",
                "codec_tag_string": "mp4a",
                "channel_layout": "stereo",
                "bits_per_sample": 16,
                "start_time": "0.125000",
                "nb_frames": "00123",
                "extradata_size": 64,
                "vendor_uri": "https://user:password@example.test/stream",
                "codec_private": "STREAM-CODEC-PRIVATE",
                "codec_private_data": "STREAM-CODEC-PRIVATE-DATA",
                "data": "STREAM-DATA",
                "extradata": "STREAM-EXTRADATA",
                "frame": "STREAM-FRAME",
                "frame_data": "STREAM-FRAME-DATA",
                "frames": "STREAM-FRAMES",
                "packet": "STREAM-PACKET",
                "packet_data": "STREAM-PACKET-DATA",
                "packets": "STREAM-PACKETS",
                "payload": "STREAM-PAYLOAD",
                "tags": {"language": "eng"},
            }
        ],
        "chapters": [
            {
                "id": 7,
                "time_base": "1/1000",
                "start": 125,
                "start_time": "0.125000",
                "end": 1000,
                "end_time": "1.000000",
                "vendor_scalar": True,
                "packet": "CHAPTER-PACKET",
                "frame_data": "CHAPTER-FRAME-DATA",
                "tags": {"title": "Opening", "language": "eng"},
            }
        ],
    }
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("8.1.1"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    result = metadata._ffprobe_adapter(media)

    assert result.sources["ffprobe"]["extras"] == {
        "nb_streams": 1,
        "probe_score": 100,
        "start_time": "0.125000",
        "vendor_scalar": "é\nformat",
    }
    assert "adapter_transport" in result.omitted["classes"]
    assert "sources.ffprobe.extras.filename" in result.omitted["paths"]
    stream = result.structure["streams"][0]
    assert stream["extras"] == {
        "bits_per_sample": 16,
        "channel_layout": "stereo",
        "codec_tag_string": "mp4a",
        "extradata_size": 64,
        "nb_frames": "00123",
        "profile": "LC",
        "start_time": "0.125000",
        "vendor_uri": "https://user:password@example.test/stream",
    }
    chapter = result.structure["chapters"][0]
    assert chapter["extras"] == {
        "end": 1000,
        "start": 125,
        "time_base": "1/1000",
        "vendor_scalar": True,
    }
    assert result.omitted["binary_values"] == len(payload_markers)
    assert "binary" in result.omitted["classes"]
    serialized = metadata.canonical_json_bytes(
        {"sources": result.sources, "structure": result.structure}
    )
    for marker in payload_markers:
        assert marker.encode() not in serialized

    definitions = metadata.MEDIA_METADATA_ENVELOPE_SCHEMA["$defs"]
    for definition, value in (
        ("ffprobe_source", result.sources["ffprobe"]),
        ("stream", stream),
        ("chapter", chapter),
    ):
        Draft202012Validator(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": definitions,
                "$ref": f"#/$defs/{definition}",
            }
        ).validate(value)


def test_subprocess_output_and_envelope_are_hard_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    command = [
        str(Path(sys.executable).resolve()),
        "-I",
        "-c",
        "import os; os.write(1, b'x' * 200000)",
    ]
    command_result = metadata._run_bounded(
        command, timeout=5, stdout_limit=1024, stderr_limit=1024
    )
    assert command_result.output_limited is True
    assert len(command_result.stdout) == 1024

    huge_sources = {
        "exif": {
            f"exif.custom_{index:03d}": "x" * metadata.MAX_SOURCE_STRING_BYTES
            for index in range(48)
        }
    }

    def huge_exiftool(_path, **_kwargs):
        return metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            sources=huge_sources,
        )

    monkeypatch.setattr(metadata, "_exiftool_adapter", huge_exiftool)
    payload = _png_header(8, 6)
    path = tmp_path / "large-envelope.png"
    path.write_bytes(payload)
    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename="large-envelope.png",
        claimed_mime="image/png",
    )
    assert len(metadata.canonical_json_bytes(envelope)) <= (metadata.MAX_DETAILS_BYTES)
    assert envelope["normalized"]["probe_status"] == "partial"
    assert envelope["extraction"]["completeness"] == "terminal_partial"
    assert envelope["extraction"]["retryable"] is False
    assert envelope["omitted"]["source_values"] > 0
    assert any(item["code"] == "resource_limit" for item in envelope["warnings"])
    assert metadata.media_metadata_cache_compatible(
        metadata.cache_entry_from_envelope(envelope)
    )


# realtime: `_run_bounded` supervises a real child process, and a manual
# clock cannot make a real child run faster.
# Irreducible residue: the cancel-to-return promptness bound measures how
# fast a REAL subprocess teardown completes, and a real duration can only be
# measured on a real clock (generous 2s bound vs a near-instant kill) — the
# contract's escape hatch, not a TODO.
@pytest.mark.realtime
def test_subprocess_cancellation_kills_an_inflight_adapter(tmp_path: Path) -> None:
    # Rendezvous, not a timer race: the child's first act atomically
    # publishes a marker, and cancel only fires once the marker exists — the
    # adapter is PROVABLY in flight when the cancel lands (the old
    # threading.Timer(0.1) could fire before the child had even spawned,
    # leaving the "in-flight" claim to timing luck).
    marker = tmp_path / "inflight.marker"
    cancel_event = threading.Event()
    cancel_at: list[float] = []

    def _cancel_once_inflight() -> None:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel_at.append(time.monotonic())
        cancel_event.set()

    watcher = threading.Thread(target=_cancel_once_inflight, daemon=True)
    watcher.start()
    try:
        result = metadata._run_bounded(
            [
                str(Path(sys.executable).resolve()),
                "-I",
                "-c",
                (
                    "import os, sys, time\n"
                    "open(sys.argv[1] + '.tmp', 'w').write('inflight')\n"
                    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
                    "time.sleep(10)"
                ),
                str(marker),
            ],
            timeout=5,
            cancel_event=cancel_event,
        )
    finally:
        watcher.join(timeout=10)

    assert marker.exists(), "the adapter must have been in flight before cancel"
    assert result.cancelled is True
    assert result.timed_out is False
    # Cancel-to-return promptness (real clock — see the marker comment).
    assert time.monotonic() - cancel_at[0] < 2


# realtime: real subprocess process-group teardown (see marker above).
# Irreducible residue: `elapsed < 2` bounds how fast a REAL leader-exit +
# descendant teardown completes — a real duration only a real clock can
# measure (the contract's escape hatch, not a TODO). The "group gone" check
# below is already a positive await (killpg-0 probe with a generous bound),
# not a negative racing the clock.
@pytest.mark.realtime
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group contract")
def test_subprocess_tears_down_descendant_after_leader_exits(tmp_path: Path) -> None:
    marker = tmp_path / "descendant.txt"
    child = (
        "import os,sys,time; "
        "open(sys.argv[1],'w').write(f'{os.getpid()} {os.getpgrp()}'); "
        "time.sleep(30)"
    )
    parent = (
        "import pathlib,subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]])\n"
        "deadline=time.monotonic()+2\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "while not p.exists() and time.monotonic()<deadline:\n"
        "    time.sleep(.01)\n"
    )

    started = time.monotonic()
    result = metadata._run_bounded(
        [str(Path(sys.executable).resolve()), "-I", "-c", parent, str(marker), child],
        timeout=5,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert marker.exists()
    _pid, pgid = (int(item) for item in marker.read_text().split())

    group_gone = False
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                group_gone = True
                break
            time.sleep(0.01)
    finally:
        if not group_gone:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert group_gone
    assert elapsed < 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource contract")
def test_subprocess_worker_applies_cpu_memory_and_fd_limits() -> None:
    result = metadata._run_bounded(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-c",
            (
                "import json,resource; print(json.dumps({"
                "'cpu':resource.getrlimit(resource.RLIMIT_CPU),"
                "'memory':resource.getrlimit(resource.RLIMIT_AS),"
                "'files':resource.getrlimit(resource.RLIMIT_NOFILE)}))"
            ),
        ],
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    limits = json.loads(result.stdout)
    assert 1 <= limits["cpu"][0] <= 5
    if sys.platform != "darwin":
        assert limits["memory"] == [
            metadata.MAX_ADAPTER_ADDRESS_SPACE_BYTES,
            metadata.MAX_ADAPTER_ADDRESS_SPACE_BYTES,
        ]
    assert limits["files"] == [
        metadata.MAX_ADAPTER_OPEN_FILES,
        metadata.MAX_ADAPTER_OPEN_FILES,
    ]


def test_run_bounded_passes_seekable_regular_file_as_stdin(tmp_path: Path) -> None:
    media = tmp_path / "seekable.bin"
    media.write_bytes(b"0123456789")
    result = metadata._run_bounded(
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-c",
            (
                "import json,os; "
                "size=os.lseek(0,0,os.SEEK_END); "
                "os.lseek(0,3,os.SEEK_SET); "
                "print(json.dumps({'size':size,'tail':os.read(0,2).decode()}))"
            ),
        ],
        timeout=5,
        stdin_path=media,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"size": 10, "tail": "34"}


def test_ffprobe_uses_seekable_fd_and_disables_file_and_network_protocols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / "untrusted.m3u8"
    media.write_text("file /private/secret.wav\n", encoding="utf-8")
    captured: dict[str, Any] = {}
    body = {
        "format": {"format_name": "wav", "duration": "1"},
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "sample_rate": "8000",
                "channels": 1,
            }
        ],
    }
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("8.1.1"),
    )

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)
    result = metadata._ffprobe_adapter(media)

    assert result.outcome == "success"
    argv = captured["argv"]
    assert argv[argv.index("-protocol_whitelist") + 1] == "fd"
    assert argv[argv.index("-fd") + 1] == "0"
    assert argv[argv.index("-i") + 1] == "fd:"
    assert "file" not in argv
    assert "http" not in argv
    assert str(media.resolve()) not in argv
    assert captured["stdin_path"] == media


def test_ffprobe_without_input_fd_capability_degrades_without_parser_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_bytes(b"stable executable")
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"media")
    calls: list[list[str]] = []
    metadata._VERSION_CACHE.clear()
    metadata._FFPROBE_FD_INPUT_CACHE.clear()
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if "-version" in argv:
            return metadata._CommandResult(0, b"ffprobe version 5.1.6\n", b"")
        if "-protocols" in argv:
            return metadata._CommandResult(
                0,
                b"Supported file protocols:\nInput:\n  file\n  pipe\nOutput:\n  fd\n",
                b"",
            )
        raise AssertionError("media parser must not run without input fd support")

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)

    first = metadata._ffprobe_adapter(media)
    second = metadata._ffprobe_adapter(media)

    for result in (first, second):
        assert result.outcome == "missing_dependency"
        assert result.available is False
        assert result.sources == {}
        assert result.warnings == [
            {
                "code": "missing_dependency",
                "source": "ffprobe",
                "message": "ffprobe lacks the required seekable fd input protocol",
            }
        ]
    assert ["-protocols" in call for call in calls] == [False, True]


def test_full_audio_video_envelope_schema_and_cache_include_primary_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    payload = b"\x00\x00\x00\x18ftypisom" + (b"\x00" * 12)
    media = tmp_path / "clip.mp4"
    media.write_bytes(payload)
    body = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "duration": "2.5",
            "bit_rate": "256000",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "duration": "2.5",
                "bit_rate": "192000",
                "width": 640,
                "height": 360,
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
                "disposition": {"default": 1},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "duration": "2.5",
                "bit_rate": "64000",
                "sample_rate": "48000",
                "channels": 2,
                "disposition": {"default": 1},
            },
        ],
    }
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("8.1.1"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )
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

    envelope = metadata.extract_media_metadata(
        media,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename="clip.mp4",
        claimed_mime="video/mp4",
    )

    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(envelope)
    assert envelope["sources"]["ffprobe"]["primary_video_index"] == 0
    assert envelope["sources"]["ffprobe"]["primary_audio_index"] == 1
    assert envelope["extraction"]["completeness"] == "complete"
    assert metadata.media_metadata_cache_compatible(
        metadata.cache_entry_from_envelope(envelope)
    )


@pytest.mark.parametrize(
    ("file_type", "mime", "expected"),
    [
        ("JPG", "application/octet-stream", ("image", "jpeg", "image/jpeg")),
        ("M4A", "audio/x-vendor", ("audio", "mp4", "audio/mp4")),
        (
            "",
            "video/quicktime; charset=binary",
            ("video", "quicktime", "video/quicktime"),
        ),
        (
            "unknown",
            "audio/x-frisket-private",
            ("audio", None, "audio/x-frisket-private"),
        ),
        ("../../image/x-hostile", "image/x-hostile-format", None),
        ("x" * 10_000, "video/" + "y" * 10_000, None),
    ],
)
def test_tool_format_registry_is_closed_and_stable(
    file_type: str,
    mime: str,
    expected: tuple[str, str | None, str | None] | None,
) -> None:
    assert metadata._format_from_tool(file_type, mime) == expected


def test_exact_mime_disambiguates_only_ambiguous_container_tool_tags() -> None:
    assert metadata._format_from_tool("MP4", "audio/mp4") == (
        "audio",
        "mp4",
        "audio/mp4",
    )
    assert metadata._format_from_tool("MOV", "audio/quicktime") == (
        "audio",
        "quicktime",
        "audio/quicktime",
    )
    assert metadata._format_from_tool("WEBM", "audio/webm") == (
        "audio",
        "webm",
        "audio/webm",
    )
    # An unrelated MIME claim cannot override a specific parser file type.
    assert metadata._format_from_tool("JPEG", "video/mp4") == (
        "image",
        "jpeg",
        "image/jpeg",
    )


def test_exact_exiftool_audio_identity_corrects_generic_mp4_signature_without_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12
    media = tmp_path / "audio-only.mp4"
    media.write_bytes(payload)
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            recognitions=[
                metadata._recognition(
                    "audio",
                    "mp4",
                    "audio/mp4",
                    offset=0,
                    confidence=85,
                    source="exiftool",
                )
            ],
        ),
    )
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    monkeypatch.setattr(
        metadata,
        "_ffprobe_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="ffprobe",
            outcome="missing_dependency",
            available=False,
        ),
    )

    envelope = metadata.extract_media_metadata(
        media,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=media.name,
        claimed_mime=None,
    )

    assert envelope["normalized"]["kind"] == "audio"
    assert envelope["normalized"]["format"] == "mp4"
    assert envelope["normalized"]["mime"] == "audio/mp4"


@pytest.mark.parametrize("with_real_video", [False, True])
def test_ffprobe_projects_attached_picture_descriptors_without_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_real_video: bool,
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / ("movie.mp4" if with_real_video else "song.mp3")
    media.write_bytes(b"media")
    streams: list[dict[str, Any]] = [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "aac" if with_real_video else "mp3",
            "disposition": {"default": 1},
        }
    ]
    if with_real_video:
        streams.append(
            {
                "index": 4,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"default": 1, "attached_pic": 0},
            }
        )
    streams.append(
        {
            "index": 9,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 1200,
            "height": 1200,
            "disposition": {"attached_pic": 1},
            "tags": {"title": "Front cover"},
            # ffprobe can expose packet/extradata fields in broader queries;
            # the descriptor projection must never retain any such bytes.
            "data": "SENSITIVE-ARTWORK-BYTES",
            "extradata": "SENSITIVE-EXTRADATA-BYTES",
        }
    )
    body = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        "streams": streams,
    }
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("8.1.1"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    result = metadata._ffprobe_adapter(media)

    expected_position = 2 if with_real_video else 1
    assert result.structure["artwork"] == [
        {
            "source_index": 9,
            "type": "attached_pic",
            "mime": "image/jpeg",
            "width_pixels": 1200,
            "height_pixels": 1200,
            "description": "Front cover",
            "source_paths": [f"structure.streams.{expected_position}"],
        }
    ]
    assert result.recognitions[0]["kind"] == ("video" if with_real_video else "audio")
    serialized = metadata.canonical_json_bytes(result.structure)
    assert b"SENSITIVE-ARTWORK-BYTES" not in serialized
    assert b"SENSITIVE-EXTRADATA-BYTES" not in serialized


def test_artwork_descriptor_bounds_hostile_values_and_unknown_codec() -> None:
    descriptor = metadata._artwork_descriptor_from_stream(
        {
            "index": -1,
            "codec": "private-image-codec",
            "disposition": {"attached_pic": True},
            "width_pixels": 0,
            "height_pixels": "1e10000",
            "tags": {
                "title": "x" * 10_000,
                "description": {"nested": "must not stringify"},
            },
            "packet": b"never serialize me",
        },
        position=7,
    )

    assert descriptor == {
        "source_index": None,
        "type": "attached_pic",
        "mime": None,
        "width_pixels": None,
        "height_pixels": None,
        "description": "x" * 1024,
        "source_paths": ["structure.streams.7"],
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": metadata.MEDIA_METADATA_ENVELOPE_SCHEMA["$defs"],
        "$ref": "#/$defs/artwork",
    }
    Draft202012Validator(schema).validate(descriptor)


def test_ffprobe_version_probe_timeout_refuses_parser_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"ID3")
    clock = [100.0]
    calls: list[dict[str, Any]] = []
    body = {
        "format": {"format_name": "mp3", "duration": "1"},
        "streams": [{"index": 0, "codec_type": "audio", "codec_name": "mp3"}],
    }
    metadata._VERSION_CACHE.clear()
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(metadata.time, "monotonic", lambda: clock[0])

    def fake_run(_argv, **kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            clock[0] += 5.0
            return metadata._CommandResult(
                returncode=-1,
                stdout=b"",
                stderr=b"",
                timed_out=True,
            )
        return metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)
    result = metadata._ffprobe_adapter(media, timeout=20.0)

    assert result.outcome == "missing_dependency"
    assert result.available is False
    assert calls[0]["timeout"] == 5.0
    assert len(calls) == 1


def test_not_applicable_ffprobe_does_not_run_an_unbounded_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    payload = _png_header(9, 6)
    media = tmp_path / "image.png"
    media.write_bytes(payload)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args, **_kwargs: pytest.fail(
            "not-applicable ffprobe must not run a version probe"
        ),
    )
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool", outcome="success", available=True, version="13.59"
        ),
    )
    monkeypatch.setattr(
        metadata,
        "_ffprobe_adapter",
        lambda *_args, **_kwargs: pytest.fail("ffprobe must not parse an image"),
    )

    envelope = metadata.extract_media_metadata(
        media,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=None,
        claimed_mime=None,
    )

    ffprobe = envelope["extraction"]["adapters"]["ffprobe"]
    assert ffprobe == {
        "required": False,
        "available": True,
        "outcome": "not_applicable",
        "version": None,
    }


def test_cancellation_during_version_probe_skips_parser_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / "audio.mp3"
    media.write_bytes(b"ID3")
    cancel_event = threading.Event()
    calls = 0
    metadata._VERSION_CACHE.clear()
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: executable)

    def fake_run(_argv, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["cancel_event"] is cancel_event
        cancel_event.set()
        return metadata._CommandResult(
            returncode=-1,
            stdout=b"",
            stderr=b"",
            cancelled=True,
        )

    monkeypatch.setattr(metadata, "_run_bounded", fake_run)
    result = metadata._ffprobe_adapter(
        media,
        timeout=20.0,
        cancel_event=cancel_event,
    )

    assert result.outcome == "cancelled"
    assert calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b'[{"[EXIF]Title":"\\ud800"}]',
        b'[{"[EXIF]Title":' + (b"[" * 20_000) + b"0" + (b"]" * 20_000) + b"}]",
    ],
    ids=("invalid-json", "lone-surrogate", "excessive-json-nesting"),
)
def test_exiftool_hostile_json_is_a_bounded_adapter_failure(
    payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("placeholder", encoding="utf-8")
    media = tmp_path / "image.jpg"
    media.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(
        metadata,
        "_tool_version",
        lambda *_args: _fake_tool_version("13.59"),
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0, stdout=payload, stderr=b""
        ),
    )

    result = metadata._exiftool_adapter(media)

    assert result.outcome == "internal_error"
    assert result.warnings[0]["message"] == "ExifTool returned invalid JSON"


def test_policy_omissions_preserve_ok_complete_cache(
    tmp_path: Path, monkeypatch
) -> None:
    payload = _png_header(9, 6)
    path = tmp_path / "policy-omissions.png"
    path.write_bytes(payload)

    def policy_filtered_exiftool(_path, **_kwargs):
        omitted = metadata._empty_omitted()
        for key, semantic_class in (
            ("binary_values", "binary"),
            ("source_values", "adapter_transport"),
        ):
            metadata._record_omission(
                omitted,
                key,
                path=f"fixture.{semantic_class}",
                semantic_class=semantic_class,
            )
        return metadata._AdapterResult(
            name="exiftool",
            outcome="success",
            available=True,
            version="13.59",
            sources={"exif": {"exif.title": "Safe title"}},
            omitted=omitted,
        )

    monkeypatch.setattr(metadata, "_exiftool_adapter", policy_filtered_exiftool)
    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename="policy-omissions.png",
        claimed_mime="image/png",
    )

    assert envelope["normalized"]["probe_status"] == "ok"
    assert envelope["extraction"]["completeness"] == "complete"
    assert envelope["extraction"]["retryable"] is False
    entry = metadata.cache_entry_from_envelope(envelope)
    assert metadata.media_metadata_cache_compatible(entry)
    assert set(entry) == {
        "cache_schema_version",
        "generation",
        "extractor_version",
        "content_facts_hash",
        "content_facts",
    }
    persisted = json.loads(metadata.canonical_json_bytes(entry))
    assert metadata.media_metadata_cache_compatible(persisted)


def test_omission_display_overflow_is_exact() -> None:
    omitted = metadata._empty_omitted()
    for index in range(2_048):
        metadata._record_omission(
            omitted, "source_values", path=f"source.path.{index:04d}"
        )
    for index in range(80):
        metadata._record_omission(
            omitted, "source_values", semantic_class=f"class_{index:03d}"
        )
    metadata._record_omission(
        omitted, "source_values", semantic_class="adapter_transport"
    )

    assert omitted["paths"] == [f"source.path.{index:04d}" for index in range(64)]
    assert omitted["paths_overflow"] == 1_984
    assert omitted["classes"] == [
        "adapter_transport",
        *(f"class_{index:03d}" for index in range(63)),
    ]
    assert omitted["classes_overflow"] == 17


def test_cache_roundtrip_restores_hidden_material_omission_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, digest, envelope = _extract_png(
        tmp_path, monkeypatch, filename=None, claimed_mime=None
    )
    for index in range(64):
        metadata._record_omission(
            envelope["omitted"],
            "source_values",
            semantic_class=f"a_class_{index:02d}",
        )
    metadata._record_omission(
        envelope["omitted"],
        "source_values",
        semantic_class="source_value_limit",
    )
    envelope["normalized"]["probe_status"] = "partial"
    envelope["extraction"]["completeness"] = "terminal_partial"

    entry = metadata.cache_entry_from_envelope(envelope)
    persisted = json.loads(metadata.canonical_json_bytes(entry))

    assert metadata.media_metadata_cache_compatible(entry)
    assert metadata.media_metadata_cache_compatible(persisted)
    restored = metadata.envelope_from_cache(
        persisted,
        digest=digest,
        size_bytes=len(_png_header(17, 11)),
    )
    assert "source_value_limit" in metadata._omitted_labels(
        restored["omitted"], "classes"
    )


def test_cache_roundtrip_preserves_exact_omission_overflow_during_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, digest, envelope = _extract_png(
        tmp_path, monkeypatch, filename=None, claimed_mime=None
    )
    for index in range(65):
        metadata._record_omission(
            envelope["omitted"],
            "source_values",
            path=f"a.path.{index:02d}",
        )
    envelope["conflicts"] = [
        {
            "field": f"fixture_{index:03d}",
            "candidate_paths": [],
            "reason": f"fixture_{index:03d}",
        }
        for index in range(metadata.MAX_CONFLICTS)
    ]
    entry = metadata.cache_entry_from_envelope(envelope)
    cold_template = entry["content_facts"]["envelope"]
    persisted = json.loads(metadata.canonical_json_bytes(entry))
    warm_template = metadata.envelope_from_cache(
        persisted,
        digest=digest,
        size_bytes=len(path.read_bytes()),
    )

    overlay_kwargs = {
        "filename": "wrong.jpg",
        "claimed_mime": "video/mp4",
    }
    cold = metadata.overlay_reference_evidence(cold_template, **overlay_kwargs)
    warm = metadata.overlay_reference_evidence(warm_template, **overlay_kwargs)

    assert metadata.canonical_envelope_json(cold) == (
        metadata.canonical_envelope_json(warm)
    )
    assert cold["omitted"]["paths_overflow"] == 2
    assert warm["omitted"]["paths_overflow"] == 2


def test_cache_rejects_missing_or_inexact_omission_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _digest, envelope = _extract_png(
        tmp_path, monkeypatch, filename=None, claimed_mime=None
    )
    for index in range(65):
        metadata._record_omission(
            envelope["omitted"], "source_values", path=f"fixture.path.{index:02d}"
        )
    entry = metadata.cache_entry_from_envelope(envelope)

    missing = copy.deepcopy(entry)
    del missing["content_facts"]["omitted_labels"]
    _rehash_cache_entry(missing)
    assert metadata.media_metadata_cache_compatible(missing) is False

    duplicate = copy.deepcopy(entry)
    tail = duplicate["content_facts"]["omitted_labels"]["paths"]
    tail.append(tail[0])
    _rehash_cache_entry(duplicate)
    assert metadata.media_metadata_cache_compatible(duplicate) is False

    plain = json.loads(metadata.canonical_envelope_json(envelope))
    with pytest.raises(ValueError, match="cannot recover exact omission labels"):
        metadata.cache_entry_from_envelope(plain)


def test_oversized_exact_omission_sidecar_is_bounded_and_bypassable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _digest, envelope = _extract_png(
        tmp_path, monkeypatch, filename=None, claimed_mime=None
    )
    for index in range(600):
        suffix = hashlib.sha256(str(index).encode()).hexdigest()
        long_path = f"{index:04d}." + (suffix * 8)
        metadata._record_omission(
            envelope["omitted"],
            "source_values",
            path=long_path[:512],
        )

    with pytest.raises(ValueError, match="cache facts exceed the cache bound"):
        metadata.cache_entry_from_envelope(envelope)


def test_hostile_numeric_dates_nested_tags_and_transient_cache_state() -> None:
    assert metadata._integer("7.0") == 7
    assert metadata._integer("7.5") is None
    assert metadata._integer("1e1000000") is None
    assert metadata._finite_decimal("1e1000000") is None
    assert metadata._rational_rate("30000/1001") == 29.97003
    assert metadata._rational_rate("0/1") is None
    assert metadata._iso_date("D:20260718103000-04'00'") == (
        "2026-07-18T10:30:00-04:00"
    )
    assert metadata._iso_date("D:20260718103000-04'00'junk") is None
    assert metadata._iso_date("2026-07") is None

    value, path = metadata._find_candidate(
        {"ffprobe": {"tags": {"title": "Nested title"}}},
        ("ffprobe",),
        ("title",),
    )
    assert value == "Nested title"
    assert path == "sources.ffprobe.tags.title"

    date_value, date_path = metadata._find_candidate(
        {
            "exif": {
                "exif.create_date": "2026-07-18T11:00:00-04:00",
                "exif.date_time_original": "2026-07-18T10:00:00-04:00",
            }
        },
        ("exif",),
        ("date_time_original", "create_date"),
    )
    assert date_value == "2026-07-18T10:00:00-04:00"
    assert date_path == "sources.exif.exif.date_time_original"

    completeness, retryable = metadata._cache_completeness(
        "partial",
        {
            "exiftool": {"outcome": "missing_dependency"},
            "ffprobe": {"outcome": "timeout"},
        },
    )
    assert (completeness, retryable) == ("retryable_failure", True)
    assert metadata._cache_completeness(
        "partial", {"ffprobe": {"outcome": "resource_limit"}}
    ) == ("terminal_partial", False)
    assert metadata._cache_completeness(
        "partial", {"exiftool": {"outcome": "missing_dependency"}}
    ) == ("degraded_missing_dependency", False)

    for completeness, retryable in (
        ("retryable_failure", True),
        ("degraded_missing_dependency", False),
    ):
        envelope = {
            "normalized": {"probe_status": "partial"},
            "extraction": {
                "completeness": completeness,
                "retryable": retryable,
            },
        }
        metadata._mark_resource_limited(envelope)
        assert envelope["extraction"] == {
            "completeness": completeness,
            "retryable": retryable,
        }


def test_extraction_has_no_per_row_execution_pin_gate() -> None:
    parameters = inspect.signature(metadata.extract_media_metadata).parameters
    assert "expected_execution_pin" not in parameters


def test_video_descriptive_fields_fall_back_to_primary_stream_tags() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    structure = {
        "streams": [
            {
                "index": 3,
                "type": "video",
                "codec": "h264",
                "disposition": {"default": True, "attached_pic": False},
                "tags": {
                    "title": "Stream title",
                    "artist": "Stream creator",
                    "creation_time": "2026-07-18T10:30:00-04:00",
                },
            }
        ],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }
    normalized, lineage, warnings = metadata._normalize(
        {"kind": "video", "format": "mp4", "mime": "video/mp4", "source": "ffprobe"},
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="a" * 64,
        size_bytes=123,
        omitted=metadata._empty_omitted(),
    )

    assert warnings == []
    assert normalized["title"] == "Stream title"
    assert normalized["creator"] == "Stream creator"
    assert normalized["created_at"] == "2026-07-18T10:30:00-04:00"
    assert lineage["fields"]["title"]["selected"] == ("structure.streams.0.tags.title")
    assert lineage["fields"]["created_at"]["selected"] == (
        "structure.streams.0.tags.creation_time"
    )
    envelope = {
        "normalized": normalized,
        "sources": sources,
        "structure": structure,
        "lineage": lineage,
        "extraction": {"adapters": {}},
    }
    assert all(
        entry["selected"] is None
        or metadata._metadata_path_resolves(envelope, entry["selected"])
        for entry in lineage["fields"].values()
    )


def test_audio_cover_art_is_not_promoted_as_a_video_stream() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    structure = {
        "streams": [
            {
                "index": 0,
                "type": "audio",
                "codec": "mp3",
                "sample_rate_hz": 44100,
                "channel_count": 2,
                "disposition": {"default": True, "attached_pic": False},
                "tags": {},
            },
            {
                "index": 1,
                "type": "video",
                "codec": "mjpeg",
                "width_pixels": 1200,
                "height_pixels": 1200,
                "avg_frame_rate": "0/0",
                "disposition": {"default": False, "attached_pic": True},
                "tags": {},
            },
        ],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }

    normalized, lineage, _ = metadata._normalize(
        {"kind": "audio", "format": "mp3", "mime": "audio/mpeg", "source": "ffprobe"},
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="c" * 64,
        size_bytes=123,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["kind"] == "audio"
    assert normalized["audio_codec"] == "mp3"
    assert normalized["video_codec"] is None
    assert normalized["width_pixels"] is None
    assert normalized["height_pixels"] is None
    assert "video_codec" not in lineage["fields"]


def test_duration_prefers_container_and_reports_divergent_primary_streams() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["ffprobe"]["duration_seconds"] = 100.0
    structure = {
        "streams": [
            {
                "index": 0,
                "type": "audio",
                "codec": "aac",
                "duration_seconds": 104.0,
                "disposition": {"default": True, "attached_pic": False},
                "tags": {},
            },
            {
                "index": 1,
                "type": "video",
                "codec": "h264",
                "duration_seconds": 100.5,
                "disposition": {"default": True, "attached_pic": False},
                "tags": {},
            },
        ],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }

    normalized, lineage, warnings = metadata._normalize(
        {"kind": "video", "format": "mp4", "mime": "video/mp4"},
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="d" * 64,
        size_bytes=1000,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["duration_seconds"] == 100.0
    assert lineage["fields"]["duration_seconds"] == {
        "selected": "sources.ffprobe.duration_seconds",
        "candidates": [
            "structure.streams.0.duration_seconds",
            "structure.streams.1.duration_seconds",
        ],
        "transforms": [],
    }
    assert warnings == [
        {
            "code": "conflict",
            "subtype": "duration",
            "source": "ffprobe",
            "path": "structure.streams.0.duration_seconds",
            "message": (
                "Container and selected stream durations diverge beyond tolerance"
            ),
        }
    ]


def test_zero_container_duration_is_a_valid_nonnegative_duration() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["ffprobe"]["duration_seconds"] = 0.0

    normalized, lineage, warnings = metadata._normalize(
        {"kind": "audio", "format": "wav", "mime": "audio/wav"},
        sources,
        {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="0" * 64,
        size_bytes=1000,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["duration_seconds"] == 0.0
    assert normalized["bitrate_bps"] is None
    assert lineage["fields"]["duration_seconds"]["selected"] == (
        "sources.ffprobe.duration_seconds"
    )
    assert warnings == []


def test_complete_bwf_origination_maps_to_created_at_without_invented_timezone() -> (
    None
):
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["riff"] = {
        "riff.origination_date": "2026:07:18",
        "riff.origination_time": "12:34:56",
        # Music release dates must not be promoted to created_at.
        "riff.date": "1999",
    }

    normalized, lineage, warnings = metadata._normalize(
        {"kind": "audio", "format": "wav", "mime": "audio/wav"},
        sources,
        {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="e" * 64,
        size_bytes=1000,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["created_at"] == "2026-07-18T12:34:56"
    assert not normalized["created_at"].endswith(("Z", "+00:00"))
    assert lineage["fields"]["created_at"] == {
        "selected": "sources.riff.riff.origination_date",
        "candidates": [
            "sources.riff.riff.origination_date",
            "sources.riff.riff.origination_time",
        ],
        "transforms": ["bwf_origination_join", "iso8601_parse"],
    }
    assert warnings == []


def test_rotated_dimensions_align_values_lineage_and_required_orientation() -> None:
    video_sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    video_structure = {
        "streams": [
            {
                "index": 0,
                "type": "video",
                "codec": "h264",
                "disposition": {"default": True, "attached_pic": False},
                "width_pixels": 1920,
                "height_pixels": 1080,
                "rotation_degrees": 90,
            }
        ],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }
    normalized, lineage, _warnings = metadata._normalize(
        {"kind": "video", "format": "mp4", "mime": "video/mp4"},
        video_sources,
        video_structure,
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="1" * 64,
        size_bytes=100,
        omitted=metadata._empty_omitted(),
    )
    assert (normalized["width_pixels"], normalized["height_pixels"]) == (1080, 1920)
    assert lineage["fields"]["width_pixels"] == {
        "selected": "structure.streams.0.height_pixels",
        "candidates": ["structure.streams.0.rotation_degrees"],
        "transforms": ["display_rotation"],
    }
    assert lineage["fields"]["height_pixels"] == {
        "selected": "structure.streams.0.width_pixels",
        "candidates": ["structure.streams.0.rotation_degrees"],
        "transforms": ["display_rotation"],
    }

    image_sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    image_sources["blob"] = {
        "header_width_pixels": 4000,
        "header_height_pixels": 3000,
    }
    image_sources["exif"] = {"exif.orientation": 6}
    image_normalized, image_lineage, _warnings = metadata._normalize(
        {"kind": "image", "format": "jpeg", "mime": "image/jpeg"},
        image_sources,
        {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="2" * 64,
        size_bytes=100,
        omitted=metadata._empty_omitted(),
    )
    assert (
        image_normalized["width_pixels"],
        image_normalized["height_pixels"],
    ) == (3000, 4000)
    assert image_lineage["fields"]["width_pixels"] == {
        "selected": "sources.blob.header_height_pixels",
        "candidates": ["sources.exif.exif.orientation"],
        "transforms": ["exif_orientation"],
    }
    assert image_lineage["fields"]["height_pixels"] == {
        "selected": "sources.blob.header_width_pixels",
        "candidates": ["sources.exif.exif.orientation"],
        "transforms": ["exif_orientation"],
    }


def test_pruning_required_transform_inputs_invalidates_derived_values() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["riff"] = {
        "riff.origination_date": "2026:07:18",
        "riff.origination_time": "12:34:56",
    }
    sources["ffprobe"]["duration_seconds"] = 10.0
    normalized, lineage, _warnings = metadata._normalize(
        {"kind": "audio", "format": "wav", "mime": "audio/wav"},
        sources,
        {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="3" * 64,
        size_bytes=1_000,
        omitted=metadata._empty_omitted(),
    )
    assert lineage["fields"]["bitrate_bps"] == {
        "selected": "sources.blob.size_bytes",
        "candidates": ["sources.ffprobe.duration_seconds"],
        "transforms": ["computed_bitrate"],
    }
    envelope = {
        "normalized": normalized,
        "sources": sources,
        "structure": {
            "streams": [],
            "chapters": [],
            "artwork": [],
            "pages": {},
            "dates": {
                "creation": {
                    "raw": "2026:07:18T12:34:56",
                    "precision": "second",
                    "source_paths": [
                        "sources.riff.riff.origination_date",
                        "sources.riff.riff.origination_time",
                    ],
                }
            },
        },
        "lineage": lineage,
        "omitted": metadata._empty_omitted(),
    }

    metadata._remove_provenance_references(
        envelope, "sources.riff.riff.origination_time"
    )
    assert envelope["normalized"]["created_at"] is None
    assert "created_at" not in envelope["lineage"]["fields"]
    assert envelope["structure"]["dates"]["creation"] is None

    metadata._remove_provenance_references(envelope, "sources.ffprobe.duration_seconds")
    assert envelope["normalized"]["duration_seconds"] is None
    assert envelope["normalized"]["bitrate_bps"] is None
    assert "duration_seconds" not in envelope["lineage"]["fields"]
    assert "bitrate_bps" not in envelope["lineage"]["fields"]


@pytest.mark.parametrize(
    ("quicktime", "xmp", "expected", "selected"),
    [
        (
            {"make": "QT Make", "model": "QT Model"},
            {"make": "XMP Make", "model": "XMP Model"},
            "QT Make QT Model",
            "sources.quicktime.make",
        ),
        (
            {"make": "Incomplete QT"},
            {"make": "XMP Make", "model": "XMP Model"},
            "XMP Make XMP Model",
            "sources.xmp.make",
        ),
        (
            {"make": "Incomplete QT"},
            {"model": "Incomplete XMP"},
            "Stream Make Stream Model",
            "structure.streams.0.tags.make",
        ),
    ],
)
def test_video_capture_device_uses_complete_registered_pairs_then_stream(
    quicktime: dict[str, str],
    xmp: dict[str, str],
    expected: str,
    selected: str,
) -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["quicktime"] = dict(quicktime)
    sources["xmp"] = dict(xmp)
    structure = {
        "streams": [
            {
                "index": 3,
                "type": "video",
                "codec": "h264",
                "disposition": {"default": True, "attached_pic": False},
                "tags": {"make": "Stream Make", "model": "Stream Model"},
            }
        ],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }

    normalized, lineage, _ = metadata._normalize(
        {"kind": "video", "format": "mp4", "mime": "video/mp4"},
        sources,
        structure,
        {
            "content_recognizer": {"outcome": "success"},
            "ffprobe": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="f" * 64,
        size_bytes=1000,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["capture_device"] == expected
    assert lineage["fields"]["capture_device"]["selected"] == selected


def test_pruning_model_invalidates_make_model_join() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["xmp"] = {"xmp.make": "Camera", "xmp.model": "Model"}
    normalized, lineage, _warnings = metadata._normalize(
        {"kind": "image", "format": "jpeg", "mime": "image/jpeg"},
        sources,
        {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "exiftool": {"outcome": "success"},
        },
        digest="4" * 64,
        size_bytes=100,
        omitted=metadata._empty_omitted(),
    )
    envelope = {
        "normalized": normalized,
        "sources": sources,
        "structure": {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        "lineage": lineage,
        "omitted": metadata._empty_omitted(),
    }

    metadata._remove_provenance_references(envelope, "sources.xmp.xmp.model")

    assert envelope["normalized"]["capture_device"] is None
    assert "capture_device" not in envelope["lineage"]["fields"]


def test_encrypted_pdf_with_encryption_fact_is_partial_not_error() -> None:
    sources = {name: {} for name in metadata.SOURCE_NAMESPACES}
    sources["pdf_info"]["encrypted"] = True
    normalized, _, _ = metadata._normalize(
        {
            "kind": "pdf",
            "format": "pdf",
            "mime": "application/pdf",
            "source": "content_signature",
        },
        sources,
        {"streams": [], "chapters": [], "artwork": [], "pages": {}},
        {
            "content_recognizer": {"outcome": "success"},
            "pypdf": {"outcome": "encrypted"},
            "exiftool": {"outcome": "success"},
        },
        digest="b" * 64,
        size_bytes=321,
        omitted=metadata._empty_omitted(),
    )

    assert normalized["pdf_encrypted"] is True
    assert normalized["probe_status"] == "partial"


def test_real_tail_moov_probe_keeps_seek_dependent_video_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe are required for the real container fixture")
    path = tmp_path / "tail-moov.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=0.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ],
        check=True,
    )
    payload = path.read_bytes()
    assert 0 < payload.find(b"mdat") < payload.find(b"moov")
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: Path(ffprobe))

    result = metadata._ffprobe_adapter(path)

    assert result.outcome == "success", result.warnings
    source = result.sources["ffprobe"]
    assert source["bit_rate_bps"] > 0
    assert source["extras"]["size"] == str(len(payload))
    assert "filename" not in source["extras"]
    video = next(
        item for item in result.structure["streams"] if item["type"] == "video"
    )
    assert video["pixel_format"] == "yuv420p"
    assert video["extras"]["profile"]
    assert "adapter_transport" in result.omitted["classes"]
    assert "sources.ffprobe.extras.filename" in result.omitted["paths"]


@pytest.mark.parametrize(
    ("suffix", "codec_args", "expected_format", "expected_mime"),
    [
        (".mp4", ["-c:a", "aac"], "mp4", "audio/mp4"),
        (".mov", ["-c:a", "aac"], "quicktime", "audio/quicktime"),
        (".webm", ["-c:a", "libopus"], "webm", "audio/webm"),
        (".mka", ["-c:a", "pcm_s16le"], "matroska", "audio/x-matroska"),
    ],
)
def test_stream_topology_wins_for_audio_only_ambiguous_containers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    codec_args: list[str],
    expected_format: str,
    expected_mime: str,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe are required for the real container fixture")
    path = tmp_path / f"audio-only{suffix}"
    subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.1",
            *codec_args,
            "-y",
            str(path),
        ],
        check=True,
    )
    payload = path.read_bytes()
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: Path(ffprobe))
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda _path, **_kwargs: metadata._AdapterResult(
            name="exiftool", outcome="success", available=True, version="13.59"
        ),
    )

    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=path.name,
        claimed_mime=None,
    )

    assert envelope["normalized"]["kind"] == "audio", envelope
    assert envelope["normalized"]["format"] == expected_format
    assert envelope["normalized"]["mime"] == expected_mime
    assert envelope["normalized"]["video_codec"] is None
    assert envelope["normalized"]["audio_codec"]
    assert envelope["normalized"]["probe_status"] == "ok"


def test_adapter_invalid_version_refuses_parser_launch(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "exiftool"
    executable.write_text("placeholder")
    blob = tmp_path / "blob"
    blob.write_bytes(_png_header())
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: executable)
    monkeypatch.setattr(metadata, "_tool_version", lambda *_args: None)

    def fail_launch(*_args, **_kwargs):
        raise OSError("host path that must not be persisted")

    monkeypatch.setattr(metadata, "_run_bounded", fail_launch)
    result = metadata._exiftool_adapter(blob)
    assert result.outcome == "missing_dependency"
    assert result.available is False
    assert result.warnings == [
        {
            "code": "missing_dependency",
            "source": "exiftool",
            "message": "ExifTool failed its version probe",
        }
    ]


def test_pypdf_worker_uses_the_installed_virtualenv_runtime() -> None:
    fixture = (
        Path(__file__).parent.parent / "fixtures" / "pdf_tables" / "simple_table.pdf"
    )
    result = metadata._pypdf_adapter(fixture)

    assert result.outcome == "success"
    assert result.available is True
    assert result.version
    assert result.sources["pdf_info"]["page_count"] == 1
    assert result.sources["pdf_info"]["encrypted"] is False
    assert result.structure["pages"]["count"] == 1


def test_pypdf_worker_accounts_for_page_descriptors_clipped_in_worker(
    tmp_path: Path,
) -> None:
    from pypdf import PdfWriter

    fixture = tmp_path / "many-pages.pdf"
    writer = PdfWriter()
    for _ in range(metadata.MAX_PAGE_DESCRIPTORS + 1):
        writer.add_blank_page(width=72, height=72)
    with fixture.open("wb") as handle:
        writer.write(handle)

    result = metadata._pypdf_adapter(fixture)

    assert result.outcome == "success"
    assert result.sources["pdf_info"]["page_count"] == (
        metadata.MAX_PAGE_DESCRIPTORS + 1
    )
    assert len(result.structure["pages"]["descriptors"]) == (
        metadata.MAX_PAGE_DESCRIPTORS
    )
    assert result.omitted["structure_items"] == 1
    assert "structure.pages.descriptors" in result.omitted["paths"]
    assert "pages" in result.omitted["classes"]


def test_pypdf_fixed_fields_keep_scalar_metadata_and_close_page_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"%PDF-1.7\n"
    fixture = tmp_path / "adversarial.pdf"
    fixture.write_bytes(payload)
    body = {
        "version": "6.1.0",
        "encrypted": False,
        "page_count": 2,
        "metadata": {
            "/Title": {
                "Title": "Nested safe title",
                "VendorCustom": "custom value",
                "FilePath": "/private/tmp/pypdf-secret",
                "Description": "https://example.test/?X-Amz-Credential=secret",
            },
            # Fixed PDF fields remain legitimate scalar sources even if their
            # public key is not in the generic native-tag allow-list.
            "/Producer": "Legitimate PDF producer",
            "/Author": r"C:\Users\operator\private.pdf",
            "/Keywords": "https://user:password@example.test/private",
        },
        "pages": [
            {
                "index": 0,
                "width_points": 612,
                "height_points": 792,
                "rotation_degrees": 0,
                "host_path": "/private/tmp/pypdf-secret",
            },
            {"index": 1, "width_points": "invalid"},
        ],
    }
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(
        metadata,
        "_pypdf_version",
        lambda: "6.1.0",
    )
    monkeypatch.setattr(
        metadata,
        "_run_bounded",
        lambda *_args, **_kwargs: metadata._CommandResult(
            returncode=0,
            stdout=json.dumps(body).encode(),
            stderr=b"",
        ),
    )

    result = metadata._pypdf_adapter(fixture)
    assert result.sources["pdf_info"]["title"] == {
        "Description": "https://example.test/?X-Amz-Credential=secret",
        "FilePath": "/private/tmp/pypdf-secret",
        "Title": "Nested safe title",
        "VendorCustom": "custom value",
    }
    assert result.sources["pdf_info"]["producer"] == "Legitimate PDF producer"
    assert result.sources["pdf_info"]["author"] == r"C:\Users\operator\private.pdf"
    assert result.sources["pdf_info"]["keywords"] == (
        "https://user:password@example.test/private"
    )
    assert result.structure["pages"]["descriptors"] == [
        {
            "index": 0,
            "width_points": 612.0,
            "height_points": 792.0,
            "rotation_degrees": 0,
        }
    ]

    serialized_adapter = metadata.canonical_json_bytes(
        {"sources": result.sources, "structure": result.structure}
    ).decode()
    assert "pypdf-secret" in serialized_adapter
    assert "operator" in serialized_adapter
    assert "X-Amz-Credential" in serialized_adapter
    assert "user:password" in serialized_adapter

    envelope = metadata.extract_media_metadata(
        fixture,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=fixture.name,
        claimed_mime="application/pdf",
    )
    Draft202012Validator(metadata.MEDIA_METADATA_ENVELOPE_SCHEMA).validate(envelope)
    assert "host_path" not in envelope["structure"]["pages"]["descriptors"][0]


def test_non_header_image_format_does_not_claim_corruption(
    tmp_path: Path, monkeypatch
) -> None:
    payload = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 12
    path = tmp_path / "image.heic"
    path.write_bytes(payload)

    def successful_exiftool(_path, **_kwargs):
        return metadata._AdapterResult(
            name="exiftool", outcome="success", available=True, version="13.59"
        )

    monkeypatch.setattr(metadata, "_exiftool_adapter", successful_exiftool)
    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename="image.heic",
        claimed_mime="image/heic",
    )
    assert envelope["normalized"]["kind"] == "image"
    assert envelope["normalized"]["probe_status"] == "ok"
    assert envelope["extraction"]["adapters"]["image_header"]["outcome"] == (
        "not_applicable"
    )
    assert not any(
        item.get("source") == "image_header" for item in envelope["warnings"]
    )


def test_corrupt_bounded_image_header_is_a_reusable_terminal_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x89PNG\r\n\x1a\n"
    path = tmp_path / "truncated.png"
    path.write_bytes(payload)
    monkeypatch.setattr(
        metadata,
        "_exiftool_adapter",
        lambda *_args, **_kwargs: metadata._AdapterResult(
            name="exiftool", outcome="success", available=True, version="13.59"
        ),
    )

    envelope = metadata.extract_media_metadata(
        path,
        digest=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        filename=None,
        claimed_mime=None,
    )

    assert envelope["normalized"]["probe_status"] == "partial"
    assert envelope["extraction"]["adapters"]["image_header"]["outcome"] == ("corrupt")
    assert envelope["extraction"]["completeness"] == "terminal_partial"
    assert metadata.media_metadata_cache_compatible(
        metadata.cache_entry_from_envelope(envelope)
    )


def test_webp_header_dimensions_cover_lossy_and_lossless_bitstreams() -> None:
    lossy_payload = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", 320, 240)
    lossy = (
        b"RIFF"
        + struct.pack("<I", len(lossy_payload) + 12)
        + b"WEBPVP8 "
        + struct.pack("<I", len(lossy_payload))
        + lossy_payload
    )
    assert metadata.image_dimensions(lossy) == (320, 240)

    width, height = 641, 359
    lossless_bits = (width - 1) | ((height - 1) << 14)
    lossless_payload = b"\x2f" + lossless_bits.to_bytes(4, "little")
    lossless = (
        b"RIFF"
        + struct.pack("<I", len(lossless_payload) + 12)
        + b"WEBPVP8L"
        + struct.pack("<I", len(lossless_payload))
        + lossless_payload
    )
    assert metadata.image_dimensions(lossless) == (width, height)


def test_cache_cas_preserves_acquisition_metadata_and_uses_generation_guard(
    tmp_path: Path, monkeypatch
) -> None:
    payload = _png_header(5, 4)
    project = Project.create(tmp_path / "cache.frisket", name="cache")
    try:
        digest = project.add_blob(
            payload,
            filename="acquired.png",
            mime="image/png",
            metadata=owned_media_metadata_document(
                acquisition={"title": "Acquisition title", "duration_seconds": 91},
                owner={"source_id": "upstream-123"},
            ),
        )
        materialized = tmp_path / "materialized"
        materialized.write_bytes(payload)
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
        envelope = metadata.extract_media_metadata(
            materialized,
            digest=digest,
            size_bytes=len(payload),
            filename=None,
            claimed_mime=None,
        )
        proposal = metadata.cache_entry_from_envelope(envelope)
        store = MediaBlobStore(project)

        invalid_hash = copy.deepcopy(proposal)
        invalid_hash["content_facts_hash"] = "sha256:not-a-digest"
        with pytest.raises(ValueError, match="sha256 content_facts_hash"):
            store.compare_and_swap_media_metadata_cache(
                digest, invalid_hash, expected_generation=0
            )

        first = store.compare_and_swap_media_metadata_cache(
            digest, proposal, expected_generation=0
        )
        assert first.written is True
        assert first.entry is not None
        assert first.entry["generation"] == 1
        document = store.metadata(digest)
        assert document[MEDIA_ACQUISITION_NAMESPACE] == {
            "title": "Acquisition title",
            "duration_seconds": 91,
        }
        assert document["source_id"] == "upstream-123"
        assert document[MEDIA_METADATA_CACHE_NAMESPACE]["generation"] == 1

        detached = store.media_metadata_cache(digest)
        assert detached is not None
        detached["generation"] = 999
        assert store.media_metadata_cache(digest)["generation"] == 1

        degraded = copy.deepcopy(proposal)
        degraded["content_facts"]["envelope"]["extraction"]["completeness"] = (
            "retryable_failure"
        )
        degraded["content_facts"]["envelope"]["extraction"]["retryable"] = True
        _rehash_cache_entry(degraded)
        loser = store.compare_and_swap_media_metadata_cache(
            digest, degraded, expected_generation=1
        )
        assert loser.written is False
        assert loser.reason == "not_reusable"
        assert loser.entry["generation"] == 1

        stale_equal = store.compare_and_swap_media_metadata_cache(
            digest, proposal, expected_generation=0
        )
        assert stale_equal.written is False
        assert stale_equal.reason == "generation_mismatch"

        current = store.compare_and_swap_media_metadata_cache(
            digest, proposal, expected_generation=1
        )
        assert current.written is True
        assert current.entry["generation"] == 2
        assert store.acquisition_metadata(digest)["title"] == "Acquisition title"
    finally:
        project.close()

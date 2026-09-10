from __future__ import annotations

import importlib
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from frisket.ops.ytdlp import (
    YTDLP_EXTRA_OPTS_ALLOWED_KEYS,
    validate_extra_opts,
)

MODULE = "frisket.plugins.media"


# ---------------------------------------------------------------------------
# Defensive product-API accessor (frozen names the implementer must supply).
# ---------------------------------------------------------------------------


def _api() -> Any:
    """Resolve the not-yet-existing media-plugin seam module + its frozen
    exports, raising a semantic AssertionError when absent so a missing product
    seam is reported as an ordinary test failure."""
    assert find_spec(MODULE) is not None, (
        f"missing {MODULE}: the media acquisition seams must provide "
        "out-of-process media acquisition, the ``extra_opts`` mapping seam, "
        "and file-based host handoff"
    )
    module = importlib.import_module(MODULE)
    required = [
        "MediaExtraOptsPlan",
        "plan_extra_opts_for_runtime",
        "UnmappableExtraOpt",
        "MediaHandoff",
        "run_media_download",
        "ingest_media_handoff",
    ]
    missing = [name for name in required if not hasattr(module, name)]
    assert not missing, f"{MODULE} lacks frozen exports: {missing}"
    return module


# ---------------------------------------------------------------------------
# Injected offline media extractor that writes files instead of doing a real
# network fetch.
# ---------------------------------------------------------------------------


# Runtime receipt evidence the media handoff must carry.
_RUNTIME_EVIDENCE = {
    "runtime_name": "yt-dlp",
    "resolved_version": "2099.12.31",
    "artifact_sha256": "a" * 64,
}

_MEDIA_BYTES = b"\x00\x01fake-audio-bytes\x02\x03"
_SUBS_BYTES = b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n"


def _fake_extractor(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    """Stand in for the yt-dlp subprocess extractor: write
    the media file + a subtitle sidecar into the child's work dir and return the
    metadata the host records. Models "the transport", not the feature."""
    work_dir = Path(work_dir)
    media = work_dir / "video123.m4a"
    media.write_bytes(_MEDIA_BYTES)
    subs = work_dir / "video123.en.vtt"
    subs.write_bytes(_SUBS_BYTES)
    return {
        "media_filename": media.name,
        "sidecar_filenames": [subs.name],
        "metadata": {
            "yt_dlp_id": "video123",
            "title": "Fake video",
            "duration_seconds": 1.0,
        },
        "runtime_evidence": dict(_RUNTIME_EVIDENCE),
    }


class _RecordingProject:
    """A fake host Project that records blob ingests. The CHILD must never touch
    one of these; only the host-side ingest seam does."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add_blob(
        self, data: bytes, *, filename: str, mime: str = "", source_url: str = ""
    ) -> str:
        self.added.append({"filename": filename, "size": len(data)})
        return f"sha256:{len(self.added)}"


def _download_request() -> dict[str, Any]:
    return {
        "url": "https://www.youtube.com/watch?v=video123",
        "media_type": "audio",
        "extra_opts": {"writesubtitles": True, "subtitleslangs": ["en"]},
    }


# ---------------------------------------------------------------------------
# 1. Media analysis remains core after removal of the dormant bundled plugin.
# ---------------------------------------------------------------------------


def test_media_analysis_is_core_only_without_bundled_plugin():
    from frisket.actions.system import root_action_catalog

    bundled_plugin = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "frisket"
        / "authoring"
        / "bundled_plugins"
        / "frisket.media"
    )
    assert not (bundled_plugin / "plugin.json").exists()
    assert not (bundled_plugin / "plugin.py").exists()

    action_kinds = {entry.kind for entry in root_action_catalog().actions}
    assert {
        "media.extract_metadata",
        "media.video_frames",
        "media.extract_faces",
    } <= action_kinds
    assert {
        "frisket.media.extract_metadata",
        "frisket.media.video_frames",
        "frisket.media.extract_faces",
    }.isdisjoint(action_kinds)


# ---------------------------------------------------------------------------
# 2. The public ``extra_opts`` schema survives unchanged.
# ---------------------------------------------------------------------------


def test_public_extra_opts_schema_is_reused_unchanged():
    module = _api()
    # The mapping seam must reuse the EXISTING public allowlist + choke point,
    # never fork a second one.
    plan = module.plan_extra_opts_for_runtime(
        {"writesubtitles": True, "subtitleslangs": ["en"]}
    )
    assert isinstance(plan, module.MediaExtraOptsPlan)
    # A disallowed key is still rejected by the same validate_extra_opts contract.
    with pytest.raises(ValueError):
        module.plan_extra_opts_for_runtime({"exec_cmd": "rm -rf /"})
    # Sanity: the public choke point itself is unchanged and still rejects it.
    with pytest.raises(ValueError):
        validate_extra_opts({"exec_cmd": "rm -rf /"})


@pytest.mark.parametrize("key", sorted(YTDLP_EXTRA_OPTS_ALLOWED_KEYS))
def test_every_allowed_extra_opt_key_is_mapped_deterministically(key):
    module = _api()
    # A representative in-bounds value per key (bounds from
    # frisket.ytdlp.YTDLP_EXTRA_OPTS_NUMERIC_BOUNDS; scalars/lists otherwise).
    sample_values: dict[str, Any] = {
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "merge_output_format": "mp4",
        "format_sort": ["res"],
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "sleep_interval": 1,
        "max_sleep_interval": 2,
        "sleep_interval_requests": 1,
        "sleep_interval_subtitles": 1,
        "ratelimit": 1_000_000,
        "throttledratelimit": 1_000_000,
    }
    value = sample_values.get(key, True)
    extra_opts = {key: value}

    plan = module.plan_extra_opts_for_runtime(extra_opts)
    # No silent drop: every provided allowed key is accounted for by the plan.
    assert key in set(plan.covered_keys), (
        f"allowed extra_opts key {key!r} was dropped by the out-of-process "
        "mapping — every allowed key must be deterministically represented, or "
        "raise UnmappableExtraOpt; silently dropping an option is forbidden"
    )
    # Deterministic: same input twice yields the same coverage.
    again = module.plan_extra_opts_for_runtime(dict(extra_opts))
    assert set(again.covered_keys) == set(plan.covered_keys)


def test_unmappable_key_stop_channel_exists():
    module = _api()
    # The STOP-and-report channel must be a real, named error the seam can raise
    # instead of silently dropping an allowed-but-unrepresentable key.
    assert isinstance(module.UnmappableExtraOpt, type)
    assert issubclass(module.UnmappableExtraOpt, Exception)


# ---------------------------------------------------------------------------
# 4. File-based media handoff: relative paths + metadata, no bytes.
# ---------------------------------------------------------------------------


def test_child_writes_files_and_returns_relative_paths_only(tmp_path):
    module = _api()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    handoff = module.run_media_download(
        _download_request(), scratch_dir=scratch, extractor=_fake_extractor
    )
    assert isinstance(handoff, module.MediaHandoff)

    # The media path is RELATIVE and resolves under the host-provided scratch dir.
    media_rel = Path(handoff.media_relpath)
    assert not media_rel.is_absolute(), "handoff must return a relative media path"
    media_abs = scratch / media_rel
    assert media_abs.is_file(), "the child must WRITE the media into the scratch dir"
    # The child seam does not compare the media against an artificial byte
    # ceiling; it only requires that a media
    # file was actually written. See test_child_accepts_media_larger_than_the_
    # former_cap below for the positive proof that a large download is not gated.
    assert media_abs.stat().st_size > 0

    # Sidecars are relative paths under the same scratch dir.
    for rel in handoff.sidecar_relpaths:
        rel_path = Path(rel)
        assert not rel_path.is_absolute()
        assert (scratch / rel_path).is_file()


def test_child_accepts_media_larger_than_the_former_cap(tmp_path):
    # The former ceiling was 512 MiB (frisket.ytdlp.MAX_YTDLP_BYTES, now deleted).
    # A download whose media file exceeds that former ceiling must now succeed
    # instead of raising "media download exceeded the media size limit". The
    # child seam only stats the file (never reads its bytes), so a sparse file
    # of 600 MiB logical size proves the absence of the gate without allocating
    # real bytes.
    module = _api()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    former_cap = 512 * 1024 * 1024

    def _oversize_extractor(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
        media = Path(work_dir) / "video123.m4a"
        with media.open("wb") as fh:
            fh.truncate(former_cap + 100 * 1024 * 1024)  # ~600 MiB logical
        return {
            "media_filename": media.name,
            "sidecar_filenames": [],
            "metadata": {"yt_dlp_id": "video123"},
            "runtime_evidence": dict(_RUNTIME_EVIDENCE),
        }

    handoff = module.run_media_download(
        _download_request(), scratch_dir=scratch, extractor=_oversize_extractor
    )
    media_abs = scratch / Path(handoff.media_relpath)
    assert media_abs.stat().st_size > former_cap, (
        "the fixture must actually exceed the former cap for this to prove anything"
    )
    # No RuntimeError was raised: the oversize download produced a valid handoff.
    assert isinstance(handoff, module.MediaHandoff)


def test_host_ingests_scratch_files_into_blob_store(tmp_path):
    module = _api()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    handoff = module.run_media_download(
        _download_request(), scratch_dir=scratch, extractor=_fake_extractor
    )

    project = _RecordingProject()
    module.ingest_media_handoff(project, handoff, scratch_dir=scratch)
    # The host reads the relpaths from scratch and adds the blobs (media + at
    # least the one sidecar the fake extractor wrote).
    assert len(project.added) >= 2, (
        "the host must ingest the media + its sidecars from the scratch dir"
    )
    total = sum(entry["size"] for entry in project.added)
    assert total == len(_MEDIA_BYTES) + len(_SUBS_BYTES)


# ---------------------------------------------------------------------------
# 5. Receipts carry the runtime's content-hash evidence.
# ---------------------------------------------------------------------------


def test_handoff_carries_managed_runtime_receipt_evidence(tmp_path):
    module = _api()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    handoff = module.run_media_download(
        _download_request(), scratch_dir=scratch, extractor=_fake_extractor
    )
    evidence = handoff.runtime_evidence
    assert set(evidence) >= {"runtime_name", "resolved_version", "artifact_sha256"}
    assert evidence["runtime_name"] == "yt-dlp"
    assert evidence["artifact_sha256"] == _RUNTIME_EVIDENCE["artifact_sha256"], (
        "the receipt evidence must stamp the resolved CONTENT hash of the managed "
        "yt-dlp runtime, not only a version string"
    )

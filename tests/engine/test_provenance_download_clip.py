from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact, record_source_span
from frisket.engine.store.media_clip import (
    MAX_CLIP_DURATION_MS,
    ClipError,
    ClipSource,
    clip_filename,
    padded_range,
)


def _wav_bytes(seconds: float, *, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buf.getvalue()


def _client_and_project(tmp_path: Path) -> tuple[TestClient, str, Path]:
    ws = tmp_path / "ws"
    client = TestClient(create_app(ws))
    project_id = client.post("/api/projects", json={"name": "Clip"}).json()["id"]
    return client, project_id, ws


def _open_project(ws: Path, project_id: str) -> Project:
    return Project(ws / f"{project_id}.frisket")


def _seed_temporal_span(
    project: Project,
    *,
    artifact_kind: str = "av",
    blob_hash: str,
    mime: str = "audio/wav",
    filename: str = "hearing.wav",
    duration_ms: int | None = 5000,
    span_kind: str = "temporal",
    start_ms: int | None = 1000,
    end_ms: int | None = 2000,
) -> str:
    artifact = record_source_artifact(
        project,
        artifact_kind=artifact_kind,
        media_type=mime,
        blob_hash=blob_hash,
        filename=filename,
        duration_ms=duration_ms,
    )
    span_kwargs: dict = {"span_kind": span_kind}
    if span_kind == "temporal":
        span_kwargs["start_ms"] = start_ms
        span_kwargs["end_ms"] = end_ms
    else:
        span_kwargs["page_start"] = 1
        span_kwargs["page_end"] = 1
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        quote="excerpt",
        **span_kwargs,
    )
    project.db.commit()
    return str(span["stable_id"])


# ---------------------------------------------------------------------------
# Real ffmpeg happy path (via the actual HTTP route)
# ---------------------------------------------------------------------------


def test_clip_endpoint_streams_a_real_ffmpeg_cut_with_default_padding(
    tmp_path: Path,
) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(5.0), filename="hearing.wav", mime="audio/wav"
    )
    span_id = _seed_temporal_span(
        project, blob_hash=blob_hash, duration_ms=5000, start_ms=1000, end_ms=2000
    )
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/spans/{span_id}/clip")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/wav"
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    # default pad_ms=500 either side of [1000,2000] -> [500,2500], "0m00s-0m02s"
    assert "hearing_0m00s-0m02s.wav" in disposition

    with wave.open(io.BytesIO(response.content)) as clipped:
        clipped_seconds = clipped.getnframes() / clipped.getframerate()
    assert abs(clipped_seconds - 2.0) < 0.1


def test_clip_endpoint_honors_pad_ms_query_param(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(5.0), filename="hearing.wav", mime="audio/wav"
    )
    span_id = _seed_temporal_span(
        project, blob_hash=blob_hash, duration_ms=5000, start_ms=2000, end_ms=2500
    )
    project.close()

    response = client.get(
        f"/api/projects/{pid}/evidence/spans/{span_id}/clip", params={"pad_ms": 0}
    )

    assert response.status_code == 200, response.text
    with wave.open(io.BytesIO(response.content)) as clipped:
        clipped_seconds = clipped.getnframes() / clipped.getframerate()
    assert abs(clipped_seconds - 0.5) < 0.1


# ---------------------------------------------------------------------------
# Guard: span_kind == "temporal" and artifact_kind == "av"
# ---------------------------------------------------------------------------


def test_clip_guard_rejects_non_temporal_span(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(2.0), filename="hearing.wav", mime="audio/wav"
    )
    span_id = _seed_temporal_span(project, blob_hash=blob_hash, span_kind="page_range")
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/spans/{span_id}/clip")

    assert response.status_code == 400
    assert response.json()["code"] == "not_temporal"


def test_clip_guard_rejects_non_av_artifact(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        b"%PDF-1.4 fake", filename="hearing.pdf", mime="application/pdf"
    )
    span_id = _seed_temporal_span(
        project,
        artifact_kind="file",
        blob_hash=blob_hash,
        mime="application/pdf",
        filename="hearing.pdf",
        duration_ms=None,
    )
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/spans/{span_id}/clip")

    assert response.status_code == 400
    assert response.json()["code"] == "not_av"


def test_clip_guard_404s_unknown_span(tmp_path: Path) -> None:
    client, pid, _ws = _client_and_project(tmp_path)

    response = client.get(f"/api/projects/{pid}/evidence/spans/does-not-exist/clip")

    assert response.status_code == 404
    assert response.json()["code"] == "span_not_found"


def test_clip_guard_404s_when_blob_missing_from_disk(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    # A blob_hash the artifact references but that was never actually written
    # to the blob store (not "locally resolvable") -- the exact failure mode
    # v1 must fail closed on, not silently serve garbage.
    span_id = _seed_temporal_span(project, blob_hash="deadbeef" * 8)
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/spans/{span_id}/clip")

    assert response.status_code == 404
    assert response.json()["code"] == "blob_missing"


def test_clip_rejects_over_cap_duration_instead_of_truncating(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(1.0), filename="hearing.wav", mime="audio/wav"
    )
    span_id = _seed_temporal_span(
        project,
        blob_hash=blob_hash,
        duration_ms=None,
        start_ms=0,
        end_ms=MAX_CLIP_DURATION_MS + 60_000,
    )
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/spans/{span_id}/clip")

    assert response.status_code == 400
    assert response.json()["code"] == "clip_too_long"


# ---------------------------------------------------------------------------
# Pure-function unit coverage: padding/cap math + filename composition
# ---------------------------------------------------------------------------


def _source(**overrides) -> ClipSource:
    base = dict(
        span_stable_id="span-1",
        blob_hash="hash-1",
        mime="audio/wav",
        filename="hearing.wav",
        artifact_duration_ms=5000,
        start_ms=1000,
        end_ms=2000,
        label="hearing.wav",
    )
    base.update(overrides)
    return ClipSource(**base)


def test_padded_range_applies_padding_and_clamps_zero_floor() -> None:
    start, end = padded_range(_source(start_ms=200, end_ms=800), pad_ms=500)
    assert start == 0
    assert end == 1300


def test_padded_range_clamps_to_known_artifact_duration() -> None:
    start, end = padded_range(
        _source(start_ms=4800, end_ms=4900, artifact_duration_ms=5000), pad_ms=500
    )
    assert end == 5000


def test_padded_range_rejects_empty_range_after_duration_clamp() -> None:
    with pytest.raises(ClipError) as excinfo:
        padded_range(
            _source(start_ms=4990, end_ms=4995, artifact_duration_ms=4990), pad_ms=0
        )
    assert excinfo.value.code == "empty_range"


def test_padded_range_rejects_over_cap() -> None:
    with pytest.raises(ClipError) as excinfo:
        padded_range(
            _source(
                start_ms=0, end_ms=MAX_CLIP_DURATION_MS + 1, artifact_duration_ms=None
            ),
            pad_ms=0,
        )
    assert excinfo.value.code == "clip_too_long"


def test_clip_filename_sanitizes_label_and_formats_range() -> None:
    name = clip_filename(
        _source(
            label="Committee: Hearing #3!", filename="hearing.mp4", mime="video/mp4"
        ),
        start_ms=65_000,
        end_ms=125_000,
    )
    assert name == "Committee-Hearing-3_1m05s-2m05s.mp4"

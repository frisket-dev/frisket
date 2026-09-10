from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    CITATION_RUN_GAP_TOLERANCE_MS,
    citation_temporal_runs,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    resolve_evidence_viewer,
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
    project_id = client.post("/api/projects", json={"name": "Citation Runs"}).json()[
        "id"
    ]
    return client, project_id, ws


def _open_project(ws: Path, project_id: str) -> Project:
    return Project(ws / f"{project_id}.frisket")


def _seed_two_run_link(
    project: Project,
    *,
    blob_hash: str,
    duration_ms: int = 20_000,
    gap_ms: int = CITATION_RUN_GAP_TOLERANCE_MS + 1,
) -> dict[str, Any]:
    """One citation link contains two contiguous runs of
    spans on the SAME av artifact. Run 0: [1000,2000] + [2500,3500] (a
    500ms internal gap -- comfortably inside CITATION_RUN_GAP_TOLERANCE_MS,
    so they merge). Run 1: one span starting ``gap_ms`` after run 0 ends --
    disjoint by construction (``gap_ms`` defaults to just over tolerance)."""

    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="audio/wav",
        blob_hash=blob_hash,
        filename="hearing.wav",
        title="Hearing",
        duration_ms=duration_ms,
    )
    span_a = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=1000,
        end_ms=2000,
        quote="first thing",
    )
    span_b = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=2500,
        end_ms=3500,
        quote="second thing, same thought",
    )
    run1_start = 3500 + gap_ms
    span_c = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=run1_start,
        end_ms=run1_start + 1000,
        quote="a distinct, later thing",
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref={"kind": "run_result", "row_id": 1, "column_id": 1},
        spans=[
            {
                "span_id": span_a["id"],
                "rank": 0,
                "span_role": "support",
                "required": True,
            },
            {
                "span_id": span_b["id"],
                "rank": 0,
                "span_role": "support",
                "required": True,
            },
            {
                "span_id": span_c["id"],
                "rank": 1,
                "span_role": "support",
                "required": True,
            },
        ],
        producer={"source_action_kind": "test"},
    )
    project.db.commit()
    return {
        "link": link,
        "artifact": artifact,
        "span_a": span_a,
        "span_b": span_b,
        "span_c": span_c,
        "run1_start": run1_start,
    }


# --------------------------------------------------------------------------- #
# citation_temporal_runs: pure grouping + viewer payload wiring
# --------------------------------------------------------------------------- #


def test_citation_temporal_runs_merges_within_tolerance_and_splits_beyond_it(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    project = Project.create(ws / "runs.frisket", name="Runs")
    blob_hash = project.add_blob(
        _wav_bytes(20.0), filename="hearing.wav", mime="audio/wav"
    )
    seeded = _seed_two_run_link(project, blob_hash=blob_hash)

    runs = citation_temporal_runs(project, seeded["link"]["stable_id"])

    assert len(runs) == 2
    assert runs[0]["start_ms"] == 1000
    assert runs[0]["end_ms"] == 3500
    assert runs[0]["span_stable_ids"] == [
        seeded["span_a"]["stable_id"],
        seeded["span_b"]["stable_id"],
    ]
    assert runs[1]["start_ms"] == seeded["run1_start"]
    assert runs[1]["end_ms"] == seeded["run1_start"] + 1000
    assert runs[1]["span_stable_ids"] == [seeded["span_c"]["stable_id"]]
    project.close()


def test_citation_temporal_runs_gap_exactly_at_tolerance_still_merges(
    tmp_path: Path,
) -> None:
    """The gap check is <=, not <: a gap of EXACTLY the tolerance is still
    "same thought", matching the real data's wide (14,750ms) margin either
    side of the chosen 1500ms -- the boundary itself merges."""
    ws = tmp_path / "ws"
    project = Project.create(ws / "runs-boundary.frisket", name="Runs Boundary")
    blob_hash = project.add_blob(
        _wav_bytes(10.0), filename="hearing.wav", mime="audio/wav"
    )
    seeded = _seed_two_run_link(
        project, blob_hash=blob_hash, gap_ms=CITATION_RUN_GAP_TOLERANCE_MS
    )

    runs = citation_temporal_runs(project, seeded["link"]["stable_id"])

    assert len(runs) == 1
    assert runs[0]["start_ms"] == 1000
    assert runs[0]["end_ms"] == seeded["run1_start"] + 1000
    project.close()


def test_citation_temporal_runs_ignores_uncited_spans(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    project = Project.create(ws / "runs-uncited.frisket", name="Runs Uncited")
    blob_hash = project.add_blob(
        _wav_bytes(10.0), filename="hearing.wav", mime="audio/wav"
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="audio/wav",
        blob_hash=blob_hash,
        filename="hearing.wav",
        duration_ms=10_000,
    )
    cited = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=1000,
        end_ms=2000,
        quote="cited",
    )
    uncited = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="temporal",
        start_ms=2100,
        end_ms=2900,
        quote="context, not cited",
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref={"kind": "run_result", "row_id": 1, "column_id": 1},
        spans=[
            {
                "span_id": cited["id"],
                "rank": 0,
                "span_role": "support",
                "required": True,
            },
            {
                "span_id": uncited["id"],
                "rank": 1,
                "span_role": "support",
                "required": False,
            },
        ],
        producer={"source_action_kind": "test"},
    )
    project.db.commit()

    runs = citation_temporal_runs(project, link["stable_id"])

    assert len(runs) == 1
    assert runs[0]["span_stable_ids"] == [cited["stable_id"]]
    project.close()


def test_citation_temporal_runs_raises_keyerror_for_unknown_link(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    project = Project.create(ws / "runs-missing.frisket", name="Runs Missing")
    with pytest.raises(KeyError):
        citation_temporal_runs(project, "evidence_link:does-not-exist")
    project.close()


def test_viewer_payload_attaches_run_index_and_clip_urls(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    project = Project.create(ws / "runs-viewer.frisket", name="Runs Viewer")
    blob_hash = project.add_blob(
        _wav_bytes(20.0), filename="hearing.wav", mime="audio/wav"
    )
    seeded = _seed_two_run_link(project, blob_hash=blob_hash)

    viewer = resolve_evidence_viewer(
        project, seeded["link"]["stable_id"], project_id="proj"
    )
    artifact = viewer["artifacts"][0]
    by_id = {span["stable_id"]: span for span in artifact["spans"]}

    assert by_id[seeded["span_a"]["stable_id"]]["run_index"] == 0
    assert by_id[seeded["span_b"]["stable_id"]]["run_index"] == 0
    assert by_id[seeded["span_c"]["stable_id"]]["run_index"] == 1

    runs = artifact["runs"]
    assert len(runs) == 2
    link_stable_id = seeded["link"]["stable_id"]
    assert runs[0]["clip_url"] == (
        f"/api/projects/proj/evidence/links/{link_stable_id}/runs/0/clip"
    )
    assert runs[1]["clip_url"] == (
        f"/api/projects/proj/evidence/links/{link_stable_id}/runs/1/clip"
    )
    project.close()


# --------------------------------------------------------------------------- #
# Route: link-level run clip -- real ffmpeg cut, one per run
# --------------------------------------------------------------------------- #


def test_run_clip_endpoint_streams_a_real_ffmpeg_cut_per_run(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(20.0), filename="hearing.wav", mime="audio/wav"
    )
    seeded = _seed_two_run_link(project, blob_hash=blob_hash)
    link_id = seeded["link"]["stable_id"]
    project.close()

    run0 = client.get(f"/api/projects/{pid}/evidence/links/{link_id}/runs/0/clip")
    assert run0.status_code == 200, run0.text
    assert run0.headers["content-type"] == "audio/wav"
    assert "attachment" in run0.headers["content-disposition"]
    # run 0 spans [1000,3500]; default pad_ms=500 -> [500,4000] = 3.5s.
    with wave.open(io.BytesIO(run0.content)) as clipped:
        seconds0 = clipped.getnframes() / clipped.getframerate()
    assert abs(seconds0 - 3.5) < 0.1

    run1 = client.get(f"/api/projects/{pid}/evidence/links/{link_id}/runs/1/clip")
    assert run1.status_code == 200, run1.text
    # run 1 is a single 1000ms span; padded -> 2.0s.
    with wave.open(io.BytesIO(run1.content)) as clipped:
        seconds1 = clipped.getnframes() / clipped.getframerate()
    assert abs(seconds1 - 2.0) < 0.1

    # The two runs cut genuinely different audio, not the same range twice.
    assert run0.content != run1.content


def test_run_clip_endpoint_honors_pad_ms(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(20.0), filename="hearing.wav", mime="audio/wav"
    )
    seeded = _seed_two_run_link(project, blob_hash=blob_hash)
    link_id = seeded["link"]["stable_id"]
    project.close()

    response = client.get(
        f"/api/projects/{pid}/evidence/links/{link_id}/runs/1/clip",
        params={"pad_ms": 0},
    )
    assert response.status_code == 200, response.text
    with wave.open(io.BytesIO(response.content)) as clipped:
        seconds = clipped.getnframes() / clipped.getframerate()
    assert abs(seconds - 1.0) < 0.1


def test_run_clip_endpoint_404s_unknown_run_index(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    blob_hash = project.add_blob(
        _wav_bytes(20.0), filename="hearing.wav", mime="audio/wav"
    )
    seeded = _seed_two_run_link(project, blob_hash=blob_hash)
    link_id = seeded["link"]["stable_id"]
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/links/{link_id}/runs/2/clip")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_run_clip_endpoint_404s_unknown_link(tmp_path: Path) -> None:
    client, pid, _ws = _client_and_project(tmp_path)

    response = client.get(
        f"/api/projects/{pid}/evidence/links/evidence_link:does-not-exist/runs/0/clip"
    )

    assert response.status_code == 404
    assert response.json()["code"] == "evidence_link_not_found"


def test_run_clip_endpoint_404s_when_blob_missing_from_disk(tmp_path: Path) -> None:
    client, pid, ws = _client_and_project(tmp_path)
    project = _open_project(ws, pid)
    # A blob_hash the artifact references but that was never written to the
    # blob store -- not "locally resolvable", same failure mode the
    # per-span clip route fails closed on.
    seeded = _seed_two_run_link(project, blob_hash="deadbeef" * 8)
    link_id = seeded["link"]["stable_id"]
    project.close()

    response = client.get(f"/api/projects/{pid}/evidence/links/{link_id}/runs/0/clip")

    assert response.status_code == 404
    assert response.json()["code"] == "blob_missing"

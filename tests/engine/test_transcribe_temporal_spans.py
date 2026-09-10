from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.sdk.ops import transcribe_engines
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer


PROJECT_ID = "project-transcribe-temporal-spans"


def _transcribe_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    idempotency_key: str = "media_transcribe@sha256:temporal-spans",
) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "faster_whisper",
        },
        "idempotency_key": idempotency_key,
    }


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        )
    }


def _seed_project(tmp_path: Path) -> dict[str, Any]:
    project = Project.create(
        tmp_path / "transcribe-temporal-spans.frisket",
        name="Temporal spans",
    )
    sheet_id = project.add_sheet("Episodes")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }

    # Row A: two in-bounds segments, exercises rounding exactness.
    blob_a = project.add_blob(
        b"RIFF0000WAVEfmt in-bounds-a",
        filename="a.wav",
        mime="audio/wav",
        source_url="https://cdn.example/a.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 1.5, "kind": "audio"}
        ),
    )
    # Row B: one segment whose end overruns the artifact duration.
    blob_b = project.add_blob(
        b"RIFF0000WAVEfmt overrun-b",
        filename="b.wav",
        mime="audio/wav",
        source_url="https://cdn.example/b.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    # Row C: zero segments (e.g. VAD dropped everything as silence).
    blob_c = project.add_blob(
        b"RIFF0000WAVEfmt zero-c",
        filename="c.wav",
        mime="audio/wav",
        source_url="https://cdn.example/c.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 1.0, "kind": "audio"}
        ),
    )
    # Row D: one in-bounds segment, but blob has no duration metadata.
    blob_d = project.add_blob(
        b"RIFF0000WAVEfmt unknown-d",
        filename="d.wav",
        mime="audio/wav",
        source_url="https://cdn.example/d.wav",
        metadata=owned_media_metadata_document(probe={"kind": "audio"}),
    )

    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode A",
                "media": media_cell(blob_a, mime="audio/wav", filename="a.wav"),
            },
            {
                "title": "Episode B",
                "media": media_cell(blob_b, mime="audio/wav", filename="b.wav"),
            },
            {
                "title": "Episode C",
                "media": media_cell(blob_c, mime="audio/wav", filename="c.wav"),
            },
            {
                "title": "Episode D",
                "media": media_cell(blob_d, mime="audio/wav", filename="d.wav"),
            },
        ],
        cols,
    )
    return {
        "project": project,
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        "blobs": {"a": blob_a, "b": blob_b, "c": blob_c, "d": blob_d},
    }


_SEGMENTS_BY_BLOB = {
    "a": [
        {"start": 0.4999, "end": 1.0001, "text": "first segment"},
        {"start": 1.1, "end": 1.4, "text": "second segment"},
    ],
    "b": [{"start": 0.0, "end": 5.0, "text": "overrun segment"}],
    "c": [],
    "d": [{"start": 0.2, "end": 0.75, "text": "unknown duration segment"}],
}


def _fake_engine(blobs: dict[str, str]):
    by_digest = {digest: label for label, digest in blobs.items()}

    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, spec
        digest = Path(path).name
        label = by_digest[digest]
        segments = _SEGMENTS_BY_BLOB[label]
        text = " ".join(seg["text"] for seg in segments)
        return {
            "text": text,
            "segments": segments,
            "language": "en",
            "duration": segments[-1]["end"] if segments else 0.0,
        }

    return fake_faster_whisper


def _transcript_column_id(project: Project, sheet_id: int) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='transcript'",
        (sheet_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seeded = _seed_project(tmp_path)
    project: Project = seeded["project"]
    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter,
        "transcribe",
        _fake_engine(seeded["blobs"]),
    )
    before = _counts(project)
    result = run_action_spec(
        project,
        _transcribe_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]),
        project_id=PROJECT_ID,
    )
    assert result.status == "completed", result.errors
    seeded["before_counts"] = before
    seeded["result"] = result
    return seeded


def test_multi_segment_row_writes_temporal_spans_with_rounded_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_a = seeded["row_ids"][0]
    try:
        transcript_column_id = _transcript_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_a,
            column_id=transcript_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        link_stable_id = cell_evidence["links"][0]["stable_id"]

        viewer = resolve_evidence_viewer(project, link_stable_id, project_id=PROJECT_ID)
        assert len(viewer["artifacts"]) == 1
        artifact = viewer["artifacts"][0]
        assert artifact["artifact_kind"] == "av"
        assert artifact["media_type"] == "audio/wav"
        # duration_seconds=1.5 -> duration_ms=1500 (used to bound end_ms).
        assert artifact["duration_ms"] == 1500

        spans = artifact["spans"]
        assert len(spans) == 2
        first, second = spans
        assert first["span_kind"] == "temporal"
        assert first["quote"] == "first segment"
        # seconds -> ms rounding exactness: 0.4999*1000 -> 500, 1.0001*1000 -> 1000
        assert first["selector"]["start_ms"] == 500
        assert first["selector"]["end_ms"] == 1000
        assert first["selector"]["temporal"]["segment_index"] == 0
        assert not first["warnings"]

        assert second["span_kind"] == "temporal"
        assert second["quote"] == "second segment"
        assert second["selector"]["start_ms"] == 1100
        assert second["selector"]["end_ms"] == 1400
        assert second["selector"]["temporal"]["segment_index"] == 1
        assert not second["warnings"]
    finally:
        project.close()


def test_overrun_segment_clamps_to_artifact_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_b = seeded["row_ids"][1]
    try:
        transcript_column_id = _transcript_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_b,
            column_id=transcript_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        viewer = resolve_evidence_viewer(
            project, cell_evidence["links"][0]["stable_id"], project_id=PROJECT_ID
        )
        artifact = viewer["artifacts"][0]
        assert artifact["duration_ms"] == 2000
        span = artifact["spans"][0]
        assert span["selector"]["start_ms"] == 0
        # raw end_ms would be 5000 (5.0s); clamped to the artifact's duration_ms.
        assert span["selector"]["end_ms"] == 2000
        assert span["warnings"], "an overrunning segment must record honestly"
    finally:
        project.close()


def test_zero_segment_row_writes_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_c = seeded["row_ids"][2]
    try:
        transcript_column_id = _transcript_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_c,
            column_id=transcript_column_id,
            project_id=PROJECT_ID,
        )
        assert cell_evidence["links"] == []
        no_artifact = project.db.execute(
            "SELECT COUNT(*) FROM source_artifacts WHERE source_row_id=?",
            (row_c,),
        ).fetchone()[0]
        assert no_artifact == 0
    finally:
        project.close()


def test_unknown_duration_row_writes_spans_without_clamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    sheet_id = seeded["sheet_id"]
    row_d = seeded["row_ids"][3]
    try:
        transcript_column_id = _transcript_column_id(project, sheet_id)
        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_d,
            column_id=transcript_column_id,
            project_id=PROJECT_ID,
        )
        assert len(cell_evidence["links"]) == 1
        viewer = resolve_evidence_viewer(
            project, cell_evidence["links"][0]["stable_id"], project_id=PROJECT_ID
        )
        artifact = viewer["artifacts"][0]
        assert artifact["duration_ms"] is None
        span = artifact["spans"][0]
        assert span["selector"]["start_ms"] == 200
        assert span["selector"]["end_ms"] == 750
        assert not span["warnings"]
    finally:
        project.close()


def test_evidence_counts_match_multi_row_expectations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _run(tmp_path, monkeypatch)
    project: Project = seeded["project"]
    try:
        after = _counts(project)
        before = seeded["before_counts"]
        # 3 rows write evidence (A, B, D); C (zero segments) writes nothing.
        assert after["source_artifacts"] == before["source_artifacts"] + 3
        assert after["source_spans"] == before["source_spans"] + 4  # A:2 + B:1 + D:1
        assert after["evidence_links"] == before["evidence_links"] + 3
        assert after["evidence_link_spans"] == before["evidence_link_spans"] + 4
    finally:
        project.close()


def test_replay_does_not_duplicate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_project(tmp_path)
    project: Project = seeded["project"]
    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter,
        "transcribe",
        _fake_engine(seeded["blobs"]),
    )
    try:
        action = _transcribe_action(
            sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"]
        )
        first = run_action_spec(project, action, project_id=PROJECT_ID)
        assert first.status == "completed"
        after_first = _counts(project)

        replay = run_action_spec(project, action, project_id=PROJECT_ID)
        assert replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert _counts(project) == after_first
    finally:
        project.close()

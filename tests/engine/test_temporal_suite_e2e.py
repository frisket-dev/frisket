from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from frisket.actions.core import RegisteredAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.temporal_extract import EXTRACT_RANGE
from frisket.actions.types import ActionRequest
from frisket.engine.executor.temporal_extract_action import (
    run_typed_temporal_extract_action,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import TimelineLease, resolve_timeline
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_span,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real ffmpeg/ffprobe not installed",
)


@dataclass(frozen=True)
class _SeededMedia:
    sheet_id: int
    row_id: int
    media_column_id: int
    transcript_column_id: int
    blob_hash: str
    lease: TimelineLease
    source_span_stable_ids: tuple[str, ...]
    source_evidence_link_stable_id: str


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _write_three_scene_video(path: Path) -> None:
    """Create three one-second hard-cut scenes plus continuous mono audio."""

    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x90:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "3:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )


def _probe(path: Path) -> dict[str, Any]:
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(completed.stdout)


def _assert_av_clip(project: Project, blob_hash: str, *, duration_ms: int) -> None:
    with project.materialize_blob(blob_hash) as materialized:
        probe = _probe(Path(materialized))
    assert {stream["codec_type"] for stream in probe["streams"]} == {
        "audio",
        "video",
    }
    assert abs(float(probe["format"]["duration"]) * 1000 - duration_ms) <= 120


def _seed_project(project: Project, source_path: Path) -> _SeededMedia:
    sheet_id = project.add_sheet("Media")
    media_column_id = project.add_column(sheet_id, "video", "video")
    transcript_column_id = project.add_column(
        sheet_id,
        "transcript",
        "timestamped_transcript",
        ai_generated=True,
    )
    blob_hash = project.add_blob_from_path(
        source_path,
        filename="three-scenes.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 3.0}),
    )
    transcript_text = "Red scene. Green scene. Blue scene."
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": "three-scenes.mp4",
                    "mime": "video/mp4",
                },
            }
        ],
        {"video": media_column_id},
    )[0]
    transcript_op_id = project.append_op(
        "media.transcribe",
        {"kind": "media.transcribe", "params": {"output_name": "transcript"}},
        label="transcribe transcript",
    )
    runs = RunResultStore(project)
    transcript_run_id = runs.start_run(
        transcript_op_id,
        sheet_id,
        "media.transcribe",
        params={"output_name": "transcript"},
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        transcript_run_id,
        [
            {
                "row_id": row_id,
                "column_id": transcript_column_id,
                "value": transcript_text,
            }
        ],
    )
    runs.finish_run(transcript_run_id)
    runs.point_column_at_run(
        transcript_op_id,
        transcript_column_id,
        transcript_run_id,
    )

    # Resolving the cell persists the canonical immutable presentation clock.
    lease = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=media_column_id,
    )
    assert lease.anchor.duration_ms == 3_000
    assert lease.anchor.fingerprint.startswith("sha256:")

    span_refs: list[dict[str, int]] = []
    span_stable_ids: list[str] = []
    for index, (start_ms, end_ms, quote) in enumerate(
        [
            (0, 1_000, "Red scene."),
            (1_000, 2_000, "Green scene."),
            (2_000, 3_000, "Blue scene."),
        ]
    ):
        span = record_source_span(
            project,
            artifact_id=lease.anchor.artifact_id,
            span_kind="temporal",
            start_ms=start_ms,
            end_ms=end_ms,
            quote=quote,
            selector={"segment_index": index},
        )
        span_refs.append({"span_id": int(span["id"]), "rank": index})
        span_stable_ids.append(str(span["stable_id"]))

    _values, refs = project.get_values_with_refs(
        sheet_id,
        transcript_column_id,
        row_ids=[row_id],
    )
    evidence_link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=span_refs,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=transcript_column_id,
        run_id=transcript_run_id,
        op_id=transcript_op_id,
        link_role="media_transcribe_temporal",
        producer={"action_kind": "media.transcribe"},
        metadata={
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
        },
    )
    return _SeededMedia(
        sheet_id=sheet_id,
        row_id=row_id,
        media_column_id=media_column_id,
        transcript_column_id=transcript_column_id,
        blob_hash=blob_hash,
        lease=lease,
        source_span_stable_ids=tuple(span_stable_ids),
        source_evidence_link_stable_id=str(evidence_link["stable_id"]),
    )


def _value(project: Project, sheet_id: int, row_id: int, column_id: int) -> Any:
    return project.get_values(sheet_id, column_id, row_ids=[row_id])[row_id]


def _column_id(project: Project, sheet_id: int, name: str) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, name),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _assert_projected_evidence(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    transcript_column_id: int,
    derived_artifact_id: int,
    expected_source_span_stable_id: str,
    expected_source_link_stable_id: str,
) -> None:
    link = project.db.execute(
        "SELECT * FROM evidence_links WHERE sheet_id=? AND row_id=? "
        "AND column_id=? AND link_role='temporal_transcript_projection' "
        "AND status='active'",
        (sheet_id, row_id, transcript_column_id),
    ).fetchone()
    assert link is not None
    metadata = json.loads(link["metadata"])
    assert metadata["schema_version"] == (
        TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION
    )
    assert metadata["semantic_type"] == "timestamped_transcript"
    assert metadata["source_evidence_link_stable_id"] == expected_source_link_stable_id
    spans = project.db.execute(
        "SELECT sp.* FROM evidence_link_spans els "
        "JOIN source_spans sp ON sp.id=els.span_id WHERE els.link_id=? "
        "ORDER BY els.rank",
        (int(link["id"]),),
    ).fetchall()
    assert len(spans) == 1
    assert int(spans[0]["artifact_id"]) == derived_artifact_id
    projection = json.loads(spans[0]["metadata"])["projection"]
    assert projection["parent_span_stable_id"] == expected_source_span_stable_id
    assert projection["evidence_link_stable_id"] == expected_source_link_stable_id


def test_extract_range_real_media_inherits_timeline_and_transcript(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "three-scenes.mp4"
    _write_three_scene_video(source_path)
    project = Project.create(tmp_path / "temporal-extract-e2e.frisket")
    try:
        seeded = _seed_project(project, source_path)
        request = ActionRequest.model_validate(
            {
                "action_id": "temporal.extract_range",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": seeded.sheet_id,
                    "row_ids": [seeded.row_id],
                },
                "params": {
                    "source": "video",
                    "selection": {
                        "kind": "draft_range",
                        "start_ms": 1_000,
                        "end_ms": 2_000,
                    },
                },
                "idempotency_key": "temporal-extract-real-e2e@sha256:v1",
            }
        )
        result = run_typed_temporal_extract_action(
            project,
            "temporal-extract-real-e2e",
            BoundTypedActionRequest.bind(
                RegisteredAction("temporal.extract_range", EXTRACT_RANGE), request
            ),
        )

        assert result.status == "completed", result.model_dump(mode="json")
        by_name = {output.name: output for output in result.outputs}
        assert set(by_name) == {
            "clip",
            "clip_transcript",
        }
        clip = by_name["clip"]
        clip_value = _value(
            project, seeded.sheet_id, seeded.row_id, int(clip.column_id)
        )
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        lineage = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "derived_temporal_artifact"
        )
        assert clip_value["blob"] == lineage["blob_hash"]
        _assert_av_clip(project, clip_value["blob"], duration_ms=1_000)

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM blob_derivations "
                "WHERE derived_hash=? AND source_hash=? "
                "AND op='ffmpeg_exact_temporal_splice'",
                (clip_value["blob"], seeded.blob_hash),
            ).fetchone()[0]
            == 1
        )
        derived_lease = resolve_timeline(
            project,
            sheet_id=seeded.sheet_id,
            row_id=seeded.row_id,
            column_id=int(clip.column_id),
        )
        mapping = project.db.execute(
            "SELECT * FROM artifact_timeline_segments WHERE derived_artifact_id=?",
            (derived_lease.anchor.artifact_id,),
        ).fetchone()
        assert mapping is not None
        assert int(mapping["source_artifact_id"]) == seeded.lease.anchor.artifact_id
        assert (
            int(mapping["derived_start_ms"]),
            int(mapping["derived_end_ms"]),
            int(mapping["source_start_ms"]),
            int(mapping["source_end_ms"]),
            int(mapping["rate_num"]),
            int(mapping["rate_den"]),
        ) == (0, 1_000, 1_000, 2_000, 1, 1)

        transcript = by_name["clip_transcript"]
        transcript_type = project.db.execute(
            "SELECT type FROM columns WHERE id=?", (transcript.column_id,)
        ).fetchone()
        assert transcript_type["type"] == "timestamped_transcript"
        assert (
            _value(
                project,
                seeded.sheet_id,
                seeded.row_id,
                int(transcript.column_id),
            )
            == "Green scene."
        )
        resolved_transcript = resolve_timestamped_transcript(
            project,
            sheet_id=seeded.sheet_id,
            row_id=seeded.row_id,
            column_id=int(transcript.column_id),
        )
        assert resolved_transcript is not None
        assert [
            (span["start_ms"], span["end_ms"], span["quote"])
            for span in resolved_transcript.spans
        ] == [(0, 1_000, "Green scene.")]
        _assert_projected_evidence(
            project,
            sheet_id=seeded.sheet_id,
            row_id=seeded.row_id,
            transcript_column_id=int(transcript.column_id),
            derived_artifact_id=derived_lease.anchor.artifact_id,
            expected_source_span_stable_id=seeded.source_span_stable_ids[1],
            expected_source_link_stable_id=(seeded.source_evidence_link_stable_id),
        )

        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None and receipt.status == "completed"
        assert {output.name for output in receipt.outputs} == set(by_name)
        receipt_range = next(
            item.ref["value"]["item"]
            for item in receipt.inputs
            if item.name == "selection"
        )
        assert re.fullmatch(r"tr_[0-9a-f]{64}", receipt_range["id"])
        assert {
            "start_ms": receipt_range["start_ms"],
            "end_ms": receipt_range["end_ms"],
            "metadata": receipt_range["metadata"],
        } == {"start_ms": 1_000, "end_ms": 2_000, "metadata": {}}
        assert any(
            evidence.ref.get("kind") == "temporal_transcript_projection"
            for evidence in receipt.evidence
        )
    finally:
        project.close()


def test_split_real_media_creates_child_rows_with_inherited_provenance(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "three-scenes.mp4"
    _write_three_scene_video(source_path)
    project = Project.create(tmp_path / "temporal-split-e2e.frisket")
    try:
        seeded = _seed_project(project, source_path)
        request = ActionRequest.model_validate(
            {
                "action_id": "derive.temporal_segments",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": seeded.sheet_id,
                    "row_ids": [seeded.row_id],
                },
                "params": {
                    "source": "video",
                    "selection": {
                        "kind": "draft_points",
                        "items": [{"at_ms": 1_000}, {"at_ms": 2_000}],
                    },
                },
                "sheet_name": "Video segments",
                "idempotency_key": "temporal-split-real-e2e@sha256:v1",
            }
        )
        result = run_action_spec(
            project,
            request.model_dump(mode="json"),
            project_id="temporal-split-real-e2e",
        )

        assert result.status == "completed", result.model_dump(mode="json")
        sheet_output = next(
            output for output in result.outputs if output.kind == "sheet"
        )
        rows_output = next(output for output in result.outputs if output.kind == "rows")
        child_sheet_id = int(sheet_output.sheet_id)
        child_row_ids = list(rows_output.row_ids)
        assert len(child_row_ids) == 3
        child_rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows WHERE sheet_id=? "
            "AND hidden=0 ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["id"]) for row in child_rows] == child_row_ids
        assert [int(row["parent_row_id"]) for row in child_rows] == [
            seeded.row_id,
            seeded.row_id,
            seeded.row_id,
        ]

        clip_column_id = _column_id(project, child_sheet_id, "clip")
        range_column_id = _column_id(project, child_sheet_id, "source_range")
        transcript_column_id = _column_id(project, child_sheet_id, "transcript")
        column_types = {
            str(row["name"]): str(row["type"])
            for row in project.columns(child_sheet_id)
        }
        assert column_types == {
            "clip": "video",
            "source_range": "timeline_range",
            "transcript": "timestamped_transcript",
        }

        expected_ranges = [(0, 1_000), (1_000, 2_000), (2_000, 3_000)]
        expected_text = ["Red scene.", "Green scene.", "Blue scene."]
        for index, (child_row_id, (start_ms, end_ms)) in enumerate(
            zip(child_row_ids, expected_ranges, strict=True)
        ):
            clip_value = _value(project, child_sheet_id, child_row_id, clip_column_id)
            _assert_av_clip(project, clip_value["blob"], duration_ms=1_000)
            source_range = _value(
                project, child_sheet_id, child_row_id, range_column_id
            )
            assert source_range["schema_version"] == "frisket.timeline_range.v1"
            assert source_range["timeline"] == seeded.lease.anchor.wire_value()
            assert (
                source_range["item"]["start_ms"],
                source_range["item"]["end_ms"],
            ) == (start_ms, end_ms)

            transcript_text = _value(
                project, child_sheet_id, child_row_id, transcript_column_id
            )
            assert transcript_text == expected_text[index]
            resolved_transcript = resolve_timestamped_transcript(
                project,
                sheet_id=child_sheet_id,
                row_id=child_row_id,
                column_id=transcript_column_id,
            )
            assert resolved_transcript is not None
            assert [
                (span["start_ms"], span["end_ms"], span["quote"])
                for span in resolved_transcript.spans
            ] == [(0, 1_000, expected_text[index])]

            derived_lease = resolve_timeline(
                project,
                sheet_id=child_sheet_id,
                row_id=child_row_id,
                column_id=clip_column_id,
            )
            mapping = project.db.execute(
                "SELECT * FROM artifact_timeline_segments WHERE derived_artifact_id=?",
                (derived_lease.anchor.artifact_id,),
            ).fetchone()
            assert mapping is not None
            assert int(mapping["source_artifact_id"]) == (
                seeded.lease.anchor.artifact_id
            )
            assert (
                int(mapping["derived_start_ms"]),
                int(mapping["derived_end_ms"]),
                int(mapping["source_start_ms"]),
                int(mapping["source_end_ms"]),
                int(mapping["rate_num"]),
                int(mapping["rate_den"]),
            ) == (0, 1_000, start_ms, end_ms, 1, 1)
            _assert_projected_evidence(
                project,
                sheet_id=child_sheet_id,
                row_id=child_row_id,
                transcript_column_id=transcript_column_id,
                derived_artifact_id=derived_lease.anchor.artifact_id,
                expected_source_span_stable_id=(seeded.source_span_stable_ids[index]),
                expected_source_link_stable_id=(seeded.source_evidence_link_stable_id),
            )

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM blob_derivations WHERE source_hash=? "
                "AND op='ffmpeg_exact_temporal_splice'",
                (seeded.blob_hash,),
            ).fetchone()[0]
            == 3
        )
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None and receipt.status == "completed"
        assert {output.name for output in receipt.outputs} == {
            "Video segments",
            "column.clip",
            "column.source_range",
            "column.transcript",
            "materialized_rows",
        }
        evidence_kinds = [evidence.ref.get("kind") for evidence in receipt.evidence]
        assert evidence_kinds.count("temporal_materialized_clip") == 3
        assert evidence_kinds.count("derived_temporal_artifact") == 3
        assert evidence_kinds.count("temporal_transcript_projection") == 3
        assert evidence_kinds.count("materialized_row_sources") == 1
        assert evidence_kinds.count("lineage_parent_rows") == 1
    finally:
        project.close()


def test_split_stage_failure_publishes_no_partial_children_or_lineage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_path = tmp_path / "three-scenes.mp4"
    _write_three_scene_video(source_path)
    project = Project.create(tmp_path / "temporal-split-atomic.frisket")
    try:
        seeded = _seed_project(project, source_path)
        request = ActionRequest.model_validate(
            {
                "action_id": "derive.temporal_segments",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": seeded.sheet_id,
                    "row_ids": [seeded.row_id],
                },
                "params": {
                    "source": "video",
                    "selection": {
                        "kind": "draft_points",
                        "items": [{"at_ms": 1_000}, {"at_ms": 2_000}],
                    },
                },
                "sheet_name": "Failed segments",
                "idempotency_key": "temporal-split-atomic-e2e@sha256:v1",
            }
        )
        from frisket.engine.executor import temporal_split

        original_stage = temporal_split.stage_media_splice
        stage_count = 0

        class _InjectedStageFailure(RuntimeError):
            code = "injected_stage_failure"
            message = "second staged clip failed"
            details = {"ordinal": 1}

        async def stage_then_fail(*args, **kwargs) -> Any:
            nonlocal stage_count
            stage_count += 1
            if stage_count == 2:
                raise _InjectedStageFailure
            return await original_stage(*args, **kwargs)

        before = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "sheets",
                "columns",
                "rows",
                "ops",
                "blobs",
                "blob_derivations",
                "source_artifacts",
                "source_spans",
                "evidence_links",
                "artifact_timeline_segments",
            )
        }
        monkeypatch.setattr(temporal_split, "stage_media_splice", stage_then_fail)
        result = run_action_spec(
            project,
            request.model_dump(mode="json"),
            project_id="temporal-split-atomic-e2e",
        )

        assert stage_count == 2, result
        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["project_write_failed"]
        after = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in before
        }
        assert after == before
        assert (
            project.db.execute(
                "SELECT 1 FROM sheets WHERE name='Failed segments'"
            ).fetchone()
            is None
        )
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        # Dynamic table preparation fails before a receipt is reserved.
        assert receipt is None
        assert result.outputs == []
    finally:
        project.close()

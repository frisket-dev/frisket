from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import TimelineError, resolve_timeline
from frisket.features.temporal_ingress import validate_temporal_persistence_value


PROJECT_ID = "project-temporal-persistence-ingress"


def _cell_edit_action(
    *,
    row_id: int,
    column_id: int,
    value: Any,
    key: str,
) -> dict[str, Any]:
    return {
        "action_id": "cell.edit",
        "scope": {"kind": "project"},
        "params": {
            "edits": [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": value,
                }
            ]
        },
        "idempotency_key": key,
    }


def _cell_edit_query_action(
    *,
    sheet_id: int,
    column_id: int,
    value: Any,
    key: str,
) -> dict[str, Any]:
    return {
        "action_id": "cell.edit_query",
        "scope": {"kind": "project"},
        "params": {
            "query": {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet_id},
                "filter": {"label": {"eq": "first"}},
            },
            "column_id": column_id,
            "value": value,
        },
        "idempotency_key": key,
    }


def _column_set_type_action(
    *, column_id: int, sheet_id: int, type_name: str, key: str
) -> dict[str, Any]:
    return {
        "action_id": "column.set_type",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "column_id": column_id,
            "type": type_name,
        },
        "idempotency_key": key,
    }


def _range_value(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "frisket.timeline_range.v1",
        "timeline": anchor,
        "item": {"id": "range-1", "start_ms": 1_000, "end_ms": 2_000},
    }


def _write_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts")
    }


def _seed_project(tmp_path: Path, name: str = "temporal-ingress") -> dict[str, Any]:
    project = Project.create(tmp_path / f"{name}.frisket")
    sheet_id = project.add_sheet("Media")
    columns = {
        "label": project.add_column(sheet_id, "label", "text"),
        "media": project.add_column(sheet_id, "media", "video"),
        "selection": project.add_column(sheet_id, "selection", "timeline_range"),
    }
    first_blob = project.add_blob(
        b"first-video",
        "first.mp4",
        "video/mp4",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 10.0, "kind": "video"}
        ),
    )
    second_blob = project.add_blob(
        b"second-video",
        "second.mp4",
        "video/mp4",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 12.0, "kind": "video"}
        ),
    )
    rows = project.add_rows(
        sheet_id,
        [
            {
                "label": "first",
                "media": media_cell(
                    first_blob,
                    mime="video/mp4",
                    filename="first.mp4",
                ),
            },
            {
                "label": "second",
                "media": media_cell(
                    second_blob,
                    mime="video/mp4",
                    filename="second.mp4",
                ),
            },
        ],
        columns,
    )
    lease = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=rows[0],
        column_id=columns["media"],
    )
    return {
        "project": project,
        "sheet_id": sheet_id,
        "columns": columns,
        "rows": rows,
        "lease": lease,
    }


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        (
            "timeline_point",
            {
                "schema_version": "frisket.timeline_point.v1",
                "item": {"id": "p-1", "at_ms": 1_000},
            },
        ),
        (
            "timeline_points",
            {
                "schema_version": "frisket.timeline_points.v1",
                "items": [{"id": "p-1", "at_ms": 1_000}],
            },
        ),
        (
            "timeline_range",
            {
                "schema_version": "frisket.timeline_range.v1",
                "item": {"id": "r-1", "start_ms": 1_000, "end_ms": 2_000},
            },
        ),
        (
            "timeline_ranges",
            {
                "schema_version": "frisket.timeline_ranges.v1",
                "items": [{"id": "r-1", "start_ms": 1_000, "end_ms": 2_000}],
            },
        ),
    ],
)
def test_host_validator_parses_all_temporal_types(
    tmp_path: Path, type_name: str, value: dict[str, Any]
) -> None:
    seeded = _seed_project(tmp_path, f"validate-{type_name}")
    project = seeded["project"]
    try:
        value["timeline"] = seeded["lease"].anchor.wire_value()
        normalized = validate_temporal_persistence_value(
            project, type_name=type_name, value=value
        )
        assert normalized is not None
        assert normalized["timeline"] == seeded["lease"].anchor.wire_value()
    finally:
        project.close()


def test_host_validator_allows_omitted_duration_for_resolvable_anchor(
    tmp_path: Path,
) -> None:
    seeded = _seed_project(tmp_path, "omitted-duration")
    project = seeded["project"]
    try:
        anchor = seeded["lease"].anchor.wire_value()
        anchor.pop("duration_ms")
        normalized = validate_temporal_persistence_value(
            project,
            type_name="timeline_point",
            value={
                "schema_version": "frisket.timeline_point.v1",
                "timeline": anchor,
                "item": {"id": "point-1", "at_ms": 1_000},
            },
        )
        assert normalized is not None
        assert normalized["timeline"]["duration_ms"] is None
        assert normalized["timeline"]["fingerprint"] == (
            seeded["lease"].anchor.fingerprint
        )

        with pytest.raises(TimelineError) as exc:
            validate_temporal_persistence_value(
                project,
                type_name="timeline_point",
                value={
                    "schema_version": "frisket.timeline_point.v1",
                    "timeline": anchor,
                    "item": {"id": "point-too-late", "at_ms": 10_001},
                },
            )
        assert exc.value.code == "range_out_of_bounds"
    finally:
        project.close()


def test_cell_edit_and_query_reject_missing_and_forged_anchors(
    tmp_path: Path,
) -> None:
    seeded = _seed_project(tmp_path)
    project = seeded["project"]
    try:
        anchor = seeded["lease"].anchor.wire_value()
        missing = _range_value(
            {
                **anchor,
                "artifact_stable_id": (
                    "source_artifact:00000000-0000-4000-8000-000000000001"
                ),
            }
        )
        missing_result = run_action_spec(
            project,
            _cell_edit_action(
                row_id=seeded["rows"][0],
                column_id=seeded["columns"]["selection"],
                value=missing,
                key="temporal-missing@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert missing_result.status == "failed"
        assert missing_result.errors[0].code == "timeline_not_found"

        forged = _range_value({**anchor, "fingerprint": "sha256:" + "0" * 64})
        forged_result = run_action_spec(
            project,
            _cell_edit_query_action(
                sheet_id=seeded["sheet_id"],
                column_id=seeded["columns"]["selection"],
                value=forged,
                key="temporal-forged-query@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert forged_result.status == "failed"
        assert forged_result.errors[0].code == "timeline_stale"
        assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    finally:
        project.close()


def test_import_rows_rejects_missing_temporal_anchor_before_any_write(
    tmp_path: Path,
) -> None:
    seeded = _seed_project(tmp_path, "import-rows-missing-anchor")
    project = seeded["project"]
    try:
        missing_anchor = {
            **seeded["lease"].anchor.wire_value(),
            "artifact_stable_id": (
                "source_artifact:00000000-0000-4000-8000-000000000011"
            ),
        }
        before = _write_counts(project)
        result = run_action_spec(
            project,
            {
                "action_id": "import.rows",
                "scope": {"kind": "project"},
                "sheet_name": "Forged selections",
                "params": {
                    "columns": [{"name": "selection", "type": "timeline_range"}],
                    "rows": [{"selection": _range_value(missing_anchor)}],
                    "source": {"kind": "inline", "label": "adversarial fixture"},
                },
                "idempotency_key": "temporal-import-missing@sha256:v1",
            },
            project_id=PROJECT_ID,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "timeline_not_found"
        assert _write_counts(project) == before
    finally:
        project.close()


def test_row_add_rejects_forged_temporal_anchor_before_any_write(
    tmp_path: Path,
) -> None:
    seeded = _seed_project(tmp_path, "row-add-forged-anchor")
    project = seeded["project"]
    try:
        forged = _range_value(
            {
                **seeded["lease"].anchor.wire_value(),
                "fingerprint": "sha256:" + "0" * 64,
            }
        )
        before = _write_counts(project)
        result = run_action_spec(
            project,
            {
                "action_id": "row.add",
                "scope": {"kind": "project"},
                "params": {
                    "sheet_id": seeded["sheet_id"],
                    "cells": {"selection": forged},
                },
                "idempotency_key": "temporal-row-add-forged@sha256:v1",
            },
            project_id=PROJECT_ID,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "timeline_stale"
        assert _write_counts(project) == before
    finally:
        project.close()


def test_cell_edit_rejects_anchor_when_persisted_identity_has_gone_stale(
    tmp_path: Path,
) -> None:
    seeded = _seed_project(tmp_path, "stale-anchor")
    project = seeded["project"]
    try:
        value = _range_value(seeded["lease"].anchor.wire_value())
        project.db.execute(
            "UPDATE source_artifacts SET duration_ms=? WHERE id=?",
            (11_000, seeded["lease"].anchor.artifact_id),
        )
        project.db.commit()

        result = run_action_spec(
            project,
            _cell_edit_action(
                row_id=seeded["rows"][0],
                column_id=seeded["columns"]["selection"],
                value=value,
                key="temporal-stale@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "failed"
        assert result.errors[0].code == "timeline_stale"
    finally:
        project.close()


def test_copying_temporal_cell_preserves_original_anchor(tmp_path: Path) -> None:
    seeded = _seed_project(tmp_path, "copy-anchor")
    project = seeded["project"]
    try:
        value = _range_value(seeded["lease"].anchor.wire_value())
        first = run_action_spec(
            project,
            _cell_edit_action(
                row_id=seeded["rows"][0],
                column_id=seeded["columns"]["selection"],
                value=value,
                key="temporal-copy-source@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed"
        stored = project.get_values(
            seeded["sheet_id"],
            seeded["columns"]["selection"],
            row_ids=[seeded["rows"][0]],
        )[seeded["rows"][0]]

        copied = run_action_spec(
            project,
            _cell_edit_action(
                row_id=seeded["rows"][1],
                column_id=seeded["columns"]["selection"],
                value=stored,
                key="temporal-copy-destination@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert copied.status == "completed"
        destination = project.get_values(
            seeded["sheet_id"],
            seeded["columns"]["selection"],
            row_ids=[seeded["rows"][1]],
        )[seeded["rows"][1]]
        assert destination["timeline"] == seeded["lease"].anchor.wire_value()
        assert destination == stored
    finally:
        project.close()


def test_column_retype_contextually_validates_existing_temporal_values(
    tmp_path: Path,
) -> None:
    seeded = _seed_project(tmp_path, "retype-anchor")
    project = seeded["project"]
    try:
        valid_column = project.add_column(seeded["sheet_id"], "valid raw", "json")
        invalid_column = project.add_column(seeded["sheet_id"], "invalid raw", "json")
        valid = _range_value(seeded["lease"].anchor.wire_value())
        missing = _range_value(
            {
                **seeded["lease"].anchor.wire_value(),
                "artifact_stable_id": (
                    "source_artifact:00000000-0000-4000-8000-000000000002"
                ),
            }
        )
        project.add_rows(
            seeded["sheet_id"],
            [{"valid raw": valid, "invalid raw": missing}],
            {"valid raw": valid_column, "invalid raw": invalid_column},
        )

        valid_result = run_action_spec(
            project,
            _column_set_type_action(
                column_id=valid_column,
                sheet_id=seeded["sheet_id"],
                type_name="timeline_range",
                key="temporal-retype-valid@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert valid_result.status == "completed"

        invalid_result = run_action_spec(
            project,
            _column_set_type_action(
                column_id=invalid_column,
                sheet_id=seeded["sheet_id"],
                type_name="timeline_range",
                key="temporal-retype-invalid@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert invalid_result.status == "failed"
        assert invalid_result.errors[0].code == "timeline_not_found"
        assert (
            project.db.execute(
                "SELECT type FROM columns WHERE id=?", (invalid_column,)
            ).fetchone()["type"]
            == "json"
        )
    finally:
        project.close()


def _transcribe_action(sheet_id: int, row_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "faster_whisper",
        },
        "idempotency_key": key,
    }


def test_transcribe_reuses_previously_resolved_media_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "transcribe-timeline-reuse.frisket")
    sheet_id = project.add_sheet("Media")
    media_column = project.add_column(sheet_id, "media", "audio")
    blob_hash = project.add_blob(
        b"RIFF timeline reuse",
        "source.wav",
        "audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 0.5, "kind": "audio"}
        ),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "media": media_cell(
                    blob_hash,
                    mime="audio/wav",
                    filename="source.wav",
                )
            }
        ],
        {"media": media_column},
    )[0]
    lease = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=media_column,
    )

    async def fake_transcribe(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, path, spec
        return {
            "text": "hello",
            "segments": [{"start": 0.0, "end": 0.5, "text": "hello"}],
            "language": "en",
            "duration": 0.5,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_transcribe
    )
    try:
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id,
                row_id,
                key="transcribe-timeline-reuse@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"
        artifacts = project.db.execute(
            "SELECT * FROM source_artifacts WHERE source_sheet_id=? "
            "AND source_row_id=? AND source_column_id=? AND artifact_kind='av'",
            (sheet_id, row_id, media_column),
        ).fetchall()
        assert len(artifacts) == 1
        assert int(artifacts[0]["id"]) == lease.anchor.artifact_id
        span_artifact_ids = {
            int(row["artifact_id"])
            for row in project.db.execute("SELECT artifact_id FROM source_spans")
        }
        assert span_artifact_ids == {lease.anchor.artifact_id}
    finally:
        project.close()


def test_transcribe_preserves_duration_unknown_legacy_artifact_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.create(tmp_path / "transcribe-unknown-duration.frisket")
    sheet_id = project.add_sheet("Media")
    media_column = project.add_column(sheet_id, "media", "audio")
    blob_hash = project.add_blob(b"RIFF unknown duration", "source.wav", "audio/wav")
    row_id = project.add_rows(
        sheet_id,
        [{"media": media_cell(blob_hash, mime="audio/wav", filename="source.wav")}],
        {"media": media_column},
    )[0]

    async def fake_transcribe(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, path, spec
        return {
            "text": "hello",
            "segments": [{"start": 0.0, "end": 0.5, "text": "hello"}],
            "language": "en",
            "duration": 0.5,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_transcribe
    )
    try:
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id,
                row_id,
                key="transcribe-unknown-duration@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed"
        artifact = project.db.execute(
            "SELECT duration_ms, metadata FROM source_artifacts "
            "WHERE source_sheet_id=? AND source_row_id=? AND source_column_id=?",
            (sheet_id, row_id, media_column),
        ).fetchone()
        assert artifact is not None
        assert artifact["duration_ms"] is None
        assert '"timeline"' not in str(artifact["metadata"])
    finally:
        project.close()

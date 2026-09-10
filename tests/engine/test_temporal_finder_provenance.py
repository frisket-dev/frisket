from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor.temporal_finder_provenance import (
    ResolvedTopicAnalysisSidecar,
    resolve_topic_analysis_receipt_ref,
    resolve_topic_analysis_sidecar,
    resolve_topic_section_unit_ids,
)
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store import Project
from frisket.engine.store.current_cells import refresh_current_cells
from frisket.engine.store.runs import RunResultStore
from frisket.features.temporal_values import canonical_temporal_hash
from frisket.features.topic_segmentation.contracts import (
    TOPIC_ANALYSIS_SIDECAR_COLUMN,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
)
from helpers import write_claimed_test_results


def _timeline(*, fingerprint_char: str = "a") -> dict[str, Any]:
    return {
        "artifact_stable_id": "source_artifact:video-1",
        "fingerprint": "sha256:" + fingerprint_char * 64,
        "duration_ms": 4_000,
    }


def _ranges(
    *,
    timeline: dict[str, Any] | None = None,
    split_at_ms: int = 2_000,
) -> dict[str, Any]:
    return {
        "schema_version": "frisket.timeline_ranges.v1",
        "timeline": timeline or _timeline(),
        "items": [
            {
                "id": "topic-1",
                "start_ms": 0,
                "end_ms": split_at_ms,
                "label": "Opening",
                "metadata": {},
            },
            {
                "id": "topic-2",
                "start_ms": split_at_ms,
                "end_ms": 4_000,
                "label": "Follow-up",
                "metadata": {},
            },
        ],
    }


def _sidecar(
    *,
    timeline: dict[str, Any] | None = None,
    evidence_id: str = "evidence_link:topic-1",
) -> dict[str, Any]:
    timeline = timeline or _timeline()
    return {
        "schema_version": TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
        "transcript_evidence_id": evidence_id,
        "transcript_snapshot_hash": "sha256:" + "b" * 64,
        "transcript_run_id": 17,
        "transcript_column_id": 23,
        "transcript_value_ref": {
            "kind": "run_result",
            "run_id": 17,
            "row_id": 1,
            "column_id": 23,
        },
        "transcript_value_hash": "sha256:" + "c" * 64,
        "artifact_stable_id": timeline["artifact_stable_id"],
        "timeline": timeline,
        "language": "en",
        "engine_id": "fixture-engine",
        "engine_version": "1",
        "resolved_settings": {"detail": "balanced"},
        "engine_diagnostics": {"fixture": True},
        "native_candidates": [],
        "locking": {"policy": "inclusive-overlap"},
    }


@dataclass(frozen=True)
class _SeededTopicRun:
    project: Project
    sheet_id: int
    row_ids: tuple[int, ...]
    selection_column_id: int
    sidecar_column_id: int
    run_id: int
    payload: dict[str, Any]


def _start_run(
    project: Project,
    *,
    sheet_id: int,
    row_ids: list[int],
    action_kind: str = "map.find_topic_sections",
) -> tuple[int, int, RunResultStore]:
    op_id = project.append_op(
        action_kind,
        {"action_kind": action_kind},
        label=action_kind,
    )
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id,
        sheet_id,
        action_kind,
        total_rows=len(row_ids),
        row_ids=row_ids,
    )
    return op_id, run_id, store


def _seed_completed_topic_run(
    tmp_path: Path,
    *,
    row_count: int = 1,
) -> _SeededTopicRun:
    project = Project.create(tmp_path / "topic-provenance.frisket", name="topic")
    sheet_id = project.add_sheet("Interviews")
    source_column_id = project.add_column(sheet_id, "Source", "text")
    selection_column_id = project.add_column(
        sheet_id,
        "Topic sections",
        "timeline_ranges",
        ai_generated=True,
    )
    sidecar_column_id = project.add_column(
        sheet_id,
        TOPIC_ANALYSIS_SIDECAR_COLUMN,
        "json",
        ai_generated=True,
        hidden=True,
    )
    row_ids = tuple(
        project.add_rows(
            sheet_id,
            [{"Source": f"Interview {index}"} for index in range(row_count)],
            {"Source": source_column_id},
        )
    )
    payload = _sidecar()
    op_id, run_id, store = _start_run(
        project,
        sheet_id=sheet_id,
        row_ids=list(row_ids),
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": selection_column_id,
                "value": _ranges(),
                "outcome": "ok",
            }
            for row_id in row_ids
        ]
        + [
            {
                "row_id": row_id,
                "column_id": sidecar_column_id,
                "value": payload,
                "outcome": "ok",
            }
            for row_id in row_ids
        ],
    )
    store.finish_run(run_id)
    store.point_column_at_run(op_id, selection_column_id, run_id)
    store.point_column_at_run(op_id, sidecar_column_id, run_id)
    return _SeededTopicRun(
        project=project,
        sheet_id=sheet_id,
        row_ids=row_ids,
        selection_column_id=selection_column_id,
        sidecar_column_id=sidecar_column_id,
        run_id=run_id,
        payload=payload,
    )


def _resolve(seed: _SeededTopicRun, row_id: int | None = None):
    return resolve_topic_analysis_sidecar(
        seed.project,
        sheet_id=seed.sheet_id,
        row_id=row_id if row_id is not None else seed.row_ids[0],
        selection_column_id=seed.selection_column_id,
    )


def test_locked_unit_ids_require_the_exact_generated_ranges() -> None:
    value = _ranges()
    payload = _sidecar()
    payload["locking"] = {
        "timeline_ranges_hash": canonical_temporal_hash("timeline_ranges", value),
        "section_unit_ids": {
            "topic-1": [],
            "topic-2": ["source_span:b", "source_span:c"],
        },
    }
    sidecar = ResolvedTopicAnalysisSidecar(
        sheet_id=1,
        row_id=1,
        selection_column_id=2,
        run_id=3,
        sidecar_column_id=4,
        payload=payload,
    )

    assert resolve_topic_section_unit_ids(sidecar, value) == {
        "topic-1": frozenset(),
        "topic-2": frozenset({"source_span:b", "source_span:c"}),
    }
    assert resolve_topic_section_unit_ids(sidecar, _ranges(split_at_ms=2_500)) is None


def test_resolves_exact_completed_topic_run(tmp_path: Path) -> None:
    seed = _seed_completed_topic_run(tmp_path)

    resolved = _resolve(seed)

    assert resolved is not None
    assert resolved.sheet_id == seed.sheet_id
    assert resolved.row_id == seed.row_ids[0]
    assert resolved.selection_column_id == seed.selection_column_id
    assert resolved.sidecar_column_id == seed.sidecar_column_id
    assert resolved.run_id == seed.run_id
    assert resolved.payload == seed.payload
    receipt_ref = resolved.receipt_ref()
    assert receipt_ref == {
        "kind": "topic_analysis_snapshot",
        "sheet_id": seed.sheet_id,
        "row_id": seed.row_ids[0],
        "selection_column_id": seed.selection_column_id,
        "run_id": seed.run_id,
        "sidecar_column_id": seed.sidecar_column_id,
        "payload_hash": canonical_json_hash(seed.payload),
        "transcript_evidence_id": seed.payload["transcript_evidence_id"],
        "transcript_snapshot_hash": seed.payload["transcript_snapshot_hash"],
    }
    assert resolve_topic_analysis_receipt_ref(seed.project, receipt_ref) == resolved
    assert (
        resolve_topic_analysis_receipt_ref(
            seed.project,
            {**receipt_ref, "payload_hash": "sha256:" + "0" * 64},
        )
        is None
    )


def test_full_successor_resolves_from_the_exact_cell_head(tmp_path: Path) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    row_id = seed.row_ids[0]
    later_payload = _sidecar(evidence_id="evidence_link:successor")
    _op_id, later_run_id, store = _start_run(
        seed.project,
        sheet_id=seed.sheet_id,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        seed.project,
        later_run_id,
        [
            {
                "row_id": row_id,
                "column_id": seed.selection_column_id,
                "value": _ranges(split_at_ms=3_000),
                "outcome": "ok",
            },
            {
                "row_id": row_id,
                "column_id": seed.sidecar_column_id,
                "value": later_payload,
                "outcome": "ok",
            },
        ],
    )
    store.finish_run(later_run_id)

    assert (
        seed.project.get_column(seed.selection_column_id)["current_run_id"]
        == seed.run_id
    )
    resolved = _resolve(seed)
    assert resolved is not None
    assert resolved.run_id == later_run_id
    assert resolved.payload == later_payload


def test_manual_edit_overlay_with_same_timeline_remains_attributable(
    tmp_path: Path,
) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    row_id = seed.row_ids[0]
    edited_value = _ranges(split_at_ms=2_500)
    seed.project.apply_edits(
        [
            {
                "row_id": row_id,
                "column_id": seed.selection_column_id,
                "value": edited_value,
            }
        ]
    )

    live_values, live_refs = seed.project.get_values_with_refs(
        seed.sheet_id,
        seed.selection_column_id,
        row_ids=[row_id],
    )
    assert live_values[row_id] == edited_value
    assert live_refs[row_id]["kind"] == "manual_edit"

    resolved = _resolve(seed)
    assert resolved is not None
    assert resolved.run_id == seed.run_id
    assert resolved.payload == seed.payload


def test_hidden_column_repoint_does_not_change_exact_old_run_resolution(
    tmp_path: Path,
) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    row_id = seed.row_ids[0]
    later_payload = _sidecar(evidence_id="evidence_link:topic-2")
    later_op_id, later_run_id, store = _start_run(
        seed.project,
        sheet_id=seed.sheet_id,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        seed.project,
        later_run_id,
        [
            {
                "row_id": row_id,
                "column_id": seed.sidecar_column_id,
                "value": later_payload,
                "outcome": "ok",
            }
        ],
    )
    store.finish_run(later_run_id)
    store.point_column_at_run(later_op_id, seed.sidecar_column_id, later_run_id)

    assert (
        seed.project.get_column(seed.sidecar_column_id)["current_run_id"]
        == later_run_id
    )
    assert (
        seed.project.get_column(seed.selection_column_id)["current_run_id"]
        == seed.run_id
    )

    resolved = _resolve(seed)
    assert resolved is not None
    assert resolved.run_id == seed.run_id
    assert resolved.payload == seed.payload
    assert resolved.payload != later_payload


def test_later_topic_run_never_claims_an_older_manual_edit(tmp_path: Path) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    row_id = seed.row_ids[0]
    seed.project.apply_edits(
        [
            {
                "row_id": row_id,
                "column_id": seed.selection_column_id,
                "value": _ranges(split_at_ms=2_500),
            }
        ]
    )
    assert _resolve(seed) is not None  # edited A still resolves to A

    later_payload = _sidecar(evidence_id="evidence_link:topic-2")
    later_op_id, later_run_id, store = _start_run(
        seed.project,
        sheet_id=seed.sheet_id,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        seed.project,
        later_run_id,
        [
            {
                "row_id": row_id,
                "column_id": seed.selection_column_id,
                "value": _ranges(split_at_ms=3_000),
                "outcome": "ok",
            },
            {
                "row_id": row_id,
                "column_id": seed.sidecar_column_id,
                "value": later_payload,
                "outcome": "ok",
            },
        ],
    )
    store.finish_run(later_run_id)
    store.point_column_at_run(later_op_id, seed.selection_column_id, later_run_id)
    store.point_column_at_run(later_op_id, seed.sidecar_column_id, later_run_id)

    live_values, live_refs = seed.project.get_values_with_refs(
        seed.sheet_id,
        seed.selection_column_id,
        row_ids=[row_id],
    )
    assert live_values[row_id] == _ranges(split_at_ms=2_500)
    assert live_refs[row_id]["kind"] == "manual_edit"
    assert _resolve(seed) is None

    edit_op_id = int(live_refs[row_id]["op_id"])
    seed.project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (edit_op_id,))
    # This test intentionally simulates undoing the overlay through its
    # operation status; keep the derived visible-cell projection in sync.
    refresh_current_cells(
        seed.project.db,
        column_ids={seed.selection_column_id},
        row_ids={row_id},
    )
    seed.project.db.commit()
    resolved_b = _resolve(seed)
    assert resolved_b is not None
    assert resolved_b.run_id == later_run_id
    assert resolved_b.payload == later_payload


def test_copied_manual_or_missing_base_value_is_not_attributable(
    tmp_path: Path,
) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    project = seed.project
    row_id = seed.row_ids[0]

    manual_column_id = project.add_column(
        seed.sheet_id,
        "Manual sections",
        "timeline_ranges",
    )
    project.apply_edits(
        [
            {
                "row_id": row_id,
                "column_id": manual_column_id,
                "value": _ranges(),
            }
        ]
    )
    assert (
        resolve_topic_analysis_sidecar(
            project,
            sheet_id=seed.sheet_id,
            row_id=row_id,
            selection_column_id=manual_column_id,
        )
        is None
    )

    copied_column_id = project.add_column(
        seed.sheet_id,
        "Copied sections",
        "timeline_ranges",
        ai_generated=True,
    )
    copy_op_id, copy_run_id, store = _start_run(
        project,
        sheet_id=seed.sheet_id,
        row_ids=[row_id],
        action_kind="map.copy",
    )
    write_claimed_test_results(
        project,
        copy_run_id,
        [
            {
                "row_id": row_id,
                "column_id": copied_column_id,
                "value": _ranges(),
                "outcome": "ok",
            }
        ],
    )
    store.finish_run(copy_run_id)
    store.point_column_at_run(copy_op_id, copied_column_id, copy_run_id)
    assert (
        resolve_topic_analysis_sidecar(
            project,
            sheet_id=seed.sheet_id,
            row_id=row_id,
            selection_column_id=copied_column_id,
        )
        is None
    )

    no_base_column_id = project.add_column(
        seed.sheet_id,
        "Missing result sections",
        "timeline_ranges",
        ai_generated=True,
    )
    no_base_op_id, no_base_run_id, store = _start_run(
        project,
        sheet_id=seed.sheet_id,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        no_base_run_id,
        [
            {
                "row_id": row_id,
                "column_id": seed.sidecar_column_id,
                "value": _sidecar(),
                "outcome": "ok",
            }
        ],
    )
    store.finish_run(no_base_run_id)
    store.point_column_at_run(no_base_op_id, no_base_column_id, no_base_run_id)
    assert (
        resolve_topic_analysis_sidecar(
            project,
            sheet_id=seed.sheet_id,
            row_id=row_id,
            selection_column_id=no_base_column_id,
        )
        is None
    )


def test_manual_edit_that_changes_timeline_is_not_attributable(tmp_path: Path) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    row_id = seed.row_ids[0]
    seed.project.apply_edits(
        [
            {
                "row_id": row_id,
                "column_id": seed.selection_column_id,
                "value": _ranges(timeline=_timeline(fingerprint_char="d")),
            }
        ]
    )

    assert _resolve(seed) is None


def test_sidecar_without_a_producing_transcript_run_is_rejected(tmp_path: Path) -> None:
    seed = _seed_completed_topic_run(tmp_path)
    invalid_payload = {**seed.payload, "transcript_run_id": None}
    with pytest.raises(
        sqlite3.IntegrityError, match="published result semantics are immutable"
    ):
        seed.project.db.execute(
            "UPDATE results SET value=? WHERE run_id=? AND row_id=? AND column_id=?",
            (
                json.dumps(invalid_payload),
                seed.run_id,
                seed.row_ids[0],
                seed.sidecar_column_id,
            ),
        )
    seed.project.db.rollback()
    assert _resolve(seed) is not None


def test_completed_partial_run_resolves_only_rows_with_successful_sidecars(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "partial-topic.frisket", name="partial")
    sheet_id = project.add_sheet("Interviews")
    source_column_id = project.add_column(sheet_id, "Source", "text")
    selection_column_id = project.add_column(
        sheet_id,
        "Topic sections",
        "timeline_ranges",
        ai_generated=True,
    )
    sidecar_column_id = project.add_column(
        sheet_id,
        TOPIC_ANALYSIS_SIDECAR_COLUMN,
        "json",
        ai_generated=True,
        hidden=True,
    )
    successful_row, failed_row, missing_sidecar_row = project.add_rows(
        sheet_id,
        [
            {"Source": "Successful"},
            {"Source": "Failed"},
            {"Source": "Missing sidecar"},
        ],
        {"Source": source_column_id},
    )
    payload = _sidecar()
    op_id, run_id, store = _start_run(
        project,
        sheet_id=sheet_id,
        row_ids=[successful_row, failed_row, missing_sidecar_row],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": successful_row,
                "column_id": selection_column_id,
                "value": _ranges(),
                "outcome": "ok",
            },
            {
                "row_id": successful_row,
                "column_id": sidecar_column_id,
                "value": payload,
                "outcome": "ok",
            },
            {
                "row_id": failed_row,
                "column_id": selection_column_id,
                "error": "segmentation failed",
                "error_code": "engine_error",
                "outcome": "model_error",
            },
            {
                "row_id": missing_sidecar_row,
                "column_id": selection_column_id,
                "value": _ranges(),
                "outcome": "ok",
            },
        ],
    )
    store.finish_run(run_id)
    store.point_column_at_run(op_id, selection_column_id, run_id)
    store.point_column_at_run(op_id, sidecar_column_id, run_id)

    def resolve(row_id: int):
        return resolve_topic_analysis_sidecar(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            selection_column_id=selection_column_id,
        )

    resolved = resolve(successful_row)
    assert resolved is not None
    assert resolved.run_id == run_id
    assert resolved.payload == payload
    assert resolve(failed_row) is None
    assert resolve(missing_sidecar_row) is None

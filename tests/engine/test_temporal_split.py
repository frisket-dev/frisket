from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pytest
from frisket.contracts.action import (
    ActionError,
    Receipt,
)
from frisket.actions.types import ActionRequest, SheetRows
from frisket.actions.temporal_segments import TemporalSegmentsParams
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.executor.temporal_split import (
    ACTION_KIND,
    resolve_temporal_split_plan,
)
from frisket.engine.executor.temporal_transcripts import (
    resolve_timestamped_transcript,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.temporal_materialization import revalidate_action_sources
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import canonical_json_hash, resolve_timeline
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_span,
)
from frisket.engine.store.media_splice import (
    MediaSpliceError,
    MediaSpliceProbe,
    MediaStreamProbe,
    StagedMediaSplice,
)
from frisket.engine.store.runs import RunResultStore
from helpers import replace_test_source_cell, write_claimed_test_results
from frisket.features.temporal_values import canonical_temporal_hash
from frisket.features.topic_segmentation.contracts import (
    TOPIC_ANALYSIS_SIDECAR_COLUMN,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
)


PROJECT_ID = "project-temporal-split-test"


def test_temporal_split_has_one_action_job_registration_owner() -> None:
    import frisket.engine.jobs as jobs

    from frisket.engine.executor.action_bindings import action_job_bindings
    from frisket.engine.executor.table_action import run_typed_table_action_job
    from frisket.engine.jobs.worker import (
        HandlerRegistry,
        register_action_job_binding_handlers,
    )

    binding_registry = HandlerRegistry()
    register_action_job_binding_handlers(binding_registry)

    assert action_job_bindings()[ACTION_KIND] is run_typed_table_action_job
    assert binding_registry.action_executor(ACTION_KIND) is run_typed_table_action_job
    assert not hasattr(jobs, "TEMPORAL_ACTION_JOB_KINDS")
    assert not hasattr(jobs, "register_temporal_action_job_handlers")


def _seed_media(
    project: Project,
    *,
    duration_ms: int = 10_000,
    transcript: bool = False,
    transcript_text: str = "First sentence. Second sentence.",
) -> dict[str, Any]:
    sheet_id = project.add_sheet("Media")
    columns = {"video": project.add_column(sheet_id, "video", "video")}
    if transcript:
        columns["transcript"] = project.add_column(
            sheet_id,
            "transcript",
            "timestamped_transcript",
            ai_generated=True,
        )
    blob_hash = project.add_blob(
        b"source-video-bytes" * 32,
        filename="source.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": duration_ms / 1000, "kind": "video"}
        ),
    )
    row_value: dict[str, Any] = {
        "video": {
            "blob": blob_hash,
            "filename": "source.mp4",
            "mime": "video/mp4",
        }
    }
    row_id = project.add_rows(sheet_id, [row_value], columns)[0]
    transcript_run_id = None
    transcript_op_id = None
    if transcript:
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
                    "column_id": columns["transcript"],
                    "value": transcript_text,
                }
            ],
        )
        runs.finish_run(transcript_run_id)
        runs.point_column_at_run(
            transcript_op_id,
            columns["transcript"],
            transcript_run_id,
        )
    return {
        "sheet_id": sheet_id,
        "columns": columns,
        "row_id": row_id,
        "blob_hash": blob_hash,
        "transcript_run_id": transcript_run_id,
        "transcript_op_id": transcript_op_id,
    }


def _request(
    seeded: dict[str, Any],
    selection: dict[str, Any],
    *,
    target: str = "Clips",
    output_name: str = "clip",
) -> ActionRequest:
    return ActionRequest(
        action_id=ACTION_KIND,
        scope=SheetRows(sheet_id=seeded["sheet_id"], row_ids=[seeded["row_id"]]),
        params={"source": "video", "selection": selection},
        sheet_name=target,
        output_names={"clip": output_name},
        idempotency_key="split-test-key",
    )


def _with_key(
    request: ActionRequest,
    *,
    key: str = "split-test-key",
) -> ActionRequest:
    return request.model_copy(update={"idempotency_key": key})


def _plan(project: Project, request: ActionRequest):
    params = TemporalSegmentsParams.model_validate(request.params)
    return resolve_temporal_split_plan(
        project, scope=request.scope, source=params.source, selection=params.selection
    )


def _run(project, request, *, stage_fn, **kwargs):
    from frisket.engine.executor import temporal_media_read

    bound = typed_action_for_request(request.model_dump(mode="json"))
    real_stage = temporal_media_read.stage_temporal_split_plan
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            temporal_media_read,
            "stage_temporal_split_plan",
            lambda *args, **opts: real_stage(*args, **opts, stage_fn=stage_fn),
        )
        return run_typed_create_sheet_action(project, bound=bound, **kwargs)


def _fake_stage(
    _project: Project,
    _source: Any,
    temporal_range: Any,
    output_path: Path,
) -> StagedMediaSplice:
    output_path.write_bytes(
        f"clip:{temporal_range.start_ms}:{temporal_range.end_ms}".encode()
    )
    duration = temporal_range.end_ms - temporal_range.start_ms
    probe = MediaSpliceProbe(
        duration_ms=duration,
        size_bytes=output_path.stat().st_size,
        format_name="mov,mp4",
        format_start_time="0.000000",
        bit_rate=128_000,
        streams=(
            MediaStreamProbe(
                index=0,
                codec_type="video",
                codec_name="h264",
                time_base="1/1000",
                start_time="0.000000",
                duration=f"{duration / 1000:.3f}",
                avg_frame_rate="25/1",
                r_frame_rate="25/1",
                width=640,
                height=360,
            ),
        ),
    )
    return StagedMediaSplice(
        path=output_path,
        filename=output_path.name,
        mime="video/mp4",
        media_kind="video",
        requested_start_ms=temporal_range.start_ms,
        requested_end_ms=temporal_range.end_ms,
        resolved_source_start_ms=temporal_range.start_ms,
        resolved_source_end_ms=temporal_range.end_ms,
        duration_ms=duration,
        alignment_error_ms=0,
        alignment_tolerance_ms=100,
        precision="frame_accurate",
        renderer_profile="test.renderer.v1",
        renderer_params={"fixture": True},
        probe=probe,
    )


def test_split_normalizes_points_and_preserves_ranges(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "split-normalization.frisket")
    try:
        seeded = _seed_media(project)
        point_plan = _plan(
            project,
            _request(
                seeded,
                {
                    "kind": "draft_points",
                    "items": [
                        {"id": "p7", "at_ms": 7_000},
                        {"id": "edge-0", "at_ms": 0},
                        {"id": "p3-a", "at_ms": 3_000},
                        {"id": "p3-b", "at_ms": 3_000},
                        {"id": "edge-end", "at_ms": 10_000},
                    ],
                },
            ),
        )
        assert not isinstance(point_plan, Exception)
        assert [
            (item.start_ms, item.end_ms) for item in point_plan.sources[0].source.ranges
        ] == [
            (0, 3_000),
            (3_000, 7_000),
            (7_000, 10_000),
        ]
        assert set(
            point_plan.sources[0].source.ranges[0].metadata["boundary_item_ids"]
        ) == {"p3-a", "p3-b"}
        assert [
            item["id"] for item in point_plan.sources[0].source.selection_value["items"]
        ] == ["p7", "edge-0", "p3-a", "p3-b", "edge-end"]
        assert len(point_plan.sources[0].warnings) == 2

        range_plan = _plan(
            project,
            _request(
                seeded,
                {
                    "kind": "draft_ranges",
                    "items": [
                        {"id": "late", "start_ms": 5_000, "end_ms": 8_000},
                        {"id": "overlap", "start_ms": 2_000, "end_ms": 6_000},
                        {"id": "duplicate", "start_ms": 5_000, "end_ms": 8_000},
                    ],
                },
                target="Range clips",
            ),
        )
        assert not isinstance(range_plan, Exception)
        ranges = range_plan.sources[0].source.ranges
        assert [(item.start_ms, item.end_ms, item.item_id) for item in ranges] == [
            (5_000, 8_000, "late"),
            (2_000, 6_000, "overlap"),
            (5_000, 8_000, "duplicate"),
        ]
    finally:
        project.close()


def test_split_warns_when_a_typed_transcript_cannot_be_inherited(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "split-transcript-warning.frisket")
    try:
        seeded = _seed_media(project, transcript=True)
        plan = _plan(
            project,
            _request(
                seeded,
                {
                    "kind": "draft_ranges",
                    "items": [{"start_ms": 1_000, "end_ms": 2_000}],
                },
            ),
        )

        assert not isinstance(plan, ActionError)
        assert plan.sources[0].transcripts == ()
        assert plan.sources[0].warnings == (
            'Transcript "transcript" was not inherited because its current timestamp '
            "evidence is missing, stale, or ambiguous.",
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    ("selection", "item_id_prefix"),
    [
        (
            {
                "kind": "draft_points",
                "items": [{"at_ms": 3_000}, {"at_ms": 7_000}],
            },
            "tp_",
        ),
        (
            {
                "kind": "draft_ranges",
                "items": [
                    {"start_ms": 1_000, "end_ms": 4_000},
                    {"start_ms": 6_000, "end_ms": 9_000},
                ],
            },
            "tr_",
        ),
    ],
)
def test_split_manual_missing_ids_are_stable_across_repeated_prepare(
    tmp_path: Path,
    selection: dict[str, Any],
    item_id_prefix: str,
) -> None:
    project = Project.create(tmp_path / f"split-stable-{item_id_prefix}.frisket")
    try:
        seeded = _seed_media(project)
        params = _request(seeded, selection)

        first = _plan(project, params)
        second = _plan(project, params)

        assert not isinstance(first, ActionError)
        assert not isinstance(second, ActionError)
        first_source = first.sources[0].source
        second_source = second.sources[0].source
        assert first_source.selection_value == second_source.selection_value
        assert first_source.selection_hash == second_source.selection_hash
        assert [item.item_id for item in first_source.ranges] == [
            item.item_id for item in second_source.ranges
        ]
        item_ids = [item["id"] for item in first_source.selection_value["items"]]
        assert len(item_ids) == len(set(item_ids))
        assert all(item_id.startswith(item_id_prefix) for item_id in item_ids)
    finally:
        project.close()


def test_typed_selection_snapshot_expands_defaults_before_hashing(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "split-canonical-selection.frisket")
    try:
        seeded = _seed_media(project)
        anchor = resolve_timeline(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["video"],
        ).anchor.wire_value()
        omitted = {
            "schema_version": "frisket.timeline_points.v1",
            "timeline": anchor,
            "items": [{"id": "point", "at_ms": 3_000}],
        }
        explicit = {
            "schema_version": "frisket.timeline_points.v1",
            "timeline": anchor,
            "items": [
                {
                    "id": "point",
                    "at_ms": 3_000,
                    "label": None,
                    "metadata": {},
                }
            ],
        }
        omitted_before = json.loads(json.dumps(omitted))
        omitted_params = _request(seeded, {"kind": "typed_value", "value": omitted})
        explicit_params = _request(seeded, {"kind": "typed_value", "value": explicit})
        params_before = omitted_params.model_dump(mode="json")

        omitted_plan = _plan(project, omitted_params)
        explicit_plan = _plan(project, explicit_params)

        assert not isinstance(omitted_plan, ActionError)
        assert not isinstance(explicit_plan, ActionError)
        omitted_source = omitted_plan.sources[0].source
        explicit_source = explicit_plan.sources[0].source
        assert omitted_source.selection_value == explicit_source.selection_value
        assert omitted_source.selection_hash == explicit_source.selection_hash
        assert omitted_source.selection_value["items"][0] == {
            "id": "point",
            "label": None,
            "metadata": {},
            "at_ms": 3_000,
        }
        assert omitted_params.model_dump(mode="json") == params_before
        assert omitted == omitted_before
        assert "label" not in omitted["items"][0]
        assert "metadata" not in omitted["items"][0]
    finally:
        project.close()


def test_selection_column_revalidation_hashes_canonical_value(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "split-canonical-revalidation.frisket")
    try:
        seeded = _seed_media(project)
        anchor = resolve_timeline(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["video"],
        ).anchor.wire_value()
        selection_column_id = project.add_column(
            seeded["sheet_id"], "timestamps", "timeline_points"
        )
        # Model a valid legacy value that predates default-expanded temporal
        # persistence.  Resolving it produces a canonical receipt snapshot;
        # revalidation of the unchanged cell must use the same representation.
        replace_test_source_cell(
            project,
            row_id=seeded["row_id"],
            column_id=selection_column_id,
            value={
                "schema_version": "frisket.timeline_points.v1",
                "timeline": anchor,
                "items": [{"id": "point", "at_ms": 3_000}],
            },
        )
        plan = _plan(
            project,
            _request(
                seeded,
                {"kind": "column", "column": "timestamps"},
            ),
        )

        assert not isinstance(plan, ActionError)
        source = plan.sources[0].source
        assert source.selection_value["items"][0]["label"] is None
        assert source.selection_value["items"][0]["metadata"] == {}
        revalidate_action_sources(project, plan.source_snapshots)
    finally:
        project.close()


def test_column_selection_without_row_ids_splits_every_visible_source(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "split-column-all-visible.frisket")
    try:
        seeded = _seed_media(project)
        second_blob = project.add_blob(
            b"second-source-video" * 32,
            filename="second.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 12, "kind": "video"}
            ),
        )
        second_row_id = project.add_rows(
            seeded["sheet_id"],
            [
                {
                    "video": {
                        "blob": second_blob,
                        "filename": "second.mp4",
                        "mime": "video/mp4",
                    }
                }
            ],
            {"video": seeded["columns"]["video"]},
        )[0]
        row_ids = [seeded["row_id"], second_row_id]
        anchors = {
            row_id: resolve_timeline(
                project,
                sheet_id=seeded["sheet_id"],
                row_id=row_id,
                column_id=seeded["columns"]["video"],
            ).anchor.wire_value()
            for row_id in row_ids
        }
        selection_column_id = project.add_column(
            seeded["sheet_id"], "boundaries", "timeline_points"
        )
        project.apply_edits(
            [
                {
                    "row_id": row_id,
                    "column_id": selection_column_id,
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": anchors[row_id],
                        "items": [
                            {
                                "id": f"cut-{row_id}",
                                "at_ms": 3_000 if row_id == row_ids[0] else 4_000,
                            }
                        ],
                    },
                }
                for row_id in row_ids
            ],
            label="seed per-row boundaries",
        )
        params = ActionRequest(
            action_id=ACTION_KIND,
            scope=SheetRows(sheet_id=seeded["sheet_id"]),
            params={
                "source": "video",
                "selection": {"kind": "column", "column": "boundaries"},
            },
            sheet_name="Every visible source",
            idempotency_key="split-column-all-visible",
        )

        assert params.scope.row_ids is None
        plan = _plan(project, params)
        assert not isinstance(plan, ActionError)
        assert [source.row_id for source in plan.source_snapshots] == row_ids
        assert plan.output_count == 4

        result = _run(
            project,
            _with_key(params, key="split-column-all-visible"),
            project_id=PROJECT_ID,
            stage_fn=_fake_stage,
        )

        assert result.status == "completed"
        child_sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert child_sheet_id is not None
        parent_ids = [
            int(row["parent_row_id"])
            for row in project.db.execute(
                "SELECT parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            )
        ]
        assert parent_ids == [row_ids[0], row_ids[0], row_ids[1], row_ids[1]]
    finally:
        project.close()


@pytest.mark.parametrize(
    "selection",
    [
        {"kind": "draft_points", "items": [{"at_ms": 10_001}]},
        {
            "kind": "draft_ranges",
            "items": [{"start_ms": 9_000, "end_ms": 10_001}],
        },
    ],
)
def test_split_reports_manual_selection_beyond_source_duration(
    tmp_path: Path,
    selection: dict[str, Any],
) -> None:
    project = Project.create(tmp_path / "split-out-of-bounds.frisket")
    try:
        seeded = _seed_media(project)
        plan = _plan(project, _request(seeded, selection))

        assert isinstance(plan, ActionError)
        assert plan.code == "range_out_of_bounds"
        assert "outside its timeline duration" in plan.message
    finally:
        project.close()


def test_split_bounds_duration_omitting_typed_point_against_resolved_timeline(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "split-typed-out-of-bounds.frisket")
    try:
        seeded = _seed_media(project)
        anchor = resolve_timeline(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["video"],
        ).anchor.wire_value()
        anchor.pop("duration_ms")
        plan = _plan(
            project,
            _request(
                seeded,
                {
                    "kind": "typed_value",
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": anchor,
                        "items": [{"id": "too-late", "at_ms": 10_001}],
                    },
                },
            ),
        )

        assert isinstance(plan, ActionError)
        assert plan.code == "range_out_of_bounds"
    finally:
        project.close()


def test_split_rejects_batch_when_one_source_has_no_effective_ranges(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "split-empty-source.frisket")
    try:
        seeded = _seed_media(project, duration_ms=10_000)
        second_blob = project.add_blob(
            b"second-source-video" * 32,
            filename="second.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 20, "kind": "video"}
            ),
        )
        second_row_id = project.add_rows(
            seeded["sheet_id"],
            [
                {
                    "video": {
                        "blob": second_blob,
                        "filename": "second.mp4",
                        "mime": "video/mp4",
                    }
                }
            ],
            {"video": seeded["columns"]["video"]},
        )[0]
        params = ActionRequest(
            action_id=ACTION_KIND,
            scope=SheetRows(
                sheet_id=seeded["sheet_id"], row_ids=[seeded["row_id"], second_row_id]
            ),
            params={
                "source": "video",
                "selection": {
                    "kind": "draft_points",
                    "items": [{"id": "ten-seconds", "at_ms": 10_000}],
                    "repeat_for_rows": True,
                },
            },
            sheet_name="Mixed clips",
            idempotency_key="split-mixed-clips",
        )
        plan = _plan(project, params)
        assert getattr(plan, "code", None) == "timestamps_required"
        assert plan.details["row_id"] == seeded["row_id"]
    finally:
        project.close()


def test_split_writes_child_ranges_and_exact_timeline_rows(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "split-write.frisket")
    try:
        seeded = _seed_media(project)
        params = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [
                    {"id": "quote", "start_ms": 1_000, "end_ms": 3_000},
                    {"id": "followup", "start_ms": 5_000, "end_ms": 9_000},
                ],
            },
            output_name="segment",
        )
        result = _run(
            project,
            _with_key(params),
            project_id=PROJECT_ID,
            stage_fn=_fake_stage,
        )
        assert result.status == "completed"
        sheet_output = next(item for item in result.outputs if item.kind == "sheet")
        child_sheet_id = sheet_output.sheet_id
        assert child_sheet_id is not None
        columns = {row["name"]: row for row in project.columns(child_sheet_id)}
        assert {name: row["type"] for name, row in columns.items()} == {
            "segment": "video",
            "source_range": "timeline_range",
        }
        row_ids = next(item for item in result.outputs if item.kind == "rows").row_ids
        assert len(row_ids) == 2
        parent_ids = [
            row["parent_row_id"]
            for row in project.db.execute(
                "SELECT parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
                (child_sheet_id,),
            )
        ]
        assert parent_ids == [seeded["row_id"], seeded["row_id"]]
        ranges = project.get_values(
            child_sheet_id,
            columns["source_range"]["id"],
            row_ids=row_ids,
        )
        assert [
            (ranges[row_id]["item"]["start_ms"], ranges[row_id]["item"]["end_ms"])
            for row_id in row_ids
        ] == [(1_000, 3_000), (5_000, 9_000)]

        timeline_rows = project.db.execute(
            "SELECT ats.derived_start_ms, ats.derived_end_ms, "
            "ats.source_start_ms, ats.source_end_ms, ats.rate_num, ats.rate_den, "
            "ats.receipt_id "
            "FROM artifact_timeline_segments ats "
            "JOIN source_artifacts sa ON sa.id=ats.derived_artifact_id "
            "WHERE sa.source_sheet_id=? ORDER BY sa.source_row_id",
            (child_sheet_id,),
        ).fetchall()
        assert [
            (
                row["derived_start_ms"],
                row["derived_end_ms"],
                row["source_start_ms"],
                row["source_end_ms"],
                row["rate_num"],
                row["rate_den"],
            )
            for row in timeline_rows
        ] == [
            (0, 2_000, 1_000, 3_000, 1, 1),
            (0, 4_000, 5_000, 9_000, 1, 1),
        ]
        assert all(row["receipt_id"] == result.receipt_id for row in timeline_rows)
        assert (
            project.db.execute(
                "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["status"]
            == "completed"
        )

        def should_not_stage(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("idempotent replay must not render again")

        replay = _run(
            project,
            _with_key(params),
            project_id=PROJECT_ID,
            stage_fn=should_not_stage,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        assert len(project.sheets()) == 2

        changed_params = params.model_copy(update={"sheet_name": "Different clips"})
        conflict = _run(
            project,
            _with_key(changed_params),
            project_id=PROJECT_ID,
            stage_fn=should_not_stage,
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
    finally:
        project.close()


def test_split_projects_compatible_typed_annotations_into_each_child(
    tmp_path: Path,
) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "split-annotations.frisket")
    try:
        seeded = _seed_media(project)
        marker_column_id = project.add_column(
            seeded["sheet_id"], "markers", "timeline_points"
        )
        text_column_id = project.add_column(seeded["sheet_id"], "notes", "text")
        source = resolve_timeline(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["video"],
        )
        project.apply_edits(
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": marker_column_id,
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": source.anchor.wire_value(),
                        "items": [
                            {"id": "first", "at_ms": 2_500},
                            {"id": "second", "at_ms": 6_500},
                        ],
                    },
                },
                {
                    "row_id": seeded["row_id"],
                    "column_id": text_column_id,
                    "value": "not a temporal annotation",
                },
            ],
            label="seed split annotations",
        )
        params = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [
                    {"id": "one", "start_ms": 2_000, "end_ms": 4_000},
                    {"id": "two", "start_ms": 6_000, "end_ms": 8_000},
                ],
            },
            target="Annotated clips",
        )
        action = _with_key(params, key="split-annotations")
        result = _run(
            project,
            action,
            project_id=PROJECT_ID,
            stage_fn=_fake_stage,
        )

        assert result.status == "completed"
        child_sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        columns = {row["name"]: row for row in project.columns(child_sheet_id)}
        assert set(columns) == {"clip", "source_range", "markers"}
        row_ids = next(
            output.row_ids for output in result.outputs if output.kind == "rows"
        )
        values = project.get_values(
            child_sheet_id,
            int(columns["markers"]["id"]),
            row_ids=row_ids,
        )
        assert [values[row_id]["items"][0]["at_ms"] for row_id in row_ids] == [
            500,
            500,
        ]
        assert [values[row_id]["items"][0]["id"] for row_id in row_ids] == [
            "first",
            "second",
        ]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt is not None
        assert (
            sum(
                item.ref.get("kind") == "temporal_annotation_projection"
                for item in receipt.evidence
            )
            == 2
        )

        replay = _run(
            project,
            action,
            project_id=PROJECT_ID,
            stage_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("replay must not render")
            ),
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


def test_split_stage_failure_publishes_no_partial_outputs(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "split-stage-failure.frisket")
    staged_paths: list[Path] = []

    def fail_second(
        project_arg: Project,
        source: Any,
        temporal_range: Any,
        output_path: Path,
    ) -> StagedMediaSplice:
        staged_paths.append(output_path)
        if len(staged_paths) == 2:
            raise MediaSpliceError(
                "ffmpeg_failed", "fixture stage failed", details={"fixture": True}
            )
        return _fake_stage(project_arg, source, temporal_range, output_path)

    try:
        seeded = _seed_media(project)
        params = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [
                    {"start_ms": 1_000, "end_ms": 2_000},
                    {"start_ms": 3_000, "end_ms": 4_000},
                ],
            },
        )
        result = _run(
            project,
            _with_key(params, key="split-stage-failure"),
            project_id=PROJECT_ID,
            stage_fn=fail_second,
        )
        assert result.status == "failed"
        assert result.errors[0].code == "temporal_materializer_unavailable"
        assert [row["name"] for row in project.sheets()] == ["Media"]
        assert (
            project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments"
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert all(not path.exists() for path in staged_paths)
    finally:
        project.close()


def _insert_split_failure_receipt(
    project: Project,
    *,
    receipt_id: str,
    status: str = "running",
    errors: list[ActionError] | None = None,
    action_id: str | None = None,
    idempotency_key: str | None = None,
    params_hash: str | None = None,
) -> Receipt:
    from frisket.engine.store.receipts import ReceiptStore

    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=PROJECT_ID,
        action_id=action_id or f"act_{receipt_id}",
        action_kind=ACTION_KIND,
        idempotency_key=idempotency_key or f"{ACTION_KIND}@sha256:{receipt_id}",
        params_hash=params_hash or f"sha256:{receipt_id}",
        status=status,
        errors=errors or [],
    )
    ReceiptStore(project).insert(receipt)
    return receipt


def test_split_failure_preserves_error_facts_by_value(tmp_path: Path) -> None:
    from frisket.engine.executor.action_reservations import (
        _terminalize_claimless_direct_failure,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "split-failure-error-facts.frisket")
    receipt_id = "receipt_split_failure_error_facts"
    error = ActionError(
        code="split_provider_declined",
        message="split provider rejected the request",
        action_kind=ACTION_KIND,
        details={"provider": "fixture", "billable": False},
    )
    try:
        _insert_split_failure_receipt(project, receipt_id=receipt_id)
        result = _terminalize_claimless_direct_failure(
            project,
            project_id=PROJECT_ID,
            action_kind=ACTION_KIND,
            stored_receipt=ReceiptStore(project).find_by_id(receipt_id),
            project_write_failed_message="Split failure receipt could not be finalized.",
            error=error,
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.errors == [error]
        assert stored is not None and stored.errors == [error]
    finally:
        project.close()


@pytest.mark.parametrize("prior_status", ["cancelled", "failed"])
def test_split_failure_preserves_prior_terminal_receipt(
    tmp_path: Path,
    prior_status: str,
) -> None:
    from frisket.engine.executor.action_reservations import (
        _terminalize_claimless_direct_failure,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / f"split-prior-{prior_status}.frisket")
    receipt_id = f"receipt_split_prior_{prior_status}"
    prior_error = ActionError(
        code="prior_failure",
        message="the prior failure wins",
        action_kind=ACTION_KIND,
        details={"attempt": 1},
    )
    try:
        prior = _insert_split_failure_receipt(
            project,
            receipt_id=receipt_id,
            status=prior_status,
            errors=[prior_error] if prior_status == "failed" else None,
        )
        result = _terminalize_claimless_direct_failure(
            project,
            project_id=PROJECT_ID,
            action_kind=ACTION_KIND,
            stored_receipt=ReceiptStore(project).find_by_id(receipt_id),
            project_write_failed_message="Split failure receipt could not be finalized.",
            error=ActionError(
                code="late_failure",
                message="late failure must not rewrite the receipt",
                action_kind=ACTION_KIND,
            ),
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.status == prior_status
        assert result.receipt_id == prior.receipt_id
        assert result.errors == prior.errors
        assert stored == prior
    finally:
        project.close()


def test_queued_split_failure_leaves_worker_owned_receipt_untouched(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor.map_rows_action import typed_request_hash
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "queued-split-owned-receipt.frisket")
    receipt_id = "receipt_queued_split_owned_by_worker"
    action_id = "act_queued_split_owned_by_worker"
    try:
        seeded = _seed_media(project)
        params = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [{"start_ms": 1_000, "end_ms": 2_000}],
            },
        )
        action = _with_key(params, key="queued-split-owned-receipt")
        prior = _insert_split_failure_receipt(
            project,
            receipt_id=receipt_id,
            action_id=action_id,
            idempotency_key=action.idempotency_key,
            params_hash=typed_request_hash(
                typed_action_for_request(action.model_dump(mode="json"))
            ),
        )

        def fail_stage(*_args: Any, **_kwargs: Any) -> StagedMediaSplice:
            raise RuntimeError("queued split fixture failure")

        result = _run(
            project,
            action,
            project_id=PROJECT_ID,
            reserved_action_id=action_id,
            reserved_receipt_id=receipt_id,
            stage_fn=fail_stage,
        )
        stored = ReceiptStore(project).parsed_by_id(receipt_id)

        assert result.status == "failed"
        assert stored == prior
    finally:
        project.close()


def test_split_source_change_after_staging_publishes_nothing(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "split-stale-before-commit.frisket")
    try:
        seeded = _seed_media(project)
        replacement_hash = project.add_blob(
            b"replacement-video-bytes" * 32,
            filename="replacement.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 10, "kind": "video"}
            ),
        )

        def stage_then_change_source(
            project_arg: Project,
            source: Any,
            temporal_range: Any,
            output_path: Path,
        ) -> StagedMediaSplice:
            staged = _fake_stage(project_arg, source, temporal_range, output_path)
            replace_test_source_cell(
                project_arg,
                row_id=seeded["row_id"],
                column_id=seeded["columns"]["video"],
                value={
                    "blob": replacement_hash,
                    "filename": "replacement.mp4",
                    "mime": "video/mp4",
                },
            )
            return staged

        params = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [{"start_ms": 1_000, "end_ms": 2_000}],
            },
        )
        result = _run(
            project,
            _with_key(params, key="split-stale-before-commit"),
            project_id=PROJECT_ID,
            stage_fn=stage_then_change_source,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "stale_input"
        assert [row["name"] for row in project.sheets()] == ["Media"]
        assert (
            project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM artifact_timeline_segments"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_topic_split_media_and_transcript_share_atomic_overlapping_chunks(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "split-transcript.frisket")
    try:
        seeded = _seed_media(
            project,
            duration_ms=6_300,
            transcript=True,
            transcript_text="w0 w1 w2 w3 w4",
        )
        lease = resolve_timeline(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["video"],
        )
        span_refs = []
        primary_span_ids: list[str] = []
        for rank, (start_ms, end_ms, text) in enumerate(
            [
                (0, 1_300, "w0"),
                (1_000, 2_300, "w1"),
                (2_000, 3_300, "w2"),
                (3_000, 4_300, "w3"),
                (4_000, 5_300, "w4"),
            ]
        ):
            span = record_source_span(
                project,
                artifact_id=lease.anchor.artifact_id,
                span_kind="temporal",
                start_ms=start_ms,
                end_ms=end_ms,
                quote=text,
                selector={"segment_index": rank},
            )
            span_refs.append({"span_id": span["id"], "rank": rank})
            primary_span_ids.append(str(span["stable_id"]))
        _values, refs = project.get_values_with_refs(
            seeded["sheet_id"],
            seeded["columns"]["transcript"],
            row_ids=[seeded["row_id"]],
        )
        primary_link = record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=refs[seeded["row_id"]],
            spans=span_refs,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["transcript"],
            run_id=seeded["transcript_run_id"],
            op_id=seeded["transcript_op_id"],
            link_role="media_transcribe_temporal",
            producer={"action_kind": "media.transcribe"},
            metadata={
                "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
                "semantic_type": "timestamped_transcript",
            },
        )
        alternate_column_id = project.add_column(
            seeded["sheet_id"],
            "alternate transcript",
            "timestamped_transcript",
            ai_generated=True,
        )
        alternate_op_id = project.append_op(
            "media.transcribe",
            {
                "kind": "media.transcribe",
                "params": {"output_name": "alternate transcript"},
            },
            label="transcribe alternate transcript",
        )
        runs = RunResultStore(project)
        alternate_run_id = runs.start_run(
            alternate_op_id,
            seeded["sheet_id"],
            "media.transcribe",
            params={"output_name": "alternate transcript"},
            total_rows=1,
            row_ids=[seeded["row_id"]],
        )
        write_claimed_test_results(
            project,
            alternate_run_id,
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": alternate_column_id,
                    "value": "Alternate wording.",
                }
            ],
        )
        runs.finish_run(alternate_run_id)
        runs.point_column_at_run(
            alternate_op_id,
            alternate_column_id,
            alternate_run_id,
        )
        alternate_span = record_source_span(
            project,
            artifact_id=lease.anchor.artifact_id,
            span_kind="temporal",
            start_ms=2_000,
            end_ms=5_000,
            quote="Alternate wording.",
            selector={"segment_index": 0},
        )
        _values, alternate_refs = project.get_values_with_refs(
            seeded["sheet_id"],
            alternate_column_id,
            row_ids=[seeded["row_id"]],
        )
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=alternate_refs[seeded["row_id"]],
            spans=[{"span_id": alternate_span["id"], "rank": 0}],
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=alternate_column_id,
            run_id=alternate_run_id,
            op_id=alternate_op_id,
            link_role="media_transcribe_temporal",
            producer={"action_kind": "media.transcribe"},
            metadata={
                "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
                "semantic_type": "timestamped_transcript",
            },
        )
        primary_transcript = resolve_timestamped_transcript(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["transcript"],
        )
        assert primary_transcript is not None
        topic_column_id = project.add_column(
            seeded["sheet_id"],
            "Topic sections",
            "timeline_ranges",
            ai_generated=True,
        )
        sidecar_column_id = project.add_column(
            seeded["sheet_id"],
            TOPIC_ANALYSIS_SIDECAR_COLUMN,
            "json",
            ai_generated=True,
            hidden=True,
        )
        topic_value = {
            "schema_version": "frisket.timeline_ranges.v1",
            "timeline": lease.anchor.wire_value(),
            "items": [
                {
                    "id": "before",
                    "start_ms": 0,
                    "end_ms": 4_300,
                    "metadata": {
                        "requested_range": {"start_ms": 0, "end_ms": 3_300},
                        "boundary_policy": ("include_intersecting_transcript_chunks"),
                    },
                },
                {
                    "id": "after",
                    "start_ms": 1_000,
                    "end_ms": 6_300,
                    "metadata": {
                        "requested_range": {"start_ms": 2_000, "end_ms": 6_300},
                        "boundary_policy": ("include_intersecting_transcript_chunks"),
                    },
                },
            ],
        }
        topic_op_id = project.append_op(
            "map.find_topic_sections",
            {"action_kind": "map.find_topic_sections"},
            label="find topic sections",
        )
        topic_run_id = runs.start_run(
            topic_op_id,
            seeded["sheet_id"],
            "map.find_topic_sections",
            total_rows=1,
            row_ids=[seeded["row_id"]],
        )
        write_claimed_test_results(
            project,
            topic_run_id,
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": topic_column_id,
                    "value": topic_value,
                    "outcome": "ok",
                },
                {
                    "row_id": seeded["row_id"],
                    "column_id": sidecar_column_id,
                    "value": {
                        "schema_version": TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
                        "transcript_evidence_id": primary_link["stable_id"],
                        "transcript_snapshot_hash": primary_transcript.snapshot_hash,
                        "transcript_run_id": primary_transcript.transcript_run_id,
                        "transcript_column_id": (
                            primary_transcript.transcript_column_id
                        ),
                        "transcript_value_ref": (
                            primary_transcript.transcript_value_ref
                        ),
                        "transcript_value_hash": canonical_json_hash(
                            primary_transcript.transcript_value
                        ),
                        "artifact_stable_id": primary_transcript.artifact_stable_id,
                        "timeline": lease.anchor.wire_value(),
                        "language": primary_transcript.language,
                        "engine_id": "fixture-engine",
                        "engine_version": "1",
                        "resolved_settings": {},
                        "engine_diagnostics": {},
                        "native_candidates": [],
                        "locking": {
                            "timeline_ranges_hash": canonical_temporal_hash(
                                "timeline_ranges", topic_value
                            ),
                            "section_unit_ids": {
                                "before": primary_span_ids[:4],
                                "after": primary_span_ids[1:],
                            },
                        },
                    },
                    "outcome": "ok",
                },
            ],
        )
        runs.finish_run(topic_run_id)
        runs.point_column_at_run(topic_op_id, topic_column_id, topic_run_id)
        runs.point_column_at_run(topic_op_id, sidecar_column_id, topic_run_id)
        params = _request(
            seeded,
            {"kind": "column", "column": "Topic sections"},
        )
        action = _with_key(params, key="split-with-transcript")
        result = _run(
            project,
            action,
            project_id=PROJECT_ID,
            stage_fn=_fake_stage,
        )
        assert result.status == "completed"
        child_sheet_id = next(
            item.sheet_id for item in result.outputs if item.kind == "sheet"
        )
        columns = {row["name"]: row for row in project.columns(child_sheet_id)}
        assert set(columns) == {
            "clip",
            "source_range",
            "transcript",
            "alternate transcript",
        }
        assert columns["transcript"]["type"] == "timestamped_transcript"
        row_ids = next(item for item in result.outputs if item.kind == "rows").row_ids
        assert row_ids is not None and len(row_ids) == 2
        media_transcripts = project.get_values(
            child_sheet_id,
            columns["transcript"]["id"],
            row_ids=row_ids,
        )
        assert [media_transcripts[row_id] for row_id in row_ids] == [
            "w0 w1 w2 w3",
            "w1 w2 w3 w4",
        ]
        alternate = project.get_values(
            child_sheet_id,
            columns["alternate transcript"]["id"],
            row_ids=row_ids,
        )
        assert "partial speech" in alternate[row_ids[0]]
        assert alternate[row_ids[1]] == "Alternate wording."

        media_projected = [
            resolve_timestamped_transcript(
                project,
                sheet_id=child_sheet_id,
                row_id=row_id,
                column_id=columns["transcript"]["id"],
            )
            for row_id in row_ids
        ]
        assert all(item is not None for item in media_projected)
        assert [
            [span["quote"] for span in item.spans]
            for item in media_projected
            if item is not None
        ] == [
            ["w0", "w1", "w2", "w3"],
            ["w1", "w2", "w3", "w4"],
        ]
        assert all(
            span["metadata"]["projection"]["clipping"] == "full"
            for item in media_projected
            if item is not None
            for span in item.spans
        )

        media_ranges = project.get_values(
            child_sheet_id,
            columns["source_range"]["id"],
            row_ids=row_ids,
        )
        assert [
            (
                media_ranges[row_id]["item"]["start_ms"],
                media_ranges[row_id]["item"]["end_ms"],
            )
            for row_id in row_ids
        ] == [(0, 4_300), (1_000, 6_300)]
        assert [
            media_ranges[row_id]["item"]["metadata"]["requested_range"]
            for row_id in row_ids
        ] == [
            {"start_ms": 0, "end_ms": 3_300},
            {"start_ms": 2_000, "end_ms": 6_300},
        ]

        projected_links = project.db.execute(
            "SELECT id FROM evidence_links WHERE sheet_id=? "
            "AND link_role='temporal_transcript_projection'",
            (child_sheet_id,),
        ).fetchall()
        assert len(projected_links) == 4

        transcript_result = run_action_spec(
            project,
            {
                "action_id": "derive.transcript_segments",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": seeded["sheet_id"],
                    "row_ids": [seeded["row_id"]],
                },
                "params": {
                    "source": "transcript",
                    "selection": {"kind": "column", "column": "Topic sections"},
                },
                "sheet_name": "Transcript sections",
                "idempotency_key": "split-transcript-parity",
            },
            project_id=PROJECT_ID,
        )
        assert transcript_result.status == "completed"
        transcript_sheet_id = next(
            item.sheet_id for item in transcript_result.outputs if item.kind == "sheet"
        )
        transcript_output = next(
            item for item in transcript_result.outputs if item.name == "transcript"
        )
        transcript_range_output = next(
            item for item in transcript_result.outputs if item.name == "source_range"
        )
        transcript_row_ids = next(
            item.row_ids for item in transcript_result.outputs if item.kind == "rows"
        )
        assert transcript_row_ids is not None
        transcript_values = project.get_values(
            transcript_sheet_id,
            transcript_output.column_id,
            row_ids=transcript_row_ids,
        )
        assert [transcript_values[row_id] for row_id in transcript_row_ids] == [
            media_transcripts[row_id] for row_id in row_ids
        ]
        transcript_ranges = project.get_values(
            transcript_sheet_id,
            transcript_range_output.column_id,
            row_ids=transcript_row_ids,
        )
        assert [
            (
                transcript_ranges[row_id]["item"]["start_ms"],
                transcript_ranges[row_id]["item"]["end_ms"],
            )
            for row_id in transcript_row_ids
        ] == [(0, 4_300), (1_000, 6_300)]
        transcript_projected = [
            resolve_timestamped_transcript(
                project,
                sheet_id=transcript_sheet_id,
                row_id=row_id,
                column_id=transcript_output.column_id,
            )
            for row_id in transcript_row_ids
        ]
        assert all(item is not None for item in transcript_projected)
        assert [
            [span["quote"] for span in item.spans]
            for item in transcript_projected
            if item is not None
        ] == [
            [span["quote"] for span in item.spans]
            for item in media_projected
            if item is not None
        ]
        assert all(
            span["metadata"]["projection"]["clipping"] == "full"
            for item in transcript_projected
            if item is not None
            for span in item.spans
        )

        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()["body"]
        )
        assert (
            sum(
                item["ref"].get("kind") == "temporal_transcript_snapshot"
                for item in receipt["inputs"]
            )
            == 2
        )
        selection_input = next(
            item
            for item in receipt["inputs"]
            if item["ref"].get("kind") == "temporal_selection_snapshot"
        )
        assert selection_input["ref"]["topic_analysis"] == {
            "kind": "topic_analysis_snapshot",
            "sheet_id": seeded["sheet_id"],
            "row_id": seeded["row_id"],
            "selection_column_id": topic_column_id,
            "run_id": topic_run_id,
            "sidecar_column_id": sidecar_column_id,
            "payload_hash": selection_input["ref"]["topic_analysis"]["payload_hash"],
            "transcript_evidence_id": primary_link["stable_id"],
            "transcript_snapshot_hash": primary_transcript.snapshot_hash,
        }

        edited_topic_value = {
            "schema_version": "frisket.timeline_ranges.v1",
            "timeline": lease.anchor.wire_value(),
            "items": [
                {"id": "manual-a", "start_ms": 0, "end_ms": 3_500},
                {"id": "manual-b", "start_ms": 3_500, "end_ms": 6_300},
            ],
        }
        project.apply_edits(
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": topic_column_id,
                    "value": edited_topic_value,
                }
            ]
        )
        edited_plan = _plan(
            project,
            _request(
                seeded,
                {"kind": "column", "column": "Topic sections"},
                target="Edited topic clips",
            ),
        )
        assert not isinstance(edited_plan, ActionError)
        assert [
            (item.start_ms, item.end_ms)
            for item in edited_plan.sources[0].source.ranges
        ] == [(0, 3_500), (3_500, 6_300)]
        assert edited_plan.sources[0].topic_span_ids_by_item == {}
    finally:
        project.close()

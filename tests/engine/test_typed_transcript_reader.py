from __future__ import annotations

from dataclasses import replace

import pytest

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    create_sheet,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest, typed_action_for_request
from frisket.actions.transcript_types import (
    TranscriptColumn,
    TranscriptReader,
    TranscriptSelection,
    TranscriptProjectionValue,
)
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    DynamicTableResult,
    RowSource,
    SheetRows,
    TableColumn,
    TableRow,
    discover_references,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.action_bindings import action_job_bindings
from frisket.engine.executor.action_jobs import (
    _mark_action_job_receipt_running,
    mark_action_job_enqueued,
    reserve_typed_action_job,
    run_action_run_job,
)
from frisket.engine.executor.table_action import (
    run_typed_create_sheet_action,
    run_typed_table_action_job,
)
from frisket.engine.executor.transcript_read import AdmittedTranscriptReader
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_artifact_timeline
from frisket.engine.store.receipts import ReceiptStore
from tests.engine.test_transcript_segments_action import (
    _seed_timestamped_transcript,
    _queued_transcript_action,
)


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "typed-transcripts.frisket")
    seeded = _seed_timestamped_transcript(project)
    try:
        yield project, seeded
    finally:
        project.close()


class RenamedParams(ActionParams):
    original: TranscriptColumn
    chosen: TranscriptSelection


def _custom_bound(handler, seeded, *, scope=None, params=None, names=None):
    definition = action(
        name="excerpts",
        title="Excerpts",
        description="Custom transcript projection",
        category=ActionCategory.CONVERT,
        run=create_sheet(handler),
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.excerpts")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.excerpts",
            scope=scope
            or SheetRows(sheet_id=seeded["sheet_id"], row_ids=[seeded["row_id"]]),
            params=params
            or {
                "original": "transcript",
                "chosen": {"kind": "draft_points", "items": [{"at_ms": 2500}]},
            },
            output_names=names or {},
            sheet_name="Custom excerpts",
            idempotency_key="custom-excerpts",
        ),
    )


def test_renamed_selection_scope_and_catalog_are_semantic(source):
    _project, seeded = source

    def produce(params: RenamedParams, reader: TranscriptReader) -> DynamicTableResult:
        return reader.read(params.original, params.chosen)

    params = {
        "original": "transcript",
        "chosen": {"kind": "draft_range", "start_ms": 0, "end_ms": 4000},
    }
    with pytest.raises(ValueError, match="explicit rows"):
        _custom_bound(
            produce, seeded, scope=SheetRows(sheet_id=seeded["sheet_id"]), params=params
        )
    with pytest.raises(ValueError, match="literal_selection_requires_confirmation"):
        _custom_bound(
            produce,
            seeded,
            scope=SheetRows(sheet_id=seeded["sheet_id"], row_ids=[1, 2]),
            params=params,
        )
    params["chosen"]["repeat_for_rows"] = True
    _custom_bound(
        produce,
        seeded,
        scope=SheetRows(sheet_id=seeded["sheet_id"], row_ids=[1, 2]),
        params=params,
    )
    params["chosen"] = {"kind": "column", "column": "sections"}
    bound = _custom_bound(
        produce, seeded, scope=SheetRows(sheet_id=seeded["sheet_id"]), params=params
    )
    assert {ref.column for ref in discover_references(bound.params)} == {
        "transcript",
        "sections",
    }
    entry = bound.action.catalog_entry()
    assert entry["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows", "exact_membership"],
    }
    selection = next(
        item
        for item in entry["ui_hints"]["source_requirements"]
        if item["param"] == "chosen"
    )
    assert selection["min"] == 0 and selection["max"] == 1
    assert selection["accepted_column_types"] == [
        "timeline_point",
        "timeline_points",
        "timeline_range",
        "timeline_ranges",
    ]
    assert (
        ACTION_REGISTRY.get("derive.transcript_segments").catalog_entry()["async_mode"]
        == "queued"
    )


@pytest.mark.parametrize("rows", [None, [1, 2]])
def test_actual_sibling_literal_model_cannot_bypass_scope_or_repeat_ack(source, rows):
    from frisket.contracts.actions.schemas.temporal import TemporalDraftRangeSelection

    project, seeded = source
    reader = AdmittedTranscriptReader(
        project,
        scope=SheetRows(sheet_id=seeded["sheet_id"], row_ids=rows),
        action_kind="custom.excerpts",
    )
    with pytest.raises(
        ValueError, match="explicit rows|literal_selection_requires_confirmation"
    ):
        reader.read(
            TranscriptColumn("transcript"),
            TemporalDraftRangeSelection(kind="draft_range", start_ms=0, end_ms=4000),
        )
    assert reader.facts == [] and reader.projections == {}


def test_reordered_repeated_occurrences_and_renames_keep_distinct_clocks(source):
    project, seeded = source

    def produce(params: RenamedParams, reader: TranscriptReader) -> DynamicTableResult:
        table = reader.read(params.original, params.chosen)
        return DynamicTableResult(
            schema=table.schema, rows=(table.rows[1], table.rows[0], table.rows[1])
        )

    bound = _custom_bound(
        produce,
        seeded,
        names={"transcript": "Excerpt", "source_range": "Requested source"},
    )
    result = run_typed_create_sheet_action(project, "project-test", bound)
    assert result.status == "completed", result.errors
    columns = {item.name: item for item in result.outputs if item.kind == "column"}
    rows = next(item.row_ids for item in result.outputs if item.kind == "rows")
    column = columns["Excerpt"]
    values = project.get_values(column.sheet_id, column.column_id, row_ids=rows)
    assert list(values.values()) == [
        "Shared chunk. Final chunk.",
        "Shared chunk.",
        "Shared chunk. Final chunk.",
    ]
    projections = [
        resolve_timestamped_transcript(
            project, sheet_id=column.sheet_id, row_id=row, column_id=column.column_id
        )
        for row in rows
    ]
    assert all(projections)
    assert len({item.artifact_id for item in projections}) == 3
    assert [item.language for item in projections] == ["en"] * 3
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    refs = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "derived_transcript_timeline"
    ]
    assert [ref["output"]["row_id"] for ref in refs] == rows
    assert all(ref["output"]["column_id"] == column.column_id for ref in refs)
    flags = {
        item["name"]: bool(item["ai_generated"])
        for item in project.columns(column.sheet_id)
    }
    assert flags == {"Excerpt": True, "Requested source": False}
    replay = run_typed_create_sheet_action(project, "project-test", bound)
    assert replay.receipt_id == result.receipt_id and replay.status == "completed"


@pytest.mark.parametrize("forge", ["projection", "parent"])
def test_foreign_projection_and_forged_parent_are_rejected_before_publication(
    source, forge
):
    project, seeded = source

    def produce(params: RenamedParams, reader: TranscriptReader) -> DynamicTableResult:
        table = reader.read(params.original, params.chosen)
        original = table.rows[0]
        values = dict(original.output.root)
        parent = original.parent
        if forge == "projection":
            values["transcript"] = TranscriptProjectionValue()
        else:
            parent = RowSource(sheet_id=parent.sheet_id, row_id=parent.row_id)
        return DynamicTableResult(
            schema=table.schema,
            rows=(
                TableRow(
                    output=DynamicOutput(values), sources=(parent,), parent=parent
                ),
            ),
        )

    before = _counts(project)
    result = run_typed_create_sheet_action(
        project, "project-test", _custom_bound(produce, seeded)
    )
    assert result.status == "failed"
    assert _counts(project) == before


def _counts(project):
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "sheets",
            "columns",
            "rows",
            "cells",
            "ops",
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "artifact_timeline_segments",
        )
    }


def test_late_projection_failure_rolls_back_every_output_and_finalizes_receipt(
    source, monkeypatch
):
    from frisket.engine.executor import transcript_read

    project, seeded = source
    before = _counts(project)
    original = transcript_read._publish_projection
    calls = []

    def publish(*args, **kwargs):
        refs = original(*args, **kwargs)
        calls.append(refs)
        if len(calls) == 2:
            raise RuntimeError("late injected evidence failure")
        return refs

    monkeypatch.setattr(transcript_read, "_publish_projection", publish)
    result = run_action_spec(
        project,
        _queued_transcript_action(
            seeded, target="Atomic excerpts", key="atomic-excerpts"
        ),
        project_id="project-test",
    )
    assert result.status == "failed" and result.errors[0].code == "project_write_failed"
    assert len(calls) == 2
    assert _counts(project) == before
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.status == "failed" and receipt.op_ids == []


def test_reserved_worker_keeps_identity_job_evidence_and_no_read_redelivery(
    source, monkeypatch
):
    project, seeded = source
    bound = typed_action_for_request(
        _queued_transcript_action(
            seeded, target="Reserved excerpts", key="reserved-excerpts"
        )
    )
    envelope = reserve_typed_action_job(project, "project-test", bound)
    mark_action_job_enqueued(
        project, receipt_id=envelope.receipt_id, job_id=29, job_kind="action.run"
    )
    prior = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    result = run_action_run_job(
        project,
        {"action_job": envelope.to_json(), "job_id": 29},
        executor_lookup=action_job_bindings().get,
    )
    assert result.status == "completed", result.errors
    assert (
        result.receipt_id == envelope.receipt_id
        and result.action.action_id == envelope.action_id
    )
    completed = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    assert all(item in completed.evidence for item in prior.evidence)

    def no_read(*_args, **_kwargs):
        pytest.fail("terminal redelivery must not prepare transcripts")

    monkeypatch.setattr(AdmittedTranscriptReader, "read", no_read)
    replay = run_action_run_job(
        project,
        {"action_job": envelope.to_json(), "job_id": 29},
        executor_lookup=action_job_bindings().get,
    )
    assert replay.status == "completed" and replay.receipt_id == result.receipt_id
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM runs WHERE action_kind='derive.transcript_segments'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("tamper", ["intent", "action_id", "receipt_id"])
def test_reserved_worker_rejects_mismatched_identity_without_publishing(source, tamper):
    project, seeded = source
    bound = typed_action_for_request(
        _queued_transcript_action(
            seeded, target="Reserved excerpts", key="reserved-excerpts"
        )
    )
    envelope = reserve_typed_action_job(project, "project-test", bound)
    _mark_action_job_receipt_running(
        project, project_id="project-test", receipt_id=envelope.receipt_id, job_id=None
    )
    prior = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    before = _counts(project)
    if tamper == "intent":
        altered = {**envelope.action, "sheet_name": "Different intent"}
        result = run_typed_table_action_job(project, replace(envelope, action=altered))
        assert (
            result.status == "failed"
            and result.errors[0].code == "invalid_action_request"
        )
    else:
        with pytest.raises(ValueError, match="reservation"):
            run_typed_table_action_job(
                project, replace(envelope, **{tamper: "foreign"})
            )
    assert _counts(project) == before
    assert ReceiptStore(project).parsed_by_id(envelope.receipt_id) == prior


@pytest.mark.parametrize(
    "kind", ["draft_range", "draft_points", "draft_ranges", "typed_value", "column"]
)
def test_all_five_selection_variants_publish(source, kind):
    project, seeded = source
    transcript = resolve_timestamped_transcript(
        project,
        sheet_id=seeded["sheet_id"],
        row_id=seeded["row_id"],
        column_id=seeded["transcript_column_id"],
    )
    timeline = resolve_artifact_timeline(project, transcript.artifact_id).wire_value()
    value = {
        "schema_version": "frisket.timeline_range.v1",
        "timeline": timeline,
        "item": {"id": "selected", "start_ms": 1000, "end_ms": 4000},
    }
    selections = {
        "draft_range": {"kind": kind, "start_ms": 1000, "end_ms": 4000},
        "draft_points": {"kind": kind, "items": [{"at_ms": 2500}]},
        "draft_ranges": {"kind": kind, "items": [{"start_ms": 1000, "end_ms": 4000}]},
        "typed_value": {"kind": kind, "value": value},
        "column": {"kind": kind, "column": "selection"},
    }
    if kind == "column":
        column = project.add_column(seeded["sheet_id"], "selection", "timeline_range")
        project.apply_edits(
            [{"row_id": seeded["row_id"], "column_id": column, "value": value}],
            label="select",
        )
    body = _queued_transcript_action(seeded, target="Selections", key="five-selections")
    body["params"]["selection"] = selections[kind]
    if kind == "column":
        body["scope"].pop("row_ids")
    result = run_action_spec(project, body, project_id="project-test")
    assert result.status == "completed", result.errors


def _annotation(project, seeded, name="markers"):
    transcript = resolve_timestamped_transcript(
        project,
        sheet_id=seeded["sheet_id"],
        row_id=seeded["row_id"],
        column_id=seeded["transcript_column_id"],
    )
    column = project.add_column(seeded["sheet_id"], name, "timeline_points")
    value = {
        "schema_version": "frisket.timeline_points.v1",
        "timeline": resolve_artifact_timeline(
            project, transcript.artifact_id
        ).wire_value(),
        "items": [{"id": "marker", "at_ms": 2500, "label": "Claim"}],
    }
    project.apply_edits(
        [{"row_id": seeded["row_id"], "column_id": column, "value": value}],
        label="annotate",
    )
    return column, value


def test_reused_annotation_handle_publishes_every_renamed_target(source):
    project, seeded = source
    _annotation(project, seeded)

    def produce(params: RenamedParams, reader: TranscriptReader) -> DynamicTableResult:
        table = reader.read(params.original, params.chosen)
        rows = tuple(
            TableRow(
                output=DynamicOutput(
                    {**row.output.root, "again": row.output.root["markers"]}
                ),
                sources=row.sources,
                parent=row.parent,
            )
            for row in table.rows
        )
        return DynamicTableResult(
            schema=(*table.schema, TableColumn("again", "timeline_points")), rows=rows
        )

    bound = _custom_bound(
        produce,
        seeded,
        names={
            "transcript": "Excerpt",
            "markers": "First markers",
            "again": "Second markers",
        },
    )
    result = run_typed_create_sheet_action(project, "project-test", bound)
    assert result.status == "completed", result.errors
    columns = {item.name: item for item in result.outputs if item.kind == "column"}
    first, second = columns["First markers"], columns["Second markers"]
    values = project.get_values(first.sheet_id, first.column_id)
    assert values == project.get_values(second.sheet_id, second.column_id)
    assert all(value is not None for value in values.values())
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    refs = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "temporal_annotation_projection"
    ]
    assert len(refs) == 4
    assert {ref["output"]["column_id"] for ref in refs} == {
        first.column_id,
        second.column_id,
    }
    assert {
        item["name"] for item in project.columns(first.sheet_id) if item["ai_generated"]
    } == {"Excerpt"}
    replay = run_typed_create_sheet_action(project, "project-test", bound)
    assert replay.status == "completed" and replay.receipt_id == result.receipt_id


@pytest.mark.parametrize(
    "changed", ["transcript", "annotation", "selection", "row", "column"]
)
def test_actual_reader_plan_is_revalidated_before_publication(
    source, monkeypatch, changed
):
    project, seeded = source
    annotation_column, annotation_value = _annotation(project, seeded)
    action_body = _queued_transcript_action(
        seeded, target="Stale excerpts", key="stale-excerpts"
    )
    if changed == "selection":
        action_body["params"]["selection"] = {"kind": "column", "column": "markers"}
    original = AdmittedTranscriptReader.close
    after_edit = []

    def close(reader):
        original(reader)
        if changed == "transcript":
            project.apply_edits(
                [
                    {
                        "row_id": seeded["row_id"],
                        "column_id": seeded["transcript_column_id"],
                        "value": "Changed source text",
                    }
                ],
                label="source edit",
            )
        elif changed in {"annotation", "selection"}:
            project.apply_edits(
                [
                    {
                        "row_id": seeded["row_id"],
                        "column_id": annotation_column,
                        "value": {
                            **annotation_value,
                            "items": [{"id": "changed", "at_ms": 3500}],
                        },
                    }
                ],
                label="annotation edit",
            )
        elif changed == "row":
            project.db.execute(
                "UPDATE rows SET hidden=1 WHERE id=?", (seeded["row_id"],)
            )
            project.db.commit()
        else:
            project.db.execute(
                "UPDATE columns SET hidden=1 WHERE id=?",
                (seeded["transcript_column_id"],),
            )
            project.db.commit()
        after_edit.append(_counts(project))

    monkeypatch.setattr(AdmittedTranscriptReader, "close", close)
    result = run_action_spec(project, action_body, project_id="project-test")
    assert result.status == "failed", result
    assert result.errors[0].code == "stale_input"
    assert _counts(project) == after_edit[0]


def test_queue_reads_current_selection_not_an_enqueue_output_snapshot(source):
    project, seeded = source
    column, value = _annotation(project, seeded, name="selection")
    body = _queued_transcript_action(
        seeded, target="Current excerpts", key="current-excerpts"
    )
    body["params"]["selection"] = {"kind": "column", "column": "selection"}
    envelope = reserve_typed_action_job(
        project, "project-test", typed_action_for_request(body)
    )
    project.apply_edits(
        [
            {
                "row_id": seeded["row_id"],
                "column_id": column,
                "value": {**value, "items": [{"id": "later", "at_ms": 5000}]},
            }
        ],
        label="new selection before worker",
    )
    result = run_action_run_job(
        project,
        {"action_job": envelope.to_json()},
        executor_lookup=action_job_bindings().get,
    )
    assert result.status == "completed", result.errors
    transcript = next(item for item in result.outputs if item.name == "transcript")
    assert list(
        project.get_values(transcript.sheet_id, transcript.column_id).values()
    ) == ["Shared chunk. Final chunk.", "Final chunk."]


def test_preview_and_refresh_stay_unsupported_and_undo_keeps_navigation_honest(
    source, monkeypatch
):
    from frisket.engine.executor.actions import resolve_map_preview

    project, seeded = source
    body = _queued_transcript_action(
        seeded, target="Unsupported modes", key="unsupported-modes"
    )
    before = _counts(project)
    error = resolve_map_preview(project, body)
    assert error.code == "unsupported_action_kind"
    assert _counts(project) == before
    result = run_action_spec(project, body, project_id="project-test")
    assert result.status == "completed", result.errors
    child = next(item.sheet_id for item in result.outputs if item.kind == "sheet")
    children = project.db.execute(
        "SELECT parent_row_id FROM rows WHERE sheet_id=? ORDER BY id", (child,)
    ).fetchall()
    assert [row[0] for row in children] == [seeded["row_id"], seeded["row_id"]]
    refresh = run_action_spec(
        project,
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": child},
            "idempotency_key": "no-transcript-refresh",
        },
        project_id="project-test",
    )
    assert (
        refresh.status == "failed" and refresh.errors[0].code == "refresh_unsupported"
    )
    assert project.undo() == result.op_ids[0]
    replay = run_action_spec(project, body, project_id="project-test")
    assert replay.status == "failed" and replay.errors[0].code == "stale_replay"


def test_completion_cas_loss_rolls_back_projection_and_keeps_reservation(
    source, monkeypatch
):
    from frisket.engine.executor.action_jobs import ActionJobTerminalizationError

    project, seeded = source
    bound = typed_action_for_request(
        _queued_transcript_action(seeded, target="CAS excerpts", key="cas-excerpts")
    )
    envelope = reserve_typed_action_job(project, "project-test", bound)
    _mark_action_job_receipt_running(
        project, project_id="project-test", receipt_id=envelope.receipt_id, job_id=None
    )
    before = _counts(project)
    prior = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    monkeypatch.setattr(
        ReceiptStore, "update_body_status", lambda *_args, **_kwargs: False
    )
    with pytest.raises(ActionJobTerminalizationError, match="completion did not land"):
        run_typed_table_action_job(project, envelope)
    assert _counts(project) == before
    assert ReceiptStore(project).parsed_by_id(envelope.receipt_id) == prior

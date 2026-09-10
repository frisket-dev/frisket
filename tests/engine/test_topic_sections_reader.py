from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.geospatial_types import Geocoder
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.temporal_finders import TranscriptColumn
from frisket.actions.temporal_types import TopicSectionsReader
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    EngineRef,
    Outcome,
    Row,
    RowError,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import _default_map_runner_factory, run_action_spec
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    run_typed_map_rows_action,
)
from frisket.engine.executor.topic_sections_read import AdmittedTopicSectionsReader
from frisket.engine.executor.temporal_finder_provenance import (
    resolve_topic_analysis_sidecar,
    resolve_topic_section_unit_ids,
)
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.features.temporal_values import TimelineRangesValue
from frisket.features.topic_segmentation import engines
from frisket.features.topic_segmentation.contracts import SegmentationExecutionError
from frisket.ops.base import OpContext
from tests.engine.test_temporal_finder_actions import (
    _ExactTopicSegmenter,
    _seed_timestamped_transcripts,
)


class RenamedParams(ActionParams):
    dialogue: TranscriptColumn
    method: EngineRef[TopicSectionsReader] = EngineRef[TopicSectionsReader](
        "texttiling"
    )


class AlternateOutput(BaseModel):
    first: TimelineRangesValue
    second: TimelineRangesValue


class RoutedEngineParams(ActionParams):
    dialogue: TranscriptColumn
    method: EngineRef[Geocoder] = EngineRef[Geocoder]("nominatim")


async def mixed_local_engine(
    params: RenamedParams, row: Row, reader: TopicSectionsReader, geocoder: Geocoder
) -> RowResult[AlternateOutput]:
    return await alternate(params, row, reader)


async def mixed_routed_engine(
    params: RoutedEngineParams,
    row: Row,
    reader: TopicSectionsReader,
    geocoder: Geocoder,
) -> RowResult[AlternateOutput]:
    value = await reader.read(
        row, params.dialogue, engine="texttiling", settings={"detail": "more"}
    )
    return RowResult(output=AlternateOutput(first=value, second=value))


async def alternate(
    params: RenamedParams, row: Row, reader: TopicSectionsReader
) -> RowResult[AlternateOutput]:
    first = await reader.read(
        row, params.dialogue, engine=params.method.root, settings={"detail": "more"}
    )
    return RowResult(output=AlternateOutput(first=first, second=first))


async def wrong_engine(
    params: RenamedParams, row: Row, reader: TopicSectionsReader
) -> RowResult[AlternateOutput]:
    value = await reader.read(
        row, params.dialogue, engine="deep_tiling", settings={"detail": "more"}
    )
    return RowResult(output=AlternateOutput(first=value, second=value))


class DynamicParams(RenamedParams):
    key: str = "sections"


async def dynamic_topic(
    params: DynamicParams, row: Row, reader: TopicSectionsReader
) -> RowResult[DynamicOutput]:
    value = await reader.read(
        row, params.dialogue, engine=params.method.root, settings={"detail": "more"}
    )
    return RowResult(output=DynamicOutput({params.key: value}))


@pytest.fixture
def source(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "topics.frisket")
    sheet, rows, column, _ = _seed_timestamped_transcripts(project)
    engine = _ExactTopicSegmenter()
    monkeypatch.setattr(engines, "get_segmenter", lambda _engine: engine)
    yield project, sheet, rows, column, engine
    project.close()


def _reader(source, **kwargs):
    project, sheet, rows, column, _ = source
    values, refs = project.get_values_with_refs(sheet, column, row_ids=[rows[0]])
    row = Row({"transcript": values[rows[0]]})
    owner = AdmittedTopicSectionsReader(project, **kwargs)
    bound = owner.bind_row(
        row,
        sheet_id=sheet,
        row_id=rows[0],
        sources={
            "transcript": {
                "column_id": column,
                "value": values[rows[0]],
                "value_ref": refs[rows[0]],
            }
        },
    )
    return owner, bound, row


def _custom(source, handler=alternate):
    _, sheet, rows, _, _ = source
    registered = RegisteredAction(
        "custom.topics",
        action(
            name="topics",
            title="Custom topics",
            description="Renamed inputs and multiple outputs.",
            category=ActionCategory.EXTRACT,
            run=map_rows(handler),
        ),
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope=SheetRows(sheet_id=sheet, row_ids=[rows[0]]),
        params={"dialogue": "transcript", "method": "texttiling"},
        output_names={"first": "First renamed", "second": "Second renamed"},
        idempotency_key="custom-topics",
    )
    return BoundTypedActionRequest.bind(registered, request)


def _body(source):
    _, sheet, rows, _, _ = source
    return {
        "action_id": "map.find_topic_sections",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
        "params": {
            "source": "transcript",
            "engine": "texttiling",
            "settings": {"detail": "more"},
        },
        "output_names": {"sections": "Topics"},
        "idempotency_key": "topics",
    }


def test_renamed_params_multiple_outputs_keep_exact_unit_and_column_association(source):
    project, sheet, rows, column, engine = source
    bound = _custom(source)
    result = run_typed_map_rows_action(
        project, "topics", bound, None, _default_map_runner_factory
    )
    assert result.status == "completed", result.errors
    assert len(result.outputs) == 2
    assert engine.segment_calls == 1
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert (
        len(
            [
                item
                for item in receipt.evidence
                if item.ref.get("kind") == "typed_hidden_output"
            ]
        )
        == 2
    )
    for name in ("First renamed", "Second renamed"):
        output = next(c for c in project.columns(sheet) if c["name"] == name)
        resolved = resolve_topic_analysis_sidecar(
            project, sheet_id=sheet, row_id=rows[0], selection_column_id=output["id"]
        )
        assert resolved is not None
        assert resolved.payload["transcript_column_id"] == column
        assert resolved.payload["range_column_id"] == output["id"]
        value = project.get_values(sheet, output["id"], row_ids=[rows[0]])[rows[0]]
        assert resolve_topic_section_unit_ids(resolved, value)


def test_renamed_engine_selection_cannot_be_overridden_by_handler(source):
    project, _, _, _, engine = source
    result = run_typed_map_rows_action(
        project,
        "topics",
        _custom(source, wrong_engine),
        None,
        _default_map_runner_factory,
    )
    assert result.status == "failed"
    assert engine.segment_calls == 0
    errors = project.db.execute(
        "SELECT error_code FROM results WHERE run_id=?", (result.run_id,)
    ).fetchall()
    assert errors and all(row[0] == "invalid_engine" for row in errors)


@pytest.mark.parametrize(
    "handler,engine,routed_engine,topic_engine",
    [
        (mixed_local_engine, "texttiling", None, "texttiling"),
        (mixed_routed_engine, "nominatim", "nominatim", None),
    ],
)
def test_mixed_capabilities_bind_engine_only_to_declared_target(
    source, handler, engine, routed_engine, topic_engine
):
    project, sheet, rows, column, _ = source
    original = _custom(source)
    registered = replace(
        original.action,
        definition=replace(original.action.definition, run=map_rows(handler)),
    )
    request = original.request.model_copy(
        update={"params": {"dialogue": "transcript", "method": engine}}
    )
    plan = build_typed_map_rows_plan(
        project, BoundTypedActionRequest.bind(registered, request)
    )
    assert plan.spec_dict().get("engine") == routed_engine
    values, refs = project.get_values_with_refs(sheet, column, row_ids=[rows[0]])
    row = Row({"transcript": values[rows[0]]})

    async def check():
        async with plan.program.execution_scope(
            plan.spec_dict(), OpContext(project=project), expected_rows=1
        ):
            owner = plan.program._capability_bindings.get()[0]
            assert owner._engine == topic_engine
            reader = owner.bind_row(
                row,
                sheet_id=sheet,
                row_id=rows[0],
                sources={
                    "transcript": {
                        "column_id": column,
                        "value": values[rows[0]],
                        "value_ref": refs[rows[0]],
                    }
                },
            )
            result = await reader.read(
                row,
                TranscriptColumn("transcript"),
                engine="texttiling",
                settings={"detail": "more"},
            )
            assert result.items

    asyncio.run(check())


def test_dynamic_output_has_only_authored_keys_but_publishes_host_sidecar(source):
    project, sheet, rows, _, _ = source
    registered = RegisteredAction(
        "custom.dynamic_topics",
        action(
            name="dynamic_topics",
            title="Dynamic topics",
            description="Params-defined output name.",
            category=ActionCategory.EXTRACT,
            run=map_rows(
                dynamic_topic,
                dynamic_outputs=lambda params: {params.key: TimelineRangesValue},
            ),
        ),
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope=SheetRows(sheet_id=sheet, row_ids=[rows[0]]),
        params={"dialogue": "transcript", "key": "summary"},
        output_names={"summary": "Summary"},
        idempotency_key="dynamic-topic",
    )
    result = run_typed_map_rows_action(
        project,
        "topics",
        BoundTypedActionRequest.bind(registered, request),
        None,
        _default_map_runner_factory,
    )
    assert result.status == "completed", result.errors
    assert len(result.outputs) == 1
    assert (
        resolve_topic_analysis_sidecar(
            project,
            sheet_id=sheet,
            row_id=rows[0],
            selection_column_id=result.outputs[0].ref["column_id"],
        )
        is not None
    )


def test_outcome_absence_and_failure_do_not_invent_analysis(source):
    owner, reader, _ = _reader(source)
    fields = ACTION_REGISTRY.get("map.find_topic_sections").definition.run.output_fields
    for value in (None, Outcome.failed("no_sections", "No sections")):
        output = type("Output", (), {"sections": value})()
        cells = reader.publication_cells(
            output, fields, {"sections": "Topics"}, {"Topics": 999}
        )
        cell = next(iter(cells.values()))
        assert cell["value"] is None
        if value is not None:
            assert cell["error_code"] == "no_sections"
    asyncio.run(owner.aclose())


def test_preview_is_read_only_and_retains_hidden_analysis_flags(source):
    project, _, rows, _, _ = source
    bound = _custom(source)
    plan = build_typed_map_rows_plan(project, bound)
    before = tuple(project.db.iterdump())
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
        authority=UnroutedOnlyAuthority(project),
    )
    preview = asyncio.run(runner.preview(plan.spec_dict(), program=plan.program))
    assert not preview.values[rows[0]]["First renamed"].get("error")
    assert [column.name for column in preview.columns if not column.hidden] == [
        "First renamed",
        "Second renamed",
    ]
    assert len([column for column in preview.columns if column.hidden]) == 2
    assert tuple(project.db.iterdump()) == before


@pytest.mark.parametrize("forgery", ["copy", "mutate", "foreign"])
def test_only_invocation_owned_unchanged_results_receive_sidecars(source, forgery):
    owner, reader, row = _reader(source)

    async def check():
        value = await reader.read(
            row,
            TranscriptColumn("transcript"),
            engine="texttiling",
            settings={"detail": "more"},
        )
        if forgery == "copy":
            value = value.model_copy(deep=True)
        elif forgery == "mutate":
            value.items[0].label = "forged"
        else:
            other_owner, other, other_row = _reader(source)
            value = await other.read(
                other_row,
                TranscriptColumn("transcript"),
                engine="texttiling",
                settings={"detail": "more"},
            )
            await other_owner.aclose()
        fields = ACTION_REGISTRY.get(
            "map.find_topic_sections"
        ).definition.run.output_fields
        with pytest.raises(RowError, match="retain their admitted"):
            reader.publication_cells(
                type("Output", (), {"sections": value})(),
                fields,
                {"sections": "Topics"},
                {"Topics": 999},
            )
        await owner.aclose()

    asyncio.run(check())


def test_foreign_row_and_nonadmitted_column_are_refused(source):
    owner, reader, row = _reader(source)

    async def check():
        for candidate, column in (
            (Row(dict(row.values)), "transcript"),
            (row, "video"),
        ):
            with pytest.raises(RowError):
                await reader.read(
                    candidate,
                    TranscriptColumn(column),
                    engine="texttiling",
                    settings={"detail": "more"},
                )
        await owner.aclose()

    asyncio.run(check())


def test_unavailable_selected_engine_refuses_without_fallback(source):
    _, _, _, _, engine = source
    engine.definition = replace(
        engine.definition, available=False, error="selected dependency absent"
    )
    owner, reader, row = _reader(source, engine="texttiling")

    async def check():
        with pytest.raises(RowError, match="selected dependency absent") as failed:
            await reader.read(
                row,
                TranscriptColumn("transcript"),
                engine="texttiling",
                settings={"detail": "more"},
            )
        assert failed.value.code == "engine_unavailable"
        assert engine.segment_calls == 0
        await owner.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("change", ["source", "cancel"])
def test_source_and_cancellation_are_rechecked_after_read_before_publication(
    source, change
):
    project, _, _, _, _ = source
    cancelled = Event()
    owner, reader, row = _reader(source, cancelled=cancelled.is_set)

    async def check():
        value = await reader.read(
            row,
            TranscriptColumn("transcript"),
            engine="texttiling",
            settings={"detail": "more"},
        )
        if change == "cancel":
            cancelled.set()
        else:
            project.db.execute("UPDATE evidence_links SET status='retracted'")
            project.db.commit()
        fields = ACTION_REGISTRY.get(
            "map.find_topic_sections"
        ).definition.run.output_fields
        with pytest.raises(asyncio.CancelledError if change == "cancel" else RowError):
            reader.publication_cells(
                type("Output", (), {"sections": value})(),
                fields,
                {"sections": "Topics"},
                {"Topics": 999},
            )
        await owner.aclose()

    asyncio.run(check())


def test_cancellation_signals_and_settles_inflight_local_engine(source, monkeypatch):
    _, _, _, _, engine = source
    started, stopped = Event(), Event()

    def segment(snapshot, settings, context):
        started.set()
        try:
            while not stopped.wait(0.01):
                context.raise_if_cancelled()
        finally:
            stopped.set()

    monkeypatch.setattr(engine, "segment", segment)
    owner, reader, row = _reader(source)

    async def check():
        task = asyncio.create_task(
            reader.read(
                row,
                TranscriptColumn("transcript"),
                engine="texttiling",
                settings={"detail": "more"},
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stopped.is_set()
        assert not owner._reads
        await owner.aclose()

    asyncio.run(check())


def test_hidden_output_renaming_is_not_public_authority(source):
    bound = _custom(source)
    hidden = next(field.key for field in bound.output_fields if field.hidden)
    with pytest.raises(ValueError, match="unknown output names"):
        BoundTypedActionRequest.bind(
            bound.action,
            bound.request.model_copy(update={"output_names": {hidden: "Visible"}}),
        )


def test_replay_checks_hidden_integrity_without_reexecuting_engine(source):
    project, _, _, _, engine = source
    body = _body(source)
    result = run_action_spec(project, body, project_id="topics")
    assert result.status == "completed", result.errors
    replay = run_action_spec(project, body, project_id="topics")
    assert replay.receipt_id == result.receipt_id
    assert replay.status == "completed"
    assert engine.segment_calls == 1
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    hidden = next(
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "typed_hidden_output"
    )
    project.db.execute(
        "UPDATE columns SET name='tampered_sidecar' WHERE id=?",
        (hidden["column_id"],),
    )
    project.db.commit()
    refused = run_action_spec(project, body, project_id="topics")
    assert refused.status == "failed"
    assert engine.segment_calls == 1


def test_typed_ranges_feed_existing_transcript_split_with_unit_provenance(source):
    project, sheet, rows, _, _ = source
    found = run_action_spec(project, _body(source), project_id="topics")
    assert found.status == "completed", found.errors
    split = run_action_spec(
        project,
        {
            "action_id": "derive.transcript_segments",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
            "params": {
                "source": "transcript",
                "selection": {"kind": "column", "column": "Topics"},
            },
            "sheet_name": "Topic excerpts",
            "idempotency_key": "split-typed-topics",
        },
        project_id="topics",
    )
    assert split.status == "completed", split.errors
    receipt = ReceiptStore(project).parsed_by_id(split.receipt_id)
    assert any(
        item.ref.get("topic_analysis", {}).get("kind") == "topic_analysis_snapshot"
        for item in receipt.inputs
    )
    child = project.db.execute(
        "SELECT id FROM sheets WHERE name='Topic excerpts'"
    ).fetchone()[0]
    column = next(
        column for column in project.columns(child) if column["name"] == "transcript"
    )
    assert list(project.get_values(child, column["id"]).values()) == [
        "First subject.",
        "Second subject.",
    ]


def test_backfill_recovery_preserves_sidecar_and_successor_receipt(source, monkeypatch):
    project, sheet, rows, _, engine = source
    real = engine.segment

    def fail(*args):
        raise SegmentationExecutionError("temporary local failure")

    monkeypatch.setattr(engine, "segment", fail)
    result = run_action_spec(project, _body(source), project_id="topics")
    assert result.status == "failed"
    monkeypatch.setattr(engine, "segment", real)
    backfill_body = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"column": "Topics"},
        "idempotency_key": "topic-backfill",
    }
    backfill = run_action_spec(project, backfill_body, project_id="topics")
    assert backfill.status in {"completed", "partial"}, backfill.errors
    receipt = ReceiptStore(project).parsed_by_id(backfill.receipt_id)
    assert any(
        item.ref.get("kind") == "topic_segmentation_analysis"
        for item in receipt.evidence
    )
    assert any(
        item.ref.get("kind") == "typed_hidden_output" for item in receipt.evidence
    )
    output = next(c for c in project.columns(sheet) if c["name"] == "Topics")
    resolved = resolve_topic_analysis_sidecar(
        project, sheet_id=sheet, row_id=rows[0], selection_column_id=output["id"]
    )
    assert resolved is not None
    assert resolved.run_id == backfill.run_id

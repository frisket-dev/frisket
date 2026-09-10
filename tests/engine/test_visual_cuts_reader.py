from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel

import frisket.engine.executor.visual_cuts_read as reader_module
from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.temporal_finders import VideoColumn
from frisket.actions.temporal_types import VisualCutsReader
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Row,
    RowError,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import MapRunner
from frisket.engine.executor.visual_cuts_read import AdmittedVisualCutsReader
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    ensure_media_timeline,
    map_range_to_root,
    resolve_timeline,
    validate_timeline_anchor,
    write_rate1_timeline_segment,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.execution.provider import ExecutionLimitExceeded, ExecutionLimits
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.features.temporal_values import TimelinePointsValue


class AlternateVisualParams(ActionParams):
    clip: VideoColumn


class AlternateVisualOutput(BaseModel):
    highlights: TimelinePointsValue


async def alternate_visual_handler(
    params: AlternateVisualParams, row: Row, reader: VisualCutsReader
) -> RowResult[AlternateVisualOutput]:
    return RowResult(
        output=AlternateVisualOutput(highlights=await reader.read(row, params.clip))
    )


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "cuts.frisket")
    sheet = project.add_sheet("Media")
    columns = {
        name: project.add_column(sheet, name, "video") for name in ("video", "copy")
    }
    digest = project.add_blob(
        b"same video bytes",
        filename="video.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"kind": "video", "duration_seconds": 10.0}
        ),
    )
    rows = project.add_rows(
        sheet,
        [
            {name: media_cell(digest, mime="video/mp4") for name in columns}
            for _ in range(2)
        ],
        columns,
    )
    try:
        yield project, sheet, columns, rows, digest
    finally:
        project.close()


def _bind(owner, source, index=0):
    project, sheet, columns, rows, _ = source
    sources = {}
    for name, column in columns.items():
        values, refs = project.get_values_with_refs(
            sheet, column, row_ids=[rows[index]]
        )
        sources[name] = {
            "column_id": column,
            "value": values[rows[index]],
            "value_ref": refs[rows[index]],
        }
    row = Row({name: cell["value"] for name, cell in sources.items()})
    return row, owner.bind_row(row, sheet_id=sheet, row_id=rows[index], sources=sources)


def _cuts(_path, *, timeline):
    return {
        "schema_version": "frisket.timeline_points.v1",
        "timeline": timeline,
        "items": [
            {
                "id": "visual-cut-0000000037",
                "at_ms": 1235,
                "metadata": {"frame": 37, "native_timecode": "00:00:01.235"},
            }
        ],
    }


def test_custom_action_renames_params_and_outputs_without_losing_source_binding(
    source, monkeypatch
):
    project, sheet, _, rows, _ = source
    monkeypatch.setattr(reader_module, "visual_cuts_value", _cuts)
    registered = ActionRegistry(
        [
            ActionNamespace(
                "custom",
                actions=(
                    action(
                        name="cuts",
                        title="Custom cuts",
                        description="Read the selected video through a renamed parameter.",
                        category=ActionCategory.EXTRACT,
                        run=map_rows(alternate_visual_handler),
                    ),
                ),
            ),
        ]
    ).get("custom.cuts")
    request = ActionRequest(
        action_id="custom.cuts",
        scope=SheetRows(sheet_id=sheet, row_ids=rows),
        params={"clip": "video"},
        output_names={"highlights": "Scene changes"},
        idempotency_key="custom-cuts-preview",
    )
    plan = build_typed_map_rows_plan(
        project, BoundTypedActionRequest.bind(registered, request)
    )
    before = tuple(project.db.iterdump())
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )
    preview = asyncio.run(runner.preview(plan.spec_dict(), program=plan.program))
    anchors = []
    for row_id in rows:
        cell = preview.values[row_id]["Scene changes"]
        assert not cell.get("error")
        value = cell["value"]
        assert value["items"][0]["at_ms"] == 1235
        anchors.append(value["timeline"]["artifact_stable_id"])
    assert len(set(anchors)) == len(rows)
    assert tuple(project.db.iterdump()) == before


def test_first_preview_has_cell_scoped_ephemeral_identity_without_writes(
    source, monkeypatch
):
    project, _, _, _, _ = source
    monkeypatch.setattr(reader_module, "visual_cuts_value", _cuts)
    before = tuple(project.db.iterdump())

    async def scenario():
        reader = AdmittedVisualCutsReader(project, preview=True)
        values = []
        try:
            for index in range(2):
                row, bound = _bind(reader, source, index)
                for name in ("video", "copy"):
                    value = await bound.read(row, ColumnRef(name))
                    assert await bound.read(row, ColumnRef(name)) == value
                    values.append(value)
        finally:
            await reader.aclose()
        assert reader.closed
        return values

    values = asyncio.run(scenario())
    assert len({value.timeline.artifact_stable_id for value in values}) == 4
    assert len({value.timeline.fingerprint for value in values}) == 1
    for value in values:
        assert value.items[0].at_ms == 1235
        assert value.items[0].metadata["frame"] == 37
        with pytest.raises(TimelineError):
            validate_timeline_anchor(project, value.timeline.model_dump(mode="json"))
    assert tuple(project.db.iterdump()) == before
    next_values = asyncio.run(scenario())
    assert {value.timeline.artifact_stable_id for value in values}.isdisjoint(
        value.timeline.artifact_stable_id for value in next_values
    )


def test_durable_anchor_is_resolvable_and_source_bound(source, monkeypatch):
    project, sheet, columns, rows, _ = source
    monkeypatch.setattr(reader_module, "visual_cuts_value", _cuts)

    async def scenario():
        reader = AdmittedVisualCutsReader(project)
        row, bound = _bind(reader, source)
        try:
            return await bound.read(row, ColumnRef("video"))
        finally:
            await reader.aclose()

    value = asyncio.run(scenario())
    lease = resolve_timeline(
        project, sheet_id=sheet, row_id=rows[0], column_id=columns["video"]
    )
    assert value.timeline.model_dump(mode="json") == lease.anchor.wire_value()
    assert validate_timeline_anchor(project, lease.anchor.wire_value()) == lease.anchor


def _mapped_clip(source):
    project, sheet, columns, rows, digest = source
    root_digest = project.add_blob(b"root video", filename="root.mp4", mime="video/mp4")
    root = ensure_media_timeline(project, blob_hash=root_digest, duration_ms=60_000)
    # An older raw cell anchor must not override the later derived identity.
    resolve_timeline(
        project, sheet_id=sheet, row_id=rows[0], column_id=columns["video"]
    )
    clip = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="video/mp4",
        blob_hash=digest,
        duration_ms=10_000,
        source_sheet_id=sheet,
        source_row_id=rows[0],
        source_column_id=columns["video"],
    )
    write_rate1_timeline_segment(
        project,
        derived_artifact_id=clip["id"],
        source_artifact_id=root.artifact_id,
        source_start_ms=20_000,
        source_end_ms=30_000,
        precision="frame_accurate",
    )
    return root, clip


@pytest.mark.parametrize("preview", [False, True])
def test_derived_clip_preserves_local_frame_and_mapped_anchor(
    source, monkeypatch, preview
):
    project, _, _, _, _ = source
    root, clip = _mapped_clip(source)
    monkeypatch.setattr(reader_module, "visual_cuts_value", _cuts)
    before = tuple(project.db.iterdump())

    async def scenario():
        reader = AdmittedVisualCutsReader(project, preview=preview)
        row, bound = _bind(reader, source)
        try:
            return await bound.read(row, ColumnRef("video"))
        finally:
            await reader.aclose()

    value = asyncio.run(scenario())
    assert value.timeline.artifact_stable_id == clip["stable_id"]
    assert value.items[0].at_ms == 1235
    assert value.items[0].metadata["frame"] == 37
    mapped = map_range_to_root(
        project, artifact_id=clip["id"], start_ms=1235, end_ms=2000
    )
    assert mapped.root_artifact_id == root.artifact_id
    assert mapped.root_start_ms == 21_235
    assert tuple(project.db.iterdump()) == before


@pytest.mark.parametrize("corruption", ["ambiguous", "stale"])
def test_preview_refuses_invalid_derived_lineage_before_fallback(
    source, monkeypatch, corruption
):
    project, sheet, columns, rows, digest = source
    root, clip = _mapped_clip(source)
    if corruption == "ambiguous":
        other = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=digest,
            duration_ms=10_000,
            source_sheet_id=sheet,
            source_row_id=rows[0],
            source_column_id=columns["video"],
        )
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=other["id"],
            source_artifact_id=root.artifact_id,
            source_start_ms=30_000,
            source_end_ms=40_000,
            precision="frame_accurate",
        )
    else:
        project.db.execute(
            "UPDATE source_artifacts SET duration_ms=25000 WHERE id=?",
            (root.artifact_id,),
        )
        project.db.commit()
    monkeypatch.setattr(
        reader_module,
        "visual_cuts_value",
        lambda *_args, **_kwargs: pytest.fail("detector ran on invalid lineage"),
    )
    before = tuple(project.db.iterdump())

    async def scenario():
        reader = AdmittedVisualCutsReader(project, preview=True)
        row, bound = _bind(reader, source)
        try:
            with pytest.raises(RowError):
                await bound.read(row, ColumnRef("video"))
        finally:
            await reader.aclose()

    asyncio.run(scenario())
    assert tuple(project.db.iterdump()) == before


def test_bound_reader_rejects_copied_row_mutation_and_unadmitted_sources(
    source, monkeypatch
):
    project, _, _, _, _ = source
    monkeypatch.setattr(reader_module, "visual_cuts_value", _cuts)

    async def scenario():
        reader = AdmittedVisualCutsReader(project, preview=True)
        row, bound = _bind(reader, source)
        monkeypatch.setattr(
            project,
            "get_values_with_refs",
            lambda *_args, **_kwargs: pytest.fail("silently reread the live cell"),
        )
        try:
            with pytest.raises(RowError, match="admitted row"):
                await bound.read(Row(row.values), ColumnRef("video"))
            with pytest.raises(RowError):
                await bound.read(row, ColumnRef("unadmitted"))
            await bound.read(row, ColumnRef("video"))
            row.values["video"]["blob"] = "0" * 64
            with pytest.raises(RowError, match="differs"):
                await bound.read(row, ColumnRef("video"))
        finally:
            await reader.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await bound.read(row, ColumnRef("video"))

    asyncio.run(scenario())


@pytest.mark.parametrize("measured", [None, 100.0])
def test_limit_uses_probe_measurement_not_anchor_duration(
    source, monkeypatch, measured
):
    project, _, _, _, _ = source
    monkeypatch.setattr(
        reader_module.MediaBlobStore,
        "display_metadata",
        lambda *_args: {"kind": "video", "duration_seconds": 10.0},
    )
    monkeypatch.setattr(
        reader_module.MediaBlobStore,
        "probe_metadata",
        lambda *_args: {"duration_seconds": measured},
    )
    monkeypatch.setattr(
        reader_module,
        "visual_cuts_value",
        lambda *_args, **_kwargs: pytest.fail("detector ran beyond limit"),
    )
    before = tuple(project.db.iterdump())

    async def scenario():
        reader = AdmittedVisualCutsReader(
            project, execution_limits=ExecutionLimits(max_media_seconds=20)
        )
        row, bound = _bind(reader, source)
        try:
            with pytest.raises(ExecutionLimitExceeded, match="max_media_seconds"):
                await bound.read(row, ColumnRef("video"))
        finally:
            await reader.aclose()

    asyncio.run(scenario())
    assert tuple(project.db.iterdump()) == before


@pytest.mark.parametrize("worker_fails", [False, True])
def test_repeated_cancellation_settles_detector_before_releasing_blob(
    source, monkeypatch, worker_fails
):
    project, _, _, _, _ = source
    started, release, borrowed = threading.Event(), threading.Event(), threading.Event()
    original_materialize = project.materialize_blob

    @contextmanager
    def materialize(digest):
        with original_materialize(digest) as path:
            borrowed.set()
            try:
                yield path
            finally:
                borrowed.clear()

    def detector(path: Path, *, timeline):
        started.set()
        assert release.wait(timeout=5)
        assert borrowed.is_set() and Path(path).exists()
        if worker_fails:
            raise RuntimeError("detector failed during cancellation settlement")
        return _cuts(path, timeline=timeline)

    monkeypatch.setattr(project, "materialize_blob", materialize)
    monkeypatch.setattr(reader_module, "visual_cuts_value", detector)

    async def scenario():
        reader = AdmittedVisualCutsReader(project, preview=True)
        row, bound = _bind(reader, source)
        task = asyncio.create_task(bound.read(row, ColumnRef("video")))
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert borrowed.is_set() and not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not borrowed.is_set()
        finally:
            release.set()
            await reader.aclose()
        assert reader.closed

    asyncio.run(scenario())

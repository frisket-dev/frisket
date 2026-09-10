"""Actual-call capability ownership and atomic typed Split publication."""

from dataclasses import replace

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.temporal_types import (
    TemporalMediaColumn,
    TemporalMediaReader,
    TemporalMediaValue,
    TranscriptSelection,
)
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicTableResult,
    RowSource,
)
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.engine.store.media_splice import (
    MediaSpliceProbe,
    MediaStreamProbe,
    StagedMediaSplice,
)


class CustomParams(ActionParams):
    recording: TemporalMediaColumn
    excerpts: TranscriptSelection


@pytest.fixture
def media(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "media.frisket")
    sheet = project.add_sheet("Media")
    column = project.add_column(sheet, "recording", "video")
    blob = project.add_blob(
        b"source",
        filename="source.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(
            probe={"kind": "video", "duration_seconds": 10}
        ),
    )
    row = project.add_rows(
        sheet,
        [{"recording": {"blob": blob, "filename": "source.mp4", "mime": "video/mp4"}}],
        {"recording": column},
    )[0]
    paths = []

    async def render(_source_path, path, *, start_ms, end_ms, **kwargs):
        assert not project.db.in_transaction
        paths.append(path)
        path.write_bytes(f"{start_ms}:{end_ms}".encode())
        duration = end_ms - start_ms
        return StagedMediaSplice(
            path=path,
            filename=path.name,
            mime="video/mp4",
            media_kind="video",
            requested_start_ms=start_ms,
            requested_end_ms=end_ms,
            resolved_source_start_ms=start_ms,
            resolved_source_end_ms=end_ms,
            duration_ms=duration,
            alignment_error_ms=0,
            alignment_tolerance_ms=100,
            precision="frame_accurate",
            renderer_profile="test.renderer.v1",
            renderer_params={},
            probe=MediaSpliceProbe(
                duration_ms=duration,
                size_bytes=path.stat().st_size,
                format_name="mov,mp4",
                format_start_time="0.000000",
                bit_rate=128000,
                streams=(
                    MediaStreamProbe(
                        index=0,
                        codec_type="video",
                        codec_name="h264",
                        time_base="1/1000",
                        start_time="0.000000",
                        duration=str(duration / 1000),
                        avg_frame_rate="25/1",
                        r_frame_rate="25/1",
                        width=640,
                        height=360,
                    ),
                ),
            ),
        )

    monkeypatch.setattr(
        "frisket.engine.executor.temporal_split.stage_media_splice", render
    )
    try:
        yield project, sheet, row, paths
    finally:
        project.close()


def _run(media, handler, *, output_names=None):
    project, sheet, row, _paths = media
    registered = RegisteredAction(
        "custom.clips",
        action(
            name="clips",
            title="Clips",
            description="Read a derived temporal selection.",
            category=ActionCategory.CONVERT,
            run=create_sheet(handler),
        ),
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row]},
        params={
            "recording": "recording",
            "excerpts": {
                "kind": "draft_ranges",
                "items": [{"start_ms": 1000, "end_ms": 2500}],
            },
        },
        sheet_name="Clips",
        output_names=output_names or {},
        idempotency_key="custom-clips",
    )
    return run_typed_create_sheet_action(
        project, "test", BoundTypedActionRequest.bind(registered, request)
    )


def test_custom_params_actual_arguments_keep_clock_and_independent_names(media):
    def split(params: CustomParams, reader: TemporalMediaReader) -> DynamicTableResult:
        return reader.read(params.recording, params.excerpts)

    result = _run(
        media, split, output_names={"clip": "excerpt", "source_range": "original range"}
    )
    assert result.status == "completed", result.errors
    project, _sheet, source_row, paths = media
    sheet = next(output.sheet_id for output in result.outputs if output.kind == "sheet")
    columns = {column["name"]: column["id"] for column in project.columns(sheet)}
    assert set(columns) == {"excerpt", "original range"}
    rows = project.visible_row_ids(sheet)
    timeline = resolve_timeline(
        project, sheet_id=sheet, row_id=rows[0], column_id=columns["excerpt"]
    )
    assert timeline.anchor.duration_ms == 1500
    assert (
        project.db.execute(
            "SELECT parent_row_id FROM rows WHERE id=?", (rows[0],)
        ).fetchone()[0]
        == source_row
    )
    assert all(not path.exists() for path in paths)
    replay = _run(
        media, split, output_names={"clip": "excerpt", "source_range": "original range"}
    )
    assert replay.status == "completed"
    assert replay.receipt_id == result.receipt_id
    assert len(paths) == 1


def test_empty_custom_projection_retains_parent_without_publishing_unused_blobs(media):
    def split(params: CustomParams, reader: TemporalMediaReader) -> DynamicTableResult:
        table = reader.read(params.recording, params.excerpts)
        return replace(table, rows=())

    result = _run(media, split)
    assert result.status == "completed", result.errors
    project, source_sheet, _row, paths = media
    sheet = next(output.sheet_id for output in result.outputs if output.kind == "sheet")
    assert project.visible_row_ids(sheet) == []
    assert (
        project.db.execute(
            "SELECT parent_sheet_id FROM sheets WHERE id=?", (sheet,)
        ).fetchone()[0]
        == source_sheet
    )
    assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    assert all(not path.exists() for path in paths)


def test_changed_staged_bytes_roll_back_cells_blobs_and_lineage(media):
    project, _sheet, _row, paths = media
    original_blobs = project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]

    def split(params: CustomParams, reader: TemporalMediaReader) -> DynamicTableResult:
        table = reader.read(params.recording, params.excerpts)
        paths[0].write_bytes(b"changed after the reader froze its output value")
        return table

    result = _run(media, split)
    assert result.status == "failed"
    assert result.errors[0].code == "stale_input"
    assert [sheet["name"] for sheet in project.sheets()] == ["Media"]
    assert (
        project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == original_blobs
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0] == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM artifact_timeline_segments"
        ).fetchone()[0]
        == 0
    )
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    "change", ["foreign_parent", "wrong_type", "unadmitted_handle"]
)
def test_grounded_media_cannot_be_reparented_or_detached(media, change):
    def split(params: CustomParams, reader: TemporalMediaReader) -> DynamicTableResult:
        table = reader.read(params.recording, params.excerpts)
        row = table.rows[0]
        if change == "foreign_parent":
            forged = RowSource(sheet_id=row.parent.sheet_id, row_id=row.parent.row_id)
            row = replace(row, sources=(forged,), parent=forged)
            return replace(table, rows=(row,))
        if change == "wrong_type":
            return replace(
                table, schema=(replace(table.schema[0], type="text"), *table.schema[1:])
            )
        row.output.root["clip"] = TemporalMediaValue()
        return replace(table, rows=(row,))

    result = _run(media, split)
    assert result.status == "failed"
    project, _sheet, _row, paths = media
    assert [sheet["name"] for sheet in project.sheets()] == ["Media"]
    assert (
        project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0] == 0
    )
    assert all(not path.exists() for path in paths)

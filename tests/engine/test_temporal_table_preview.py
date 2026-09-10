"""Real typed producer previews stay bounded and leave the source project alone."""

from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.actions.system import typed_action_for_request, BoundTypedActionRequest
from frisket.actions.core import RegisteredAction, action, create_sheet, ActionCategory
from frisket.actions.temporal_types import TemporalMediaReader
from frisket.actions.temporal_segments import TemporalSegmentsParams
from frisket.actions.types import TableError, DynamicTableResult
from frisket.engine.executor.temporal_media_read import _planned_schema
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.table_preview import preview_table
from frisket.engine.executor.temporal_materialization import NormalizedRange
from frisket.engine.executor.temporal_preview import PreviewTemporalValue
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    resolve_timeline,
    ensure_media_timeline,
)
from frisket.features.temporal_values import parse_temporal_value
from tests.engine.test_temporal_split import (
    _fake_stage,
    _request,
    _seed_media,
    _run,
    _plan,
)
from tests.engine.test_temporal_transcript_inheritance import (
    _add_timestamped_transcript,
)


@pytest.fixture
def local_render(monkeypatch):
    calls = []

    async def render(_source, path, *, start_ms, end_ms, **kwargs):
        calls.append((start_ms, end_ms))
        return _fake_stage(None, None, NormalizedRange(start_ms, end_ms, "test"), path)

    monkeypatch.setattr(
        "frisket.engine.executor.temporal_split.stage_media_splice", render
    )
    monkeypatch.setattr(
        "frisket.engine.executor.import_blob_stage.probe_for_ingest",
        lambda path, **kwargs: {
            "kind": "video",
            "size_bytes": Path(path).stat().st_size,
        },
    )
    return calls


def _preview(project, request, *, cancelled=lambda: False):
    bound = typed_action_for_request(request.model_dump(mode="json"))
    return preview_table(
        project,
        "preview",
        bound,
        deps=ExecutorDeps(),
        progress=lambda *_: None,
        cancelled=cancelled,
    )


def _snapshot(project):
    return tuple(project.db.iterdump())


def test_preview_first_twenty_clips_without_source_artifacts_or_durable_values(
    tmp_path, local_render
):
    with closing(Project.create(tmp_path / "preview.frisket")) as project:
        seeded = _seed_media(project, duration_ms=30000)
        request = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [
                    {"start_ms": i * 1000, "end_ms": (i + 1) * 1000} for i in range(25)
                ],
            },
        )
        before = _snapshot(project)
        result = _preview(project, request)
        try:
            assert _snapshot(project) == before
            assert len(result.rows) == len(local_render) == 20
            assert result.total is None
            assert len(result.artifacts) == 20
            value = result.rows[0]["source_range"]["value"]
            assert isinstance(value, PreviewTemporalValue)
            wire = value.wire_value()
            assert wire["timeline"].keys() == {
                "preview_clock_id",
                "fingerprint",
                "duration_ms",
            }
            assert wire["item"]["end_ms"] == 1000
            with pytest.raises(ValueError):
                parse_temporal_value("timeline_range", wire)
            with pytest.raises(ValueError):
                parse_temporal_value(
                    "timeline_range",
                    {**wire, "schema_version": "frisket.timeline_range.v1"},
                )
            with pytest.raises(ValueError):
                TemporalSegmentsParams.model_validate(
                    {
                        "source": "video",
                        "selection": {"kind": "typed_value", "value": wire},
                    }
                )
            paths = [blob.path for blob in result.artifacts.values()]
        finally:
            result.close()
        assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("existing_source_anchor", [False, True])
def test_preview_annotation_transcript_and_schema_match_real_run(
    tmp_path, local_render, existing_source_anchor
):
    with closing(Project.create(tmp_path / "annotations.frisket")) as project:
        seeded = _seed_media(project)
        lease = (
            resolve_timeline(
                project,
                sheet_id=seeded["sheet_id"],
                row_id=seeded["row_id"],
                column_id=seeded["columns"]["video"],
            )
            if existing_source_anchor
            else SimpleNamespace(
                anchor=ensure_media_timeline(
                    project, blob_hash=seeded["blob_hash"], duration_ms=10000
                )
            )
        )
        _add_timestamped_transcript(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            artifact_id=lease.anchor.artifact_id,
            name="spoken",
            text="Hello. Later.",
            span_values=[(1000, 3000, "Hello."), (6000, 8000, "Later.")],
        )
        marker = project.add_column(seeded["sheet_id"], "markers", "timeline_points")
        project.apply_edits(
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": marker,
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": lease.anchor.wire_value(),
                        "items": [
                            {"id": "first", "at_ms": 1500, "label": "First"},
                            {"id": "later", "at_ms": 6500},
                        ],
                    },
                }
            ],
            label="markers",
        )
        request = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [
                    {"start_ms": 1000, "end_ms": 3000},
                    {"start_ms": 6000, "end_ms": 8000},
                ],
            },
        )
        before = _snapshot(project)
        preview = _preview(project, request)
        try:
            assert _snapshot(project) == before
            assert preview.total == 2
            assert [row["spoken"]["value"] for row in preview.rows] == [
                "Hello.",
                "Later.",
            ]
            assert [
                row["markers"]["value"].items["items"][0]["at_ms"]
                for row in preview.rows
            ] == [500, 500]
            result = _run(project, request, project_id="preview", stage_fn=_fake_stage)
            assert result.status == "completed", result.errors
            sheet = next(
                output.sheet_id for output in result.outputs if output.kind == "sheet"
            )
            columns = project.columns(sheet)
            assert [
                (column.name, column.column_type) for column in preview.columns
            ] == [(column["name"], column["type"]) for column in columns]
            rows = project.visible_row_ids(sheet)
            marker_column = next(
                column["id"] for column in columns if column["name"] == "markers"
            )
            actual = project.get_values(sheet, marker_column, row_ids=rows)
            assert [actual[row]["items"] for row in rows] == [
                row["markers"]["value"].items["items"] for row in preview.rows
            ]
        finally:
            preview.close()


def test_missing_duration_is_probed_without_metadata_write_and_reuses_lease(
    tmp_path, monkeypatch, local_render
):
    with closing(Project.create(tmp_path / "probe.frisket")) as project:
        seeded = _seed_media(project)
        project.db.execute("UPDATE blobs SET metadata='{}'")
        project.db.commit()
        probes, leases, released = [], [], []
        original = project.materialize_blob

        @contextmanager
        def materialize(blob):
            leases.append(blob)
            try:
                with original(blob) as path:
                    yield path
            finally:
                released.append(blob)

        async def probe(path, **kwargs):
            probes.append(path)
            return SimpleNamespace(duration_ms=10000)

        monkeypatch.setattr(project, "materialize_blob", materialize)
        monkeypatch.setattr(
            "frisket.engine.executor.temporal_preview.probe_staged_media", probe
        )
        monkeypatch.setattr(
            "frisket.engine.store.media_blobs.update_blob_metadata",
            lambda *_args, **_kwargs: pytest.fail(
                "preview must not update stored metadata"
            ),
        )
        before = _snapshot(project)
        result = _preview(
            project,
            _request(
                seeded,
                {"kind": "draft_points", "items": [{"at_ms": 3000}, {"at_ms": 7000}]},
            ),
        )
        try:
            assert len(probes) == 1
            assert leases == released == [seeded["blob_hash"]]
            assert local_render == [(0, 3000), (3000, 7000), (7000, 10000)]
            assert _snapshot(project) == before
        finally:
            result.close()


def test_shared_schema_retains_mixed_media_type_without_rendering(
    tmp_path, local_render
):
    with closing(Project.create(tmp_path / "mixed.frisket")) as project:
        seeded = _seed_media(project)
        plan = _plan(
            project,
            _request(seeded, {"kind": "draft_range", "start_ms": 0, "end_ms": 1000}),
        )
        first = plan.sources[0]
        audio = replace(
            first,
            source=replace(
                first.source, lease=replace(first.source.lease, media_kind="audio")
            ),
        )
        mixed = replace(plan, sources=(first, audio), output_count=2)
        assert _planned_schema(mixed)[0][0].type == "file"
        assert _planned_schema(plan)[0][0].type == "video"
        assert local_render == []


def test_cancel_after_one_clip_releases_lease_and_removes_scratch(
    tmp_path, monkeypatch, local_render
):
    from frisket.engine.executor import import_blob_stage, temporal_media_read

    directories = []
    original_directory = temporal_media_read.tempfile.TemporaryDirectory

    def directory(*args, **kwargs):
        result = original_directory(*args, **kwargs)
        directories.append(Path(result.name))
        return result

    monkeypatch.setattr(temporal_media_read.tempfile, "TemporaryDirectory", directory)
    assert import_blob_stage.tempfile is temporal_media_read.tempfile
    with closing(Project.create(tmp_path / "cancel.frisket")) as project:
        seeded = _seed_media(project)
        before = _snapshot(project)
        with pytest.raises(TableError) as failure:
            _preview(
                project,
                _request(
                    seeded,
                    {
                        "kind": "draft_points",
                        "items": [{"at_ms": 3000}, {"at_ms": 7000}],
                    },
                ),
                cancelled=lambda: bool(local_render),
            )
        assert failure.value.code == "action_cancelled"
        assert len(local_render) == 1
        assert _snapshot(project) == before
        assert directories and all(not path.exists() for path in directories)


def test_nullable_inherited_columns_are_frozen_from_whole_selection(
    tmp_path, local_render
):
    with closing(Project.create(tmp_path / "late-fields.frisket")) as project:
        seeded = _seed_media(project, duration_ms=30000)
        anchor = ensure_media_timeline(
            project, blob_hash=seeded["blob_hash"], duration_ms=30000
        )
        _add_timestamped_transcript(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            artifact_id=anchor.artifact_id,
            name="late speech",
            text="Later.",
            span_values=[(24000, 25000, "Later.")],
        )
        column = project.add_column(seeded["sheet_id"], "late marker", "timeline_point")
        project.apply_edits(
            [
                {
                    "row_id": seeded["row_id"],
                    "column_id": column,
                    "value": {
                        "schema_version": "frisket.timeline_point.v1",
                        "timeline": anchor.wire_value(),
                        "item": {"id": "late", "at_ms": 24500},
                    },
                }
            ],
            label="marker",
        )
        before = _snapshot(project)
        request = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [
                    {"start_ms": i * 1000, "end_ms": (i + 1) * 1000} for i in range(25)
                ],
            },
        )
        result = _preview(project, request)
        try:
            assert {column.name for column in result.columns} == {
                "clip",
                "source_range",
                "late speech",
                "late marker",
            }
            assert all(
                row["late speech"]["value"] is None
                and row["late marker"]["value"] is None
                for row in result.rows
            )
            assert len(local_render) == 20
            assert _snapshot(project) == before
        finally:
            result.close()


def test_preview_range_handle_cannot_move_to_a_different_clip(tmp_path, local_render):
    def swapped(
        params: TemporalSegmentsParams, media: TemporalMediaReader
    ) -> DynamicTableResult:
        result = media.read(params.source, params.selection)
        rows = list(result.rows)
        rows[1].output.root["source_range"] = rows[0].output.root["source_range"]
        return replace(result, rows=rows)

    registered = RegisteredAction(
        "test.temporal_preview",
        action(
            name="temporal_preview",
            title="Preview",
            description="Test role binding.",
            category=ActionCategory.CONVERT,
            run=create_sheet(swapped),
        ),
    )
    with closing(Project.create(tmp_path / "roles.frisket")) as project:
        seeded = _seed_media(project)
        request = _request(
            seeded, {"kind": "draft_points", "items": [{"at_ms": 5000}]}
        ).model_copy(update={"action_id": registered.action_id})
        bound = BoundTypedActionRequest.bind(registered, request)
        before = _snapshot(project)
        with pytest.raises(ValueError, match="exact admitted parent"):
            preview_table(
                project,
                "preview",
                bound,
                deps=ExecutorDeps(),
                progress=lambda *_: None,
                cancelled=lambda: False,
            )
        assert _snapshot(project) == before


def test_duration_probe_cancellation_is_not_reclassified_as_invalid_input(
    tmp_path, monkeypatch, local_render
):
    from frisket.engine.store.media_splice import MediaSpliceError

    async def cancelled_probe(*_args, **_kwargs):
        raise MediaSpliceError("cancelled", "probe cancelled")

    monkeypatch.setattr(
        "frisket.engine.executor.temporal_preview.probe_staged_media", cancelled_probe
    )
    with closing(Project.create(tmp_path / "cancel-probe.frisket")) as project:
        seeded = _seed_media(project)
        project.db.execute("UPDATE blobs SET metadata='{}'")
        project.db.commit()
        before = _snapshot(project)
        with pytest.raises(TableError) as failure:
            _preview(
                project,
                _request(
                    seeded, {"kind": "draft_range", "start_ms": 0, "end_ms": 1000}
                ),
            )
        assert failure.value.code == "action_cancelled"
        assert local_render == []
        assert _snapshot(project) == before

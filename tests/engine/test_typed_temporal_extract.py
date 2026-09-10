from dataclasses import replace

import pytest

from frisket.actions.core import RegisteredAction, action, ActionCategory
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.temporal_extract import EXTRACT_RANGE
from frisket.actions.temporal_extract_types import (
    PreparedTemporalExtract,
    TemporalExtractor,
)
from frisket.actions.temporal_types import TemporalMediaColumn, TranscriptRangeSelection
from frisket.actions.types import ActionParams, ActionRequest
from frisket.engine.executor.temporal_extract_action import (
    run_typed_temporal_extract_action,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from test_temporal_extract_action import _seed_two_media_rows, _fake_rendered_clip


class CustomParams(ActionParams):
    camera: TemporalMediaColumn
    offset: int


def custom_extract(
    params: CustomParams, extractor: TemporalExtractor
) -> PreparedTemporalExtract:
    return extractor.prepare(
        params.camera,
        TranscriptRangeSelection(
            kind="draft_range", start_ms=params.offset, end_ms=params.offset + 1000
        ),
    )


def forged_extract(
    params: CustomParams, extractor: TemporalExtractor
) -> PreparedTemporalExtract:
    return PreparedTemporalExtract(selected_row_count=1)


def bound_request(sheet, rows, *, custom=False, forged=False):
    definition = (
        action(
            name="extract",
            title="Custom extract",
            description="Test actual capability arguments",
            category=ActionCategory.EXTRACT,
            run=custom_extract if not forged else forged_extract,
        )
        if custom
        else EXTRACT_RANGE
    )
    return BoundTypedActionRequest.bind(
        RegisteredAction(
            "custom.extract" if custom else "temporal.extract_range", definition
        ),
        ActionRequest.model_validate(
            {
                "action_id": "custom.extract" if custom else "temporal.extract_range",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
                "params": {"camera": "video", "offset": 2000}
                if custom
                else {
                    "source": "video",
                    "selection": {
                        "kind": "draft_range",
                        "start_ms": 2000,
                        "end_ms": 5000,
                        "repeat_for_rows": len(rows) > 1,
                    },
                },
                "output_names": {"clip": "excerpt"},
                "idempotency_key": "typed-extract-test",
            }
        ),
    )


@pytest.mark.parametrize("custom", [False, True])
def test_admitted_extract_uses_actual_arguments_and_generic_output_names(
    tmp_path, monkeypatch, custom
):
    project = Project.create(tmp_path / "extract.frisket")
    try:
        sheet, _, rows, _ = _seed_two_media_rows(project)
        calls = []

        def stage(self, project, source, selected, path):
            assert not project.db.in_transaction
            calls.append((selected.start_ms, selected.end_ms))
            path.write_bytes(b"clip")
            return _fake_rendered_clip(
                path, start_ms=selected.start_ms, end_ms=selected.end_ms
            )

        monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", stage)
        bound = bound_request(sheet, rows[:1], custom=custom)
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {
                **ACTION_REGISTRY._actions,
                bound.action.action_id: bound.action,
            },
        )
        body = bound.request.model_dump(mode="json")
        result = run_action_spec(project, body, project_id="test")
        assert result.status == "completed", result
        assert result.outputs[0].name == "excerpt"
        assert calls == [(2000, 3000 if custom else 5000)]
        replay = run_action_spec(project, body, project_id="test")
        assert replay.receipt_id == result.receipt_id
        assert len(calls) == 1
    finally:
        project.close()


def test_forged_preparation_refused_before_render(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "forged.frisket")
    try:
        sheet, _, rows, _ = _seed_two_media_rows(project)

        def stage(*args):
            pytest.fail("forged handle must not render")

        monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", stage)
        result = run_typed_temporal_extract_action(
            project, "test", bound_request(sheet, rows[:1], custom=True, forged=True)
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_params"
    finally:
        project.close()


def test_second_render_failure_leaves_no_columns_or_lineage(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "atomic.frisket")
    try:
        sheet, _, rows, _ = _seed_two_media_rows(project)
        calls = []

        def stage(self, project, source, selected, path):
            calls.append(source.row_id)
            if len(calls) == 2:
                raise RuntimeError("renderer failed")
            path.write_bytes(b"clip")
            return _fake_rendered_clip(
                path, start_ms=selected.start_ms, end_ms=selected.end_ms
            )

        monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", stage)
        result = run_typed_temporal_extract_action(
            project, "test", bound_request(sheet, rows)
        )
        assert result.status == "failed"
        assert len(calls) == 2
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE sheet_id=?", (sheet,)
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_expanded_actual_cut_keeps_newly_intersecting_annotation(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "expanded.frisket")
    try:
        sheet, source_column, rows, _ = _seed_two_media_rows(project)
        source = resolve_timeline(
            project, sheet_id=sheet, row_id=rows[0], column_id=source_column
        )
        for name, position in (("markers", 1950), ("far", 7000)):
            column = project.add_column(sheet, name, type="timeline_points")
            project.apply_edits(
                [
                    {
                        "row_id": rows[0],
                        "column_id": column,
                        "value": {
                            "schema_version": "frisket.timeline_points.v1",
                            "timeline": source.anchor.wire_value(),
                            "items": [{"id": name, "at_ms": position}],
                        },
                    }
                ],
                label="seed annotations",
            )
        # A never-published sibling must not cause a false output-name collision.
        project.add_column(sheet, "clip_far", type="text")

        def stage(self, project, source, selected, path):
            path.write_bytes(b"expanded clip")
            return replace(
                _fake_rendered_clip(
                    path, start_ms=selected.start_ms, end_ms=selected.end_ms
                ),
                resolved_source_start_ms=1900,
                duration_ms=3100,
                probe={"duration_ms": 3100},
            )

        monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", stage)
        result = run_typed_temporal_extract_action(
            project, "test", bound_request(sheet, rows[:1])
        )
        assert result.status == "completed", result
        assert {output.name for output in result.outputs} == {"excerpt", "clip_markers"}
        markers = next(
            output for output in result.outputs if output.name == "clip_markers"
        )
        projected = project.get_values(sheet, markers.column_id, row_ids=rows[:1])[
            rows[0]
        ]
        assert [(item["id"], item["at_ms"]) for item in projected["items"]] == [
            ("markers", 50)
        ]
    finally:
        project.close()

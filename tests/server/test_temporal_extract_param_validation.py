from types import SimpleNamespace

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.temporal_extract_types import (
    PreparedTemporalExtract,
    TemporalExtractor,
)
from frisket.actions.temporal_types import TemporalMediaColumn, TranscriptRangeSelection
from frisket.actions.types import ActionParams
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.server.services import action_param_validation


class CameraParams(ActionParams):
    camera: TemporalMediaColumn


def extract_camera(
    params: CameraParams, extractor: TemporalExtractor
) -> PreparedTemporalExtract:
    return extractor.prepare(
        params.camera,
        TranscriptRangeSelection(kind="draft_range", start_ms=1000, end_ms=2000),
    )


@pytest.mark.parametrize("custom", [False, True])
def test_extract_form_discovers_actual_outputs_before_names_or_effects(
    tmp_path, monkeypatch, custom
):
    project = Project.create(tmp_path / "form.frisket")
    try:
        sheet = project.add_sheet("Media")
        video = project.add_column(sheet, "camera", type="video")
        blob = project.add_blob(
            b"source",
            filename="source.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(
                probe={"kind": "video", "duration_seconds": 3.0}
            ),
        )
        row = project.add_rows(sheet, [{"camera": {"blob": blob}}], {"camera": video})[
            0
        ]
        timeline = resolve_timeline(
            project, sheet_id=sheet, row_id=row, column_id=video
        )
        markers = project.add_column(sheet, "markers", type="timeline_points")
        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": markers,
                    "value": {
                        "schema_version": "frisket.timeline_points.v1",
                        "timeline": timeline.anchor.wire_value(),
                        "items": [{"id": "one", "at_ms": 1500}],
                    },
                }
            ],
            label="seed annotations",
        )
        # Discovery must still work while default names collide: the UI is about
        # to ask the user to choose the final names.
        project.add_column(sheet, "clip", type="text")
        project.add_column(sheet, "clip_markers", type="text")
        kind = "custom.camera" if custom else "temporal.extract_range"
        if custom:
            definition = action(
                name="camera",
                title="Camera",
                description="Custom source parameter",
                category=ActionCategory.EXTRACT,
                run=extract_camera,
            )
            monkeypatch.setattr(
                ACTION_REGISTRY,
                "_actions",
                {**ACTION_REGISTRY._actions, kind: RegisteredAction(kind, definition)},
            )
            monkeypatch.setattr(
                action_param_validation,
                "NEW_ACTION_IDS",
                action_param_validation.NEW_ACTION_IDS | {kind},
            )
        service = action_param_validation.ActionParamValidationService(
            SimpleNamespace(edition="local", get=lambda _id: project)
        )

        def no_render(*args, **kwargs):
            pytest.fail("form validation cannot render")

        monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", no_render)
        before = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("columns", "rows", "ops", "blobs", "receipts")
        }
        result = service.validate_params(
            "project",
            {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row]},
                "params": {"camera": "camera"}
                if custom
                else {
                    "source": "camera",
                    "selection": {
                        "kind": "draft_range",
                        "start_ms": 1000,
                        "end_ms": 2000,
                    },
                },
            },
        )
        assert result["diagnostics"] == {}
        assert result["logical_outputs"] == [
            {"key": "clip", "column_type": "video"},
            {"key": "clip_markers", "column_type": "timeline_points"},
        ]
        assert before == {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    finally:
        project.close()


@pytest.mark.parametrize("media_kind", ["audio", "video"])
def test_generic_file_media_is_refused_by_discovery_and_execution(
    tmp_path, monkeypatch, media_kind
):
    project = Project.create(tmp_path / "generic-file.frisket")
    try:
        sheet = project.add_sheet("Files")
        column = project.add_column(sheet, "media", type="file")
        blob = project.add_blob(
            b"media",
            filename="source.bin",
            mime=f"{media_kind}/mp4",
            metadata=owned_media_metadata_document(
                probe={"kind": media_kind, "duration_seconds": 3.0}
            ),
        )
        row = project.add_rows(sheet, [{"media": {"blob": blob}}], {"media": column})[0]
        request = {
            "action_id": "temporal.extract_range",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row]},
            "params": {
                "source": "media",
                "selection": {"kind": "draft_range", "start_ms": 1000, "end_ms": 2000},
            },
            "idempotency_key": "unsupported-file",
        }
        service = action_param_validation.ActionParamValidationService(
            SimpleNamespace(edition="local", get=lambda _id: project)
        )

        def no_render(*args, **kwargs):
            pytest.fail("unsupported source must not render")

        monkeypatch.setattr(CoreTemporalMediaMaterializer, "stage", no_render)
        discovery = service.validate_params("project", request)
        assert discovery["logical_outputs"] == []
        assert discovery["diagnostics"]
        assert all(not value["ok"] for value in discovery["diagnostics"].values())
        execution = run_action_spec(project, request, project_id="project")
        assert execution.status == "failed"
        assert execution.errors[0].code == "invalid_input_ref"
        assert execution.receipt_id is None
        assert len(project.columns(sheet)) == 1
    finally:
        project.close()

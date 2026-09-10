"""Runtime source admission follows the same semantic types as authoring."""

import pytest

from frisket.actions.temporal_types import TemporalMediaColumn, TemporalSelectionColumn
from frisket.actions.transcript_types import TranscriptColumn, TranscriptColumnSelection
from frisket.actions.types import SheetRows
from frisket.contracts.action import ActionError
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.temporal_materialization import (
    CoreTemporalMediaMaterializer,
)
from frisket.engine.executor.transcript_read import resolve_transcript_segments_plan
from frisket.engine.store import Project


@pytest.mark.parametrize("kind", ["temporal.extract_range", "derive.temporal_segments"])
@pytest.mark.parametrize("narrowed", ["source", "selection"])
def test_temporal_host_reads_semantic_source_authority(
    tmp_path, monkeypatch, kind, narrowed
):
    project = Project.create(tmp_path / "temporal-source-authority.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        project.add_column(sheet_id, "source", "video")
        project.add_column(sheet_id, "reviewed", "timeline_range")
        row_id = project.add_rows(sheet_id, [{}], {})[0]
        semantic = (
            TemporalMediaColumn if narrowed == "source" else TemporalSelectionColumn
        )
        accepted = ("audio",) if narrowed == "source" else ("text",)
        monkeypatch.setattr(semantic, "accepted_column_types", accepted)
        monkeypatch.setattr(
            CoreTemporalMediaMaterializer,
            "stage",
            lambda *_args, **_kwargs: pytest.fail("invalid source must never render"),
        )
        monkeypatch.setattr(
            "frisket.engine.executor.temporal_split.stage_media_splice",
            lambda *_args, **_kwargs: pytest.fail("invalid source must never render"),
        )
        request = {
            "action_id": kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
            "params": {
                "source": "source",
                "selection": {"kind": "column", "column": "reviewed"},
            },
            "idempotency_key": "semantic-source",
        }
        if kind == "derive.temporal_segments":
            request["sheet_name"] = "Segments"
        result = run_action_spec(project, request, project_id="test")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        invalid = result.errors[0].details["columns"][0]
        assert invalid["name"] == ("source" if narrowed == "source" else "reviewed")
        assert invalid["accepted_column_types"] == list(accepted)
        assert len(project.columns(sheet_id)) == 2
        assert [sheet["name"] for sheet in project.sheets()] == ["Media"]
    finally:
        project.close()


@pytest.mark.parametrize(
    ("source_type", "selection_type", "bad_name"),
    [
        ("text", "timeline_range", "source"),
        ("timestamped_transcript", "text", "reviewed"),
    ],
)
def test_transcript_runtime_reads_semantic_source_types(
    tmp_path, source_type, selection_type, bad_name
):
    project = Project.create(tmp_path / "transcript-semantic-source.frisket")
    try:
        sheet_id = project.add_sheet("Sources")
        project.add_column(sheet_id, "source", source_type)
        project.add_column(sheet_id, "reviewed", selection_type)
        result = resolve_transcript_segments_plan(
            project,
            scope=SheetRows(sheet_id=sheet_id, row_ids=[1]),
            source=TranscriptColumn("source"),
            selection=TranscriptColumnSelection(kind="column", column="reviewed"),
        )
        assert isinstance(result, ActionError)
        assert result.code == "invalid_input_ref"
        assert result.details["columns"][0]["name"] == bad_name

    finally:
        project.close()

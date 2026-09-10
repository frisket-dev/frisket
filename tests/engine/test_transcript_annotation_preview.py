"""Transcript samples project annotations without publishing a derived clock."""

from contextlib import closing

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.table_preview import preview_table
from frisket.engine.executor.temporal_preview import PreviewTemporalValue
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_artifact_timeline
from frisket.features.temporal_values import parse_temporal_value
from tests.engine.test_transcript_segments_action import _seed_timestamped_transcript


def test_preview_point_and_range_annotations_match_publication_without_writes(tmp_path):
    with closing(Project.create(tmp_path / "transcript")) as project:
        seeded = _seed_timestamped_transcript(project)
        source = resolve_timestamped_transcript(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["transcript_column_id"],
        )
        anchor = resolve_artifact_timeline(project, source.artifact_id)
        for name, kind, items in (
            (
                "markers",
                "timeline_points",
                [
                    {"id": "inside", "at_ms": 2500, "label": "Claim"},
                    {"id": "outside", "at_ms": 5000},
                ],
            ),
            (
                "highlights",
                "timeline_ranges",
                [
                    {"id": "clipped", "start_ms": 800, "end_ms": 2800},
                    {"id": "outside", "start_ms": 6000, "end_ms": 7000},
                ],
            ),
        ):
            column = project.add_column(seeded["sheet_id"], name, kind)
            project.apply_edits(
                [
                    {
                        "row_id": seeded["row_id"],
                        "column_id": column,
                        "value": {
                            "schema_version": f"frisket.{kind}.v1",
                            "timeline": anchor.wire_value(),
                            "items": items,
                        },
                    }
                ],
                label="annotations",
            )
        request = {
            "action_id": "derive.transcript_segments",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": seeded["sheet_id"],
                "row_ids": [seeded["row_id"]],
            },
            "params": {
                "source": "transcript",
                "selection": {
                    "kind": "draft_ranges",
                    "items": [{"start_ms": 2500, "end_ms": 3000}],
                },
            },
            "output_names": {"markers": "Important marks", "highlights": "Highlights"},
            "sheet_name": "Excerpt",
            "idempotency_key": "preview-then-run",
        }
        before = tuple(project.db.iterdump())
        preview = preview_table(
            project,
            "project-test",
            typed_action_for_request(request),
            deps=ExecutorDeps(),
            progress=lambda *_: None,
            cancelled=lambda: False,
        )
        try:
            assert tuple(project.db.iterdump()) == before
            assert not preview.artifacts
            assert len(preview.rows) == 1
            values = {
                name: preview.rows[0][name]["value"]
                for name in ("Important marks", "Highlights")
            }
            assert all(
                isinstance(value, PreviewTemporalValue) for value in values.values()
            )
            assert values["Important marks"].items["items"][0]["at_ms"] == 1500
            assert values["Highlights"].items["items"][0]["start_ms"] == 0
            assert values["Highlights"].items["items"][0]["end_ms"] == 1800
            assert len(values["Important marks"].items["items"]) == 1
            assert len(values["Highlights"].items["items"]) == 1
            assert values["Important marks"].timeline == values["Highlights"].timeline
            assert values["Important marks"].timeline["duration_ms"] == 3000
            assert "artifact_id" not in values["Important marks"].timeline
            for sampled in values.values():
                wire = sampled.wire_value()
                assert wire["schema_version"] == "frisket.preview_temporal.v1"
                with pytest.raises(ValueError):
                    parse_temporal_value(sampled.type_name, wire)
                with pytest.raises(ValueError):
                    parse_temporal_value(
                        sampled.type_name,
                        {
                            **wire,
                            "schema_version": f"frisket.{sampled.type_name}.v1",
                        },
                    )
        finally:
            preview.close()
        assert tuple(project.db.iterdump()) == before
        result = run_action_spec(project, request, project_id="project-test")
        assert result.status == "completed", result.errors
        row_id = next(
            output.row_ids[0] for output in result.outputs if output.kind == "rows"
        )
        for name, sampled in values.items():
            output = next(output for output in result.outputs if output.name == name)
            actual = project.get_values(
                output.sheet_id, output.column_id, row_ids=[row_id]
            )[row_id]
            assert actual["items"] == sampled.items["items"]
            assert actual["timeline"]["duration_ms"] == sampled.timeline["duration_ms"]

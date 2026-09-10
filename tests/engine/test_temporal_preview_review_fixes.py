"""Resolved temporal clips cannot silently escape the planned output schema."""

from contextlib import closing
from dataclasses import replace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from tests.engine.test_temporal_split import _fake_stage, _request, _run, _seed_media
from tests.engine.test_temporal_transcript_inheritance import (
    _add_timestamped_transcript,
)


@pytest.mark.parametrize("role", ["transcript", "annotation"])
@pytest.mark.parametrize("fits_schema", [False, True])
def test_widened_clip_requires_only_declared_projection_columns(
    tmp_path, role, fits_schema
):
    with closing(Project.create(tmp_path / "widened.frisket")) as project:
        seeded = _seed_media(project)
        lease = resolve_timeline(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=seeded["row_id"],
            column_id=seeded["columns"]["video"],
        )
        at_ms = 1500 if fits_schema else 2500
        if role == "transcript":
            _add_timestamped_transcript(
                project,
                sheet_id=seeded["sheet_id"],
                row_id=seeded["row_id"],
                artifact_id=lease.anchor.artifact_id,
                name="spoken",
                text="Hello.",
                span_values=[(at_ms, at_ms + 200, "Hello.")],
            )
        else:
            column = project.add_column(seeded["sheet_id"], "marker", "timeline_point")
            project.apply_edits(
                [
                    {
                        "row_id": seeded["row_id"],
                        "column_id": column,
                        "value": {
                            "schema_version": "frisket.timeline_point.v1",
                            "timeline": lease.anchor.wire_value(),
                            "item": {"id": "marker", "at_ms": at_ms},
                        },
                    }
                ],
                label="marker",
            )
        counts = {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sheets", "columns", "rows", "blobs", "source_artifacts")
        }

        def widened(project, source, requested, path):
            staged = _fake_stage(project, source, requested, path)
            return replace(
                staged,
                resolved_source_end_ms=3000,
                duration_ms=3000,
                probe=replace(staged.probe, duration_ms=3000),
            )

        request = _request(
            seeded,
            {
                "kind": "draft_ranges",
                "items": [{"start_ms": 0, "end_ms": 2000}],
            },
        )
        result = _run(project, request, stage_fn=widened, project_id="test")
        if fits_schema:
            assert result.status == "completed", result.errors
            sheet_id = next(
                item.sheet_id for item in result.outputs if item.kind == "sheet"
            )
            names = {column["name"] for column in project.columns(sheet_id)}
            assert ("spoken" if role == "transcript" else "marker") in names
            column = next(
                column
                for column in project.columns(sheet_id)
                if column["name"] == ("spoken" if role == "transcript" else "marker")
            )
            values = project.get_values(sheet_id, column["id"])
            if role == "transcript":
                assert list(values.values()) == ["Hello."]
            else:
                assert next(iter(values.values()))["item"]["at_ms"] == at_ms
        else:
            assert result.status == "failed"
            assert result.errors[0].code == "cut_alignment_failed"
            assert result.outputs == []
            assert counts == {
                table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in counts
            }

"""Copied transcript evidence owns its exact copied value independently."""

import json
import pytest

from frisket.engine.store import Project
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.temporal_transcripts import resolve_timestamped_transcript
from tests.engine.test_transcript_segments_action import _seed_timestamped_transcript


@pytest.mark.parametrize("change", [None, "owner", "run", "subject", "source_undo"])
def test_reapplied_timestamped_transcript_retains_temporal_authority(tmp_path, change):
    project = Project.create(tmp_path / "copied-transcript.frisket")
    try:
        source = _seed_timestamped_transcript(project)
        assert (
            resolve_timestamped_transcript(
                project,
                sheet_id=source["sheet_id"],
                row_id=source["row_id"],
                column_id=source["transcript_column_id"],
            )
            is not None
        )
        run_id, role = project.db.execute(
            "SELECT run_id,output_role FROM run_output_generations WHERE column_id=?",
            (source["transcript_column_id"],),
        ).fetchone()
        result = run_action_spec(
            project,
            {
                "action_id": "run.reapply",
                "idempotency_key": "copy-transcript",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": source["sheet_id"],
                    "row_ids": [source["row_id"]],
                },
                "params": {"source_run_id": run_id},
                "output_names": {role: "Copied transcript"},
            },
            project_id="review",
        )
        assert result.status == "completed", result.errors
        target = next(
            column
            for column in project.columns(source["sheet_id"])
            if column["name"] == "Copied transcript"
        )
        assert (
            project.db.execute(
                "SELECT count(*) FROM evidence_links WHERE column_id=? AND status='active'",
                (target["id"],),
            ).fetchone()[0]
            == 1
        )
        link = project.db.execute(
            "SELECT * FROM evidence_links WHERE column_id=?", (target["id"],)
        ).fetchone()
        if change == "owner":
            project.db.execute(
                "UPDATE evidence_links SET op_id=(SELECT op_id FROM runs WHERE id=?) WHERE id=?",
                (run_id, link["id"]),
            )
        elif change == "run":
            project.db.execute(
                "UPDATE evidence_links SET run_id=NULL WHERE id=?", (link["id"],)
            )
        elif change == "subject":
            ref = json.loads(link["subject_ref_json"])
            ref["retained_result"]["row_id"] += 1
            project.db.execute(
                "UPDATE evidence_links SET subject_ref_json=? WHERE id=?",
                (json.dumps(ref), link["id"]),
            )
        elif change == "source_undo":
            from frisket.engine.store.op_log import step_operation

            op = project.db.execute(
                "SELECT op_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            with project.transaction_scope():
                step_operation(project, "revert", op_id=op)
        project.db.commit()
        resolved = resolve_timestamped_transcript(
            project,
            sheet_id=source["sheet_id"],
            row_id=source["row_id"],
            column_id=target["id"],
        )
        assert (resolved is not None) == (change in {None, "source_undo"})
    finally:
        project.close()

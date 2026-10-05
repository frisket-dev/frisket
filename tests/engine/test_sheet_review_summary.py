from __future__ import annotations

import json

from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


def test_deleting_reviewed_sheet_refreshes_project_pending_summary(tmp_path):
    path = tmp_path / "delete-reviewed.frisket"
    project = Project.create(path)
    try:
        sheet = project.add_sheet("Scratch")
        column = project.add_column(sheet, "answer", ai_generated=True)
        [row] = project.add_rows(sheet, [{}], {})
        op = project.append_op("map.extract")
        store = RunResultStore(project)
        run = store.start_run(op, sheet, "map.extract", total_rows=1, row_ids=[row])
        project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value) "
            "VALUES (?,?,?,'text','answer')",
            (run, row, column),
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (run, column)
        )
        store.finish_run(run)
        assert project.refresh_pending_review_summary() == 1

        project.delete_sheet(sheet)

        assert (
            json.loads((path / "manifest.json").read_text())["pending_review_count"]
            == 0
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM run_review_fields").fetchone()[0]
            == 0
        )
    finally:
        project.close()

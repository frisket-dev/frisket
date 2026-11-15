from __future__ import annotations

import json

from helpers import write_claimed_test_results
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
        write_claimed_test_results(
            project, run, [{"row_id": row, "column_id": column, "value": "answer"}]
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
            == 1
        )
        # Deletion hides the sheet; its saved review inventory survives Undo.
        assert project.undo() is not None
        assert project.refresh_pending_review_summary() == 1
    finally:
        project.close()

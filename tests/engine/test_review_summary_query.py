from __future__ import annotations

from pathlib import Path

from frisket.engine.runner.review import review_bundle_count
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore


def test_review_summary_keeps_exact_bundle_and_visibility_semantics(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "exact-summary.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        source_id = project.add_column(sheet_id, "source")
        answer_id = project.add_column(sheet_id, "answer", ai_generated=True)
        category_id = project.add_column(sheet_id, "category", ai_generated=True)
        support_id = project.add_column(
            sheet_id, "answer_confidence", ai_generated=True
        )
        row_ids = project.add_rows(
            sheet_id,
            [{"source": "one"}, {"source": "two"}],
            {"source": source_id},
        )
        op_id = project.append_op("map", {"phase": "review summary fixture"})
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.extract",
            row_ids=row_ids,
            total_rows=len(row_ids),
        )
        project.db.executemany(
            "INSERT INTO results (run_id,row_id,column_id,value,outcome) "
            "VALUES (?,?,?,?,?)",
            [
                (run_id, row_id, column_id, '"value"', "ok")
                for row_id in row_ids
                for column_id in (answer_id, category_id, support_id)
            ],
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id IN (?,?,?)",
            (run_id, answer_id, category_id, support_id),
        )
        project.db.commit()

        # Two primary fields and an unreviewed support field still make one
        # bundle per run/row.
        assert project.refresh_pending_review_summary() == 2
        assert review_bundle_count(project) == 2

        project.db.execute(
            "UPDATE results SET review_state='verified' "
            "WHERE run_id=? AND row_id=? AND column_id IN (?,?)",
            (run_id, row_ids[0], answer_id, category_id),
        )
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[1],))
        project.db.commit()

        # The support field cannot keep row one pending, and hidden row two
        # cannot contribute a bundle.
        assert project.refresh_pending_review_summary() == 0
        assert review_bundle_count(project) == 0

        hidden_sheet_id = project.add_sheet("Hidden")
        hidden_output_id = project.add_column(
            hidden_sheet_id, "hidden_answer", ai_generated=True
        )
        hidden_row_id = project.add_rows(hidden_sheet_id, [{}], {})[0]
        hidden_op_id = project.append_op("map", {"phase": "hidden sheet fixture"})
        hidden_run_id = RunResultStore(project).start_run(
            hidden_op_id,
            hidden_sheet_id,
            "map.extract",
            row_ids=[hidden_row_id],
            total_rows=1,
        )
        project.db.execute(
            "INSERT INTO results (run_id,row_id,column_id,value,outcome) "
            "VALUES (?,?,?,?,?)",
            (hidden_run_id, hidden_row_id, hidden_output_id, '"value"', "ok"),
        )
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (hidden_run_id, hidden_output_id),
        )
        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (hidden_sheet_id,))
        project.db.commit()

        # Row ids are globally unique across sheets, and a hidden sheet never
        # adds a bundle even when its current result remains unreviewed.
        assert project.refresh_pending_review_summary() == 0
        assert review_bundle_count(project) == 0
    finally:
        project.close()

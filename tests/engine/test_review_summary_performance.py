from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.runner.review import review_bundle_count
from frisket.engine.store.review_stats import mark_run_review_stats_dirty
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import encode_stored_value


def _publish_managed_results(
    project: Project,
    *,
    sheet_id: int,
    action_kind: str,
    columns: Iterable[int],
    row_ids: list[int],
    point_heads: bool = True,
) -> int:
    column_ids = list(columns)
    op_id = project.append_op(action_kind, {"fixture": "review summary"})
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        action_kind,
        row_ids=row_ids,
        total_rows=len(row_ids),
    )
    project.db.executemany(
        "INSERT INTO run_output_generations "
        "(run_id,column_id,output_role,compatibility_key,write_mode,state,"
        "claim_token) VALUES (?,?,?,?,?,?,?)",
        [
            (
                run_id,
                column_id,
                f"output:{column_id}",
                f"sha256:review-summary:{column_id}",
                "create",
                "active",
                f"claim:{run_id}",
            )
            for column_id in column_ids
        ],
    )
    project.db.executemany(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (
                run_id,
                row_id,
                column_id,
                *encode_stored_value("value"),
                "ok",
                "publish_value",
            )
            for row_id in row_ids
            for column_id in column_ids
        ],
    )
    if point_heads:
        project.db.executemany(
            "INSERT INTO cell_result_heads (column_id,row_id,run_id) "
            "VALUES (?,?,?) ON CONFLICT(column_id,row_id) "
            "DO UPDATE SET run_id=excluded.run_id",
            [
                (column_id, row_id, run_id)
                for row_id in row_ids
                for column_id in column_ids
            ],
        )
    RunResultStore(project).finish_run(run_id)
    return run_id


def test_large_review_summary_keeps_exact_active_visibility_and_primary_semantics(
    tmp_path: Path,
) -> None:
    """Exercise the production-sized query shape without timing assertions."""
    project = Project.create(tmp_path / "review-summary-shape.frisket")
    try:
        sheet_id = project.add_sheet("Visible")
        answer_id = project.add_column(sheet_id, "answer", ai_generated=True)
        support_id = project.add_column(
            sheet_id, "answer_confidence", ai_generated=True
        )
        find_confidence_id = project.add_column(
            sheet_id, "confidence", ai_generated=True
        )
        row_ids = project.add_rows(sheet_id, [{} for _ in range(600)], {})

        first_run_id = _publish_managed_results(
            project,
            sheet_id=sheet_id,
            action_kind="map.extract",
            columns=(answer_id, support_id),
            row_ids=row_ids,
        )
        replacement_rows = row_ids[:60]
        replacement_run_id = _publish_managed_results(
            project,
            sheet_id=sheet_id,
            action_kind="map.extract",
            columns=(answer_id,),
            row_ids=replacement_rows,
        )
        _publish_managed_results(
            project,
            sheet_id=sheet_id,
            action_kind="map.find",
            columns=(find_confidence_id,),
            row_ids=row_ids,
        )

        # A partially replaced run remains one stable review task while any of
        # its outputs are current. Support fields do not add items, while
        # map.find owns fields whose names conventionally mark them as support.
        assert project.refresh_pending_review_summary() == 1_260
        assert review_bundle_count(project) == 1_260
        manifest = json.loads((project.path / "manifest.json").read_text())
        assert manifest["pending_review_count"] == 1_260

        # A direct global read heals a dirty terminal current run before it
        # trusts the cached bundle and field totals.
        project.db.execute(
            "UPDATE runs SET review_stats_ready=0,review_bundle_count=0,"
            "review_resolved_bundle_count=0 WHERE id=?",
            (replacement_run_id,),
        )
        project.db.execute(
            "UPDATE run_review_fields SET eligible_count=0,resolved_count=0 "
            "WHERE run_id=?",
            (replacement_run_id,),
        )
        project.db.commit()
        assert review_bundle_count(project) == 1_260

        RunResultStore(project).set_result_review_state(
            replacement_run_id,
            replacement_rows[0],
            answer_id,
            "verified",
            commit=False,
        )
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[1],))

        other_sheet_id = project.add_sheet("Other visible sheet")
        other_row_id = project.add_rows(other_sheet_id, [{}], {})[0]
        project.db.execute(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value_kind,value,outcome,publication_effect) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                replacement_run_id,
                other_row_id,
                answer_id,
                *encode_stored_value("wrong sheet"),
                "ok",
                "publish_value",
            ),
        )
        project.db.execute(
            "INSERT INTO cell_result_heads (column_id,row_id,run_id) VALUES (?,?,?)",
            (answer_id, other_row_id, replacement_run_id),
        )
        mark_run_review_stats_dirty(project.db, replacement_run_id)
        project.db.commit()

        # The decision resolves one stable bundle. Row visibility does not
        # rewrite historical totals, and malformed cross-sheet data remains
        # excluded by the membership join.
        assert project.refresh_pending_review_summary() == 1_259
        assert review_bundle_count(project) == 1_259

        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
        project.db.commit()
        assert project.refresh_pending_review_summary() == 0

        # Sanity-check that the test actually left historical data behind.
        stale = project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=? AND column_id=? "
            "AND row_id IN (SELECT row_id FROM cell_result_heads "
            "WHERE column_id=? AND run_id=?)",
            (first_run_id, answer_id, answer_id, replacement_run_id),
        ).fetchone()
        assert stale is not None and int(stale[0]) == len(replacement_rows)
    finally:
        project.close()

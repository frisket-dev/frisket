"""Scoped cached reads stay fast without missing new dirty-scope matches."""

from contextlib import closing
import json

from frisket.engine.store import Project
from frisket.engine.store.cell_projection import repair_current_cells
from frisket.engine.store.current_read_sql import current_values_sql
from frisket.querysets import resolve_sheet_filter_rows


def _seed(tmp_path):
    project = Project.create(tmp_path / "reads.frisket")
    sheet = project.add_sheet("Rows")
    source = project.add_column(sheet, "source")
    repair = project.add_column(sheet, "repair")
    rows = project.add_rows(
        sheet, [{"source": "old"} for _ in range(700)], {"source": source}
    )
    while repair_current_cells(project):
        pass
    return project, sheet, source, repair, rows


def test_dirty_filter_finds_values_absent_from_old_projection(tmp_path):
    project, sheet, source, target, rows = _seed(tmp_path)
    with closing(project):
        project.apply_edits(
            [{"row_id": row, "column_id": target, "value": "new"} for row in rows[:600]]
        )
        assert project.db.execute(
            "SELECT COUNT(*) FROM current_cell_repair WHERE column_id=?", (target,)
        ).fetchone()[0]
        with project.read_snapshot() as snapshot:
            selected = resolve_sheet_filter_rows(
                snapshot,
                sheet,
                filter_=json.dumps({"repair": {"eq": "new"}}),
                limit=1000,
            )
            assert selected.row_ids == rows[:600]
            values = current_values_sql(snapshot.db, [target], [rows[0], rows[-1]])
            assert [
                tuple(row)
                for row in snapshot.db.execute(
                    f"SELECT row_id,value FROM {values} ORDER BY row_id"
                )
            ] == [(rows[0], "new")]
            # Unrelated dirtiness must not force a whole-history read of source.
            steps = 0

            def progress():
                nonlocal steps
                steps += 100
                return steps > 10000

            snapshot.db.set_progress_handler(progress, 100)
            try:
                clean = current_values_sql(snapshot.db, [source], [rows[-1]])
                assert (
                    snapshot.db.execute(f"SELECT value FROM {clean}").fetchone()[0]
                    == "old"
                )
            finally:
                snapshot.db.set_progress_handler(None, 0)
            assert steps < 10000


def test_clean_dispatch_is_bound_to_its_read_snapshot(tmp_path):
    project, sheet, source, target, rows = _seed(tmp_path)
    with closing(project), closing(Project(project.path)) as writer:
        with project.read_snapshot() as snapshot:
            relation = current_values_sql(snapshot.db, [source], [rows[0]])
            writer.apply_edits(
                [
                    {"row_id": row, "column_id": source, "value": "new"}
                    for row in rows[:600]
                ]
            )
            assert (
                snapshot.db.execute(f"SELECT value FROM {relation}").fetchone()[0]
                == "old"
            )
        with project.read_snapshot() as snapshot:
            relation = current_values_sql(snapshot.db, [source], [rows[0]])
            assert (
                snapshot.db.execute(f"SELECT value FROM {relation}").fetchone()[0]
                == "new"
            )


def test_autocommit_relation_cannot_capture_stale_clean_decision(tmp_path):
    project, sheet, source, target, rows = _seed(tmp_path)
    with closing(project):
        assert not project.db.in_transaction
        relation = current_values_sql(project.db, [source], [rows[0]])
        project.apply_edits(
            [{"row_id": row, "column_id": source, "value": "new"} for row in rows[:600]]
        )
        assert (
            project.db.execute(f"SELECT value FROM {relation}").fetchone()[0] == "new"
        )

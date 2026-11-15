"""Current-reader scope must reach the indexed authority before materialization."""

from frisket.engine.store import Project
from frisket.engine.store.current_value_windows import _authority_cte, _params
from frisket.engine.store.cell_projection import invalidate_region, repair_current_cells
from frisket.querysets import resolve_sheet_filter_rows


def _plan(db, sql, params=()):
    return [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _assert_point_scope(plan):
    # A mere column search is not enough: materializing that column still
    # visits a million rows to display one cell. Both key parts must reach the
    # original contribution tables beneath the UNION/window expression.
    assert any("SEARCH cell " in line and "row_id=?" in line for line in plan), plan
    assert any("SEARCH scope " in line and "row_id=?" in line for line in plan), plan
    assert any("SEARCH edit " in line and "row_id=?" in line for line in plan), plan


def test_bounded_current_read_query_plan(tmp_path):
    project = Project.create(tmp_path / "plan.frisket")
    try:
        sheet = project.add_sheet("Data")
        column = project.add_column(sheet, "value", type="integer")
        rows = project.add_rows(
            sheet, [{"value": i} for i in range(700)], {"value": column}
        )
        # Exercise an actual deferred invalidation independently of whether
        # fresh-source insertion can initialize its new heads cheaply.
        project.db.execute("BEGIN")
        invalidate_region(project.db, column, rows[0], rows[-1])
        project.db.commit()
        # This uses the actual CTE used by bounded grid/window reads.
        _assert_point_scope(
            _plan(
                project.db,
                "WITH "
                + _authority_cte(project.db, column, rows[:1])
                + " SELECT * FROM authority",
                _params(sheet, column, rows[:1]),
            )
        )
        statements = []
        project.db.set_trace_callback(statements.append)
        try:
            assert (
                project.get_values_with_refs(sheet, column, rows[:1])[0][rows[0]] == 0
            )
        finally:
            project.db.set_trace_callback(None)
        value_query = next(s for s in statements if "SELECT r.id, c.value_kind" in s)
        _assert_point_scope(_plan(project.db, value_query))

        # Repair must also constrain original source history to its row window.
        plan = _plan(
            project.db,
            "SELECT * FROM canonical_cell_heads WHERE column_id=? AND row_id BETWEEN ? AND ?",
            (column, rows[0], rows[6]),
        )
        assert any("SEARCH cell " in line and "row_id>?" in line for line in plan), plan
        assert repair_current_cells(project, limit=7) == 7
        pending = project.db.execute(
            "SELECT row_id_start FROM current_cell_repair"
        ).fetchone()[0]
        assert pending == rows[7]

        # Numeric predicates retain correct false-negative protection while
        # some rows are repaired and the rest still require canonical reads.
        filtered = resolve_sheet_filter_rows(
            project, sheet, filter_='{"value":{"gte":"699"}}'
        )
        assert filtered.row_ids == [rows[-1]]
        assert filtered.total == 1
    finally:
        project.close()

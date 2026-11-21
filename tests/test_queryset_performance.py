"""Runtime regressions for large sheet filter and sort paths."""

from __future__ import annotations

from contextlib import closing
from datetime import date
import json

import pytest

from frisket.engine.store import Project
from frisket.querysets import (
    resolve_sheet_filter_rows,
    sheet_row_scope_plan,
    sheet_row_scope_query,
)
from frisket.server.services.sheet_grid import SheetGridService


class _OneProjectWorkspace:
    def __init__(self, project: Project):
        self.project = project

    def get(self, _project_id: str) -> Project:
        return self.project


def _number_sheet(make_project) -> tuple[Project, int, int, list[int]]:
    project = make_project("query-performance")
    sheet_id = project.add_sheet("Scores")
    column_id = project.add_column(sheet_id, "score", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"score": 2500},
            {"score": 2000},
            {"score": 2000.0},
            {"score": 1999},
            {"score": "3000"},
            {"score": None},
            {"score": True},
            {},
        ],
        {"score": column_id},
    )
    return project, sheet_id, column_id, row_ids


def _vm_steps(project: Project, call):
    steps = 0
    interval = 100

    def progress() -> int:
        nonlocal steps
        steps += interval
        return 0

    project.db.set_progress_handler(progress, interval)
    try:
        result = call()
    finally:
        project.db.set_progress_handler(None, 0)
    return result, steps


def test_numeric_filter_and_sort_preserve_typed_value_semantics(make_project) -> None:
    project, sheet_id, _column_id, row_ids = _number_sheet(make_project)
    result = resolve_sheet_filter_rows(
        project,
        sheet_id,
        filter_=json.dumps({"score": {"gte": 2000}}),
        sort=json.dumps([{"column": "score", "dir": "desc"}]),
        limit=2,
        offset=1,
    )
    sorted_result = resolve_sheet_filter_rows(
        project,
        sheet_id,
        sort=json.dumps([{"column": "score", "dir": "asc"}]),
        limit=10,
    )

    # Numeric JSON values participate; numeric strings, booleans, explicit
    # nulls, and missing cells retain the existing invalid/missing behavior.
    assert result.total == 3
    assert result.row_ids == row_ids[1:3]
    assert sorted_result.row_ids == [
        row_ids[3],
        row_ids[1],
        row_ids[2],
        row_ids[0],
        row_ids[4],
        row_ids[5],
        row_ids[6],
        row_ids[7],
    ]


def test_numeric_filtered_pages_keep_exact_totals_at_page_boundaries(
    make_project,
) -> None:
    project, sheet_id, _column_id, _row_ids = _number_sheet(make_project)
    service = SheetGridService(_OneProjectWorkspace(project))
    filter_json = json.dumps({"score": {"gte": 2000}})
    sort_json = json.dumps([{"column": "score", "dir": "desc"}])
    for limit, offset, total in ((0, 0, 3), (2, 99, 3)):
        resolved = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=limit,
            offset=offset,
        )
        grid = service.sheet_data(
            "project",
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=limit,
            offset=offset,
        )
        assert (resolved.row_ids, resolved.total) == ([], total)
        assert (grid["rows"], grid["total"]) == ([], total)

    empty_filter = json.dumps({"score": {"gte": 9999}})
    resolved = resolve_sheet_filter_rows(
        project, sheet_id, filter_=empty_filter, sort=sort_json, limit=2
    )
    grid = service.sheet_data(
        "project", sheet_id, filter_=empty_filter, sort=sort_json, limit=2
    )
    assert (resolved.row_ids, resolved.total) == ([], 0)
    assert (grid["rows"], grid["total"]) == ([], 0)


def test_full_range_default_order_keeps_bounded_page_query(make_project) -> None:
    project = make_project("query-full-range-work")
    sheet_id = project.add_sheet("Scores")
    column_id = project.add_column(sheet_id, "score", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [{"score": value} for value in range(3_000)],
        {"score": column_id},
    )
    filter_json = json.dumps({"score": {"gte": 0}})
    plan = sheet_row_scope_plan(project, sheet_id, filter_=filter_json)

    def count_and_page():
        total = int(
            project.db.execute(
                f"SELECT COUNT(*) FROM {plan.filter_from_sql} WHERE {plan.where_sql}",
                plan.filter_params,
            ).fetchone()[0]
        )
        rows = project.db.execute(
            f"SELECT r.id FROM {plan.from_sql} WHERE {plan.where_sql} "
            f"ORDER BY {', '.join(plan.order_parts)} LIMIT 10 OFFSET 0",
            plan.select_params,
        ).fetchall()
        return total, [int(row["id"]) for row in rows]

    old_page, old_steps = _vm_steps(project, count_and_page)
    resolved, resolved_steps = _vm_steps(
        project,
        lambda: resolve_sheet_filter_rows(
            project, sheet_id, filter_=filter_json, limit=10
        ),
    )

    expected = (3_000, row_ids[:10])
    assert old_page == expected
    assert (resolved.total, resolved.row_ids) == expected
    assert resolved_steps <= old_steps * 1.05


def test_numeric_filter_and_sort_reduce_sqlite_vm_work(make_project) -> None:
    project = make_project("query-vm-work")
    sheet_id = project.add_sheet("Scores")
    column_id = project.add_column(sheet_id, "score", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [{"score": value} for value in range(3_000)],
        {"score": column_id},
    )
    filter_json = json.dumps({"score": {"gte": 2900}})
    sort_json = json.dumps([{"column": "score", "dir": "desc"}])

    _, where_sql, where_params, order_parts, order_params = sheet_row_scope_query(
        project,
        sheet_id,
        filter_=filter_json,
        sort=sort_json,
    )
    order_sql = ", ".join(order_parts)
    plan = sheet_row_scope_plan(
        project,
        sheet_id,
        filter_=filter_json,
        sort=sort_json,
    )

    legacy_count, legacy_count_steps = _vm_steps(
        project,
        lambda: int(
            project.db.execute(
                f"SELECT COUNT(*) FROM rows r WHERE {where_sql}", where_params
            ).fetchone()[0]
        ),
    )
    optimized_count, optimized_count_steps = _vm_steps(
        project,
        lambda: int(
            project.db.execute(
                f"SELECT COUNT(*) FROM {plan.filter_from_sql} WHERE {plan.where_sql}",
                plan.filter_params,
            ).fetchone()[0]
        ),
    )
    legacy_rows, legacy_page_steps = _vm_steps(
        project,
        lambda: project.db.execute(
            "SELECT r.id FROM rows r "
            f"WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*where_params, *order_params, 10, 0],
        ).fetchall(),
    )
    optimized_rows, optimized_page_steps = _vm_steps(
        project,
        lambda: project.db.execute(
            f"SELECT r.id FROM {plan.from_sql} WHERE {plan.where_sql} "
            f"ORDER BY {', '.join(plan.order_parts)} LIMIT ? OFFSET ?",
            [*plan.select_params, 10, 0],
        ).fetchall(),
    )
    resolved, resolved_steps = _vm_steps(
        project,
        lambda: resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=10,
        ),
    )
    grid, grid_steps = _vm_steps(
        project,
        lambda: SheetGridService(_OneProjectWorkspace(project)).sheet_data(
            "project",
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=10,
        ),
    )

    expected = (100, list(reversed(row_ids[-10:])))
    assert (legacy_count, [int(row["id"]) for row in legacy_rows]) == expected
    assert (optimized_count, [int(row["id"]) for row in optimized_rows]) == expected
    assert (resolved.total, resolved.row_ids) == expected
    assert (grid["total"], [row["id"] for row in grid["rows"]]) == expected
    assert optimized_count_steps < legacy_count_steps * 0.7
    assert optimized_page_steps < legacy_page_steps * 0.7
    # Count/page now use the native range index separately. The public resolver
    # should add only modest metadata work to those already-cheap queries.
    assert resolved_steps <= (optimized_count_steps + optimized_page_steps) * 1.2
    assert resolved_steps < (legacy_count_steps + legacy_page_steps) * 0.7
    assert grid_steps < (legacy_count_steps + legacy_page_steps) * 0.9


def test_correlated_scope_query_binds_date_filter_parameters(make_project) -> None:
    project = make_project("query-date-bindings")
    sheet_id = project.add_sheet("Events")
    column_id = project.add_column(sheet_id, "published", type="date")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"published": "2026-01-03"},
            {"published": "2026-01-04"},
            {"published": "2026-01-11"},
        ],
        {"published": column_id},
    )
    _, where_sql, where_params, _order_parts, _order_params = sheet_row_scope_query(
        project,
        sheet_id,
        filter_=json.dumps(
            {"published": {"date_relative": {"amount": 7, "unit": "days"}}}
        ),
        reference_date=date(2026, 1, 10),
    )

    matched = project.db.execute(
        f"SELECT r.id FROM rows r WHERE {where_sql}", where_params
    ).fetchall()

    assert [int(row["id"]) for row in matched] == [row_ids[1]]


def test_filter_total_and_page_share_a_snapshot(make_project, monkeypatch):
    import frisket.querysets as queries

    project, sheet, column, rows = _number_sheet(make_project)
    with closing(Project(project.path)) as writer:
        original = queries.execute_sheet_row_page_without_total

        def change_after_count(*args, **kwargs):
            writer.apply_edits([{"row_id": rows[0], "column_id": column, "value": 0}])
            return original(*args, **kwargs)

        monkeypatch.setattr(
            queries, "execute_sheet_row_page_without_total", change_after_count
        )
        result = resolve_sheet_filter_rows(
            project, sheet, filter_=json.dumps({"score": {"gte": 2000}})
        )
        assert result.total == 3
        assert result.row_ids == rows[:3]
        assert not project.db.in_transaction
        assert project.get_values(sheet, column)[rows[0]] == 0


def test_filter_borrows_caller_transaction_without_ending_it(make_project):
    project, sheet, column, rows = _number_sheet(make_project)
    project.db.execute("BEGIN")
    result = resolve_sheet_filter_rows(
        project, sheet, filter_=json.dumps({"score": {"gte": 2000}})
    )
    assert result.row_ids == rows[:3]
    assert project.db.in_transaction
    project.db.rollback()


@pytest.mark.parametrize(
    "condition", [{"eq": "x"}, {"in": ["x", "y"]}, {"contains": "x"}]
)
def test_sparse_comparison_filters_scan_populated_coordinates(
    make_project, condition: dict
) -> None:
    project = make_project("query-sparse-comparison")
    sheet_id = project.add_sheet("Tags")
    column_id = project.add_column(sheet_id, "tag", type="category")
    row_ids = project.add_rows(
        sheet_id,
        [{"tag": "x"} if i % 100 == 0 else {} for i in range(3_000)],
        {"tag": column_id},
    )
    filter_json = json.dumps({"tag": condition})
    _, where_sql, params, _, _ = sheet_row_scope_query(
        project, sheet_id, filter_=filter_json
    )
    old_count, old_steps = _vm_steps(
        project,
        lambda: project.db.execute(
            f"SELECT COUNT(*) FROM rows r WHERE {where_sql}", params
        ).fetchone()[0],
    )
    result, steps = _vm_steps(
        project,
        lambda: resolve_sheet_filter_rows(
            project, sheet_id, filter_=filter_json, limit=10
        ),
    )
    assert result.total == old_count == 30
    assert result.row_ids == row_ids[::100][:10]
    # Count plus page must not probe all the absent coordinates just to
    # find the sparse matching cells. VM work avoids wall-clock thresholds.
    assert steps < old_steps * 0.5


@pytest.mark.parametrize(
    "condition", [{"eq": "x"}, {"in": ["x", "y"]}, {"contains": ""}, {"neq": "x"}]
)
def test_comparison_filter_preserves_missing_clear_invalid_and_sort(
    make_project, condition: dict
) -> None:
    project = make_project("query-comparison-values")
    sheet_id = project.add_sheet("Tags")
    tag = project.add_column(sheet_id, "tag", type="category")
    other = project.add_column(sheet_id, "other", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [
            {},
            {"tag": None},
            {"tag": 5},
            {"tag": ""},
            {"tag": "x"},
            {"tag": "y", "other": 2},
            {"tag": "x", "other": 1},
        ],
        {"tag": tag, "other": other},
    )
    project.apply_edits([{"row_id": row_ids[-1], "column_id": tag, "value": None}])
    filter_json = json.dumps({"tag": condition})
    sort_json = json.dumps([{"column": "other", "dir": "desc"}])
    _, where, params, order, order_params = sheet_row_scope_query(
        project, sheet_id, filter_=filter_json, sort=sort_json
    )
    expected = [
        int(row[0])
        for row in project.db.execute(
            f"SELECT r.id FROM rows r WHERE {where} ORDER BY {', '.join(order)}",
            [*params, *order_params],
        )
    ]
    result = resolve_sheet_filter_rows(
        project, sheet_id, filter_=filter_json, sort=sort_json, limit=100
    )
    assert result.row_ids == expected
    assert result.total == len(expected)

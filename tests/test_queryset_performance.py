"""Runtime regressions for large sheet filter and sort paths."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

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


def _number_sheet(tmp_path: Path) -> tuple[Project, int, int, list[int]]:
    project = Project.create(tmp_path / "query-performance.frisket")
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


def test_numeric_filter_and_sort_preserve_typed_value_semantics(tmp_path: Path) -> None:
    project, sheet_id, _column_id, row_ids = _number_sheet(tmp_path)
    try:
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
    finally:
        project.close()

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


def test_numeric_filter_and_sort_reduce_sqlite_vm_work(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "query-vm-work.frisket")
    try:
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
                    f"SELECT COUNT(*) FROM {plan.filter_from_sql} "
                    f"WHERE {plan.where_sql}",
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
        grid = SheetGridService(_OneProjectWorkspace(project)).sheet_data(
            "project",
            sheet_id,
            filter_=filter_json,
            sort=sort_json,
            limit=10,
        )
    finally:
        project.close()

    expected = (100, list(reversed(row_ids[-10:])))
    assert (legacy_count, [int(row["id"]) for row in legacy_rows]) == expected
    assert (optimized_count, [int(row["id"]) for row in optimized_rows]) == expected
    assert (grid["total"], [row["id"] for row in grid["rows"]]) == expected
    assert optimized_count_steps < legacy_count_steps * 0.7
    assert optimized_page_steps < legacy_page_steps * 0.7


def test_correlated_scope_query_binds_date_filter_parameters(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "query-date-bindings.frisket")
    try:
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
    finally:
        project.close()

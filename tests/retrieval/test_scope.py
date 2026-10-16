from __future__ import annotations

import json
from collections import Counter

import pytest

from frisket.engine.store import Project
from frisket.retrieval.contracts import RetrievalScope, SheetScope
from frisket.retrieval.scope import compile_scope


def _project(tmp_path):
    project = Project.create(tmp_path / "scope.frisket", name="scope")
    sheet = project.add_sheet("Sources")
    category = project.add_column(sheet, "kind", type="category")
    body = project.add_column(sheet, "body")
    rows = project.add_rows(
        sheet,
        [
            {"kind": "included", "body": "one"},
            {"kind": "excluded", "body": "two"},
            {"kind": "included", "body": "three"},
        ],
        {"kind": category, "body": body},
    )
    return project, sheet, category, body, rows


def test_scope_uses_existing_filter_compiler_and_explicit_selection(tmp_path):
    project, sheet, _category, body, rows = _project(tmp_path)
    try:
        filter_json = json.dumps({"kind": {"eq": "included"}})
        scope = compile_scope(
            project,
            RetrievalScope(
                sheets=(
                    SheetScope(
                        sheet_id=sheet,
                        row_ids=(rows[1], rows[2]),
                        column_ids=(body,),
                        filter_json=filter_json,
                    ),
                )
            ),
        )
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT * FROM scoped_bindings ORDER BY row_id",
            scope.params,
        ).fetchall()

        assert [tuple(row) for row in found] == [(sheet, rows[2], body)]
        assert "json_each" in scope.cte_sql
        assert str(rows[2]) not in scope.cte_sql
    finally:
        project.close()


def test_empty_explicit_scope_compiles_to_empty_relation(tmp_path):
    project, sheet, _category, _body, _rows = _project(tmp_path)
    try:
        scope = compile_scope(
            project, RetrievalScope(sheets=(SheetScope(sheet, row_ids=()),))
        )
        assert (
            project.db.execute(
                f"WITH {scope.cte_sql} SELECT count(*) FROM scoped_bindings",
                scope.params,
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_unrestricted_scope_still_excludes_hidden_rows_and_sheets(tmp_path):
    project, sheet, _category, _body, rows = _project(tmp_path)
    try:
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[0],))
        project.db.commit()
        scope = compile_scope(project, RetrievalScope())
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT row_id FROM scoped_bindings ORDER BY row_id"
        ).fetchall()
        assert [row[0] for row in found] == rows[1:]
    finally:
        project.close()


def test_explicit_sheet_scope_excludes_a_hidden_sheet(tmp_path):
    project, sheet, _category, body, rows = _project(tmp_path)
    try:
        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet,))
        project.db.commit()

        scope = compile_scope(
            project,
            RetrievalScope(
                sheets=(
                    SheetScope(
                        sheet_id=sheet,
                        row_ids=(rows[0],),
                        column_ids=(body,),
                    ),
                )
            ),
        )
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT * FROM scoped_bindings", scope.params
        ).fetchall()

        assert found == []
    finally:
        project.close()


def test_repeated_sheet_scopes_keep_row_and_column_rules_correlated(tmp_path):
    project, sheet, category, body, rows = _project(tmp_path)
    try:
        scope = compile_scope(
            project,
            RetrievalScope(
                sheets=(
                    SheetScope(sheet, row_ids=(rows[0],)),
                    SheetScope(sheet, row_ids=(rows[1],), column_ids=(body,)),
                )
            ),
        )
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT * FROM scoped_bindings "
            "ORDER BY row_id,column_id",
            scope.params,
        ).fetchall()

        assert [tuple(row) for row in found] == [
            (sheet, rows[0], None),
            (sheet, rows[1], body),
        ]
        assert (sheet, rows[1], category) not in {tuple(row) for row in found}
    finally:
        project.close()


def test_all_row_and_explicit_row_scopes_remain_independent(tmp_path):
    project, sheet, category, body, rows = _project(tmp_path)
    try:
        scope = compile_scope(
            project,
            RetrievalScope(
                sheets=(
                    SheetScope(sheet, column_ids=(category,)),
                    SheetScope(sheet, row_ids=(rows[0],), column_ids=(body,)),
                )
            ),
        )
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT * FROM scoped_bindings "
            "ORDER BY row_id,column_id",
            scope.params,
        ).fetchall()

        assert [tuple(row) for row in found] == [
            (sheet, rows[0], category),
            (sheet, rows[0], body),
            (sheet, rows[1], category),
            (sheet, rows[2], category),
        ]
    finally:
        project.close()


@pytest.mark.parametrize("scope_count", [600, 2_000])
def test_large_cell_selection_uses_one_correlated_json_relation(tmp_path, scope_count):
    project, sheet, category, body, rows = _project(tmp_path)
    try:
        rows.extend(
            project.add_rows(
                sheet,
                [
                    {"kind": "included", "body": f"extra {index}"}
                    for index in range(scope_count - 2)
                ],
                {"kind": category, "body": body},
            )
        )
        assert len(rows) == scope_count + 1
        requested = tuple(
            SheetScope(
                sheet,
                row_ids=(rows[index],),
                column_ids=((category if index % 2 == 0 else body),),
            )
            for index in range(scope_count)
        )
        scope = compile_scope(project, RetrievalScope(sheets=requested))
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT * FROM scoped_bindings", scope.params
        ).fetchall()

        assert Counter(tuple(row) for row in found) == Counter(
            (sheet, rows[index], category if index % 2 == 0 else body)
            for index in range(scope_count)
        )
        assert "UNION ALL" not in scope.cte_sql
        assert len(scope.params) == 2
    finally:
        project.close()


def test_scope_accepts_forty_thousand_row_ids_as_one_json_parameter(tmp_path):
    project, sheet, _category, body, rows = _project(tmp_path)
    try:
        row_ids = (*rows, *range(max(rows) + 1, max(rows) + 39_998))
        assert len(row_ids) == 40_000
        scope = compile_scope(
            project,
            RetrievalScope(
                sheets=(SheetScope(sheet, row_ids=row_ids, column_ids=(body,)),)
            ),
        )
        found = project.db.execute(
            f"WITH {scope.cte_sql} SELECT * FROM scoped_bindings ORDER BY row_id",
            scope.params,
        ).fetchall()

        assert [tuple(row) for row in found] == [
            (sheet, rows[0], body),
            (sheet, rows[1], body),
            (sheet, rows[2], body),
        ]
        assert len(scope.params) == 2
    finally:
        project.close()

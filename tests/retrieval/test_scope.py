from __future__ import annotations

import json

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

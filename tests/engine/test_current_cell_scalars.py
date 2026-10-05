"""Runtime contracts for bounded scalar values on current-cell heads."""

from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.querysets import resolve_sheet_filter_rows
from frisket.server.services.project_qa_analytics import evaluate_analytics
from test_result_generation_store import (
    _declare,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)


def _inline_value(
    project: Project, row_id: int, column_id: int
) -> tuple[str | None, object]:
    row = project.db.execute(
        "SELECT inline_value_kind,inline_value FROM current_cells "
        "WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    assert row is not None
    return row["inline_value_kind"], row["inline_value"]


def test_inline_scalars_follow_public_precedence_and_transactions(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    project.set_column_type(column_id, "integer")
    generations = ResultGenerationStore(project)
    run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="numeric scalar generation",
    )
    try:
        _declare(generations, run, column_id, write_mode="create")
        _write(
            project,
            run,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": column_id,
                    "value": 10,
                    "publication_effect": "publish_value",
                },
                {
                    "row_id": row_ids[1],
                    "column_id": column_id,
                    "value": None,
                    "publication_effect": "publish_null",
                },
                {
                    "row_id": row_ids[2],
                    "column_id": column_id,
                    "error": "fixture failure",
                    "error_code": "fixture_failure",
                    "publication_effect": "publish_error",
                },
                {
                    "row_id": row_ids[3],
                    "column_id": column_id,
                    "value": 40,
                    "publication_effect": "publish_value",
                },
            ],
        )
        _seal(generations, run, column_id)

        assert project.get_values(sheet_id, column_id, row_ids) == {
            row_ids[0]: 10,
            row_ids[1]: None,
            row_ids[2]: None,
            row_ids[3]: 40,
        }
        assert [_inline_value(project, row_id, column_id) for row_id in row_ids] == [
            ("integer", 10),
            ("null", None),
            (None, None),
            ("integer", 40),
        ]
        assert resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"generated": {"gte": "20"}}),
        ).row_ids == [row_ids[3]]
        analytics = evaluate_analytics(
            project,
            {
                "sheet_id": sheet_id,
                "metrics": [
                    {"id": "rows", "kind": "count"},
                    {"id": "sum", "kind": "sum", "column_id": column_id},
                ],
            },
            {"kind": "sheet", "sheet_id": sheet_id},
        )
        assert analytics["groups"][0]["metrics"] == {"rows": 4, "sum": 50.0}

        edit_op_id = project.apply_edits(
            [{"row_id": row_ids[0], "column_id": column_id, "value": 99}]
        )
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {row_ids[0]: 99}
        assert _inline_value(project, row_ids[0], column_id) == ("integer", 99)
        assert project.undo() == edit_op_id
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {row_ids[0]: 10}
        assert _inline_value(project, row_ids[0], column_id) == ("integer", 10)
        assert project.redo() == edit_op_id
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {row_ids[0]: 99}

        rollback_op_id = project.append_op("edit", {}, label="rolled back edit")
        project.db.execute("BEGIN")
        insert_edits(
            project.db,
            op_id=rollback_op_id,
            edits=[EditCellWrite(row_ids[0], column_id, 123)],
        )
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {
            row_ids[0]: 123
        }
        assert _inline_value(project, row_ids[0], column_id) == ("integer", 123)
        project.db.rollback()
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {row_ids[0]: 99}
        assert _inline_value(project, row_ids[0], column_id) == ("integer", 99)
    finally:
        if project.db.in_transaction:
            project.db.rollback()
        _release(project, run)
        project.close()


def test_inline_scalar_eligibility_and_type_reclassification(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "scalar-eligibility.frisket")
    try:
        sheet_id = project.add_sheet("Values")
        columns = {
            name: project.add_column(sheet_id, name, type=type_)
            for name, type_ in {
                "integer": "integer",
                "real": "number",
                "boolean": "boolean",
                "category": "category",
                "date": "date",
                "bigint": "number",
                "text": "text",
                "json": "json",
                "long_category": "category",
            }.items()
        }
        values = {
            "integer": 7,
            "real": 1.5,
            "boolean": True,
            "category": "alpha",
            "date": "2026-10-05",
            "bigint": 2**70,
            "text": "short",
            "json": {"a": 1},
            "long_category": "é" * 65,
        }
        row_id = project.add_rows(sheet_id, [values], columns)[0]

        assert project.get_values(sheet_id, columns["json"], [row_id]) == {
            row_id: {"a": 1}
        }
        assert {
            name: _inline_value(project, row_id, column_id)
            for name, column_id in columns.items()
        } == {
            "integer": ("integer", 7),
            "real": ("real", 1.5),
            "boolean": ("boolean", 1),
            "category": ("text", "alpha"),
            "date": ("text", "2026-10-05"),
            "bigint": ("bigint", str(2**70)),
            "text": (None, None),
            "json": (None, None),
            "long_category": (None, None),
        }

        project.set_column_type(columns["category"], "text")
        assert project.get_values(sheet_id, columns["category"], [row_id]) == {
            row_id: "alpha"
        }
        assert _inline_value(project, row_id, columns["category"]) == (None, None)
    finally:
        project.close()

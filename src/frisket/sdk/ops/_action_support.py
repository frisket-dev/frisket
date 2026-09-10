from typing import Any

from frisket.contracts.action import ActionOutput


def _current_run_columns_by_name(
    project: Any, sheet_id: int, run_id: int
) -> dict[str, Any]:
    """Return the exact output columns declared by one managed run."""

    return {
        row["name"]: row
        for row in project.db.execute(
            "SELECT column.* FROM run_output_generations generation "
            "JOIN columns column ON column.id=generation.column_id "
            "WHERE column.sheet_id=? AND generation.run_id=?",
            (int(sheet_id), int(run_id)),
        ).fetchall()
    }


def _result_row_ids(project: Any, run_id: int) -> list[int]:
    return [
        int(row["row_id"])
        for row in project.db.execute(
            "SELECT DISTINCT row_id FROM results WHERE run_id=? ORDER BY row_id",
            (run_id,),
        ).fetchall()
    ]


def _column_outputs(
    output_refs: list[dict[str, Any]], sheet_id: int, row_ids: list[int]
) -> list[ActionOutput]:
    return [
        ActionOutput(
            kind="column",
            name=ref["name"],
            sheet_id=sheet_id,
            column_id=ref["column_id"],
            row_ids=row_ids,
            ref=ref,
        )
        for ref in output_refs
    ]

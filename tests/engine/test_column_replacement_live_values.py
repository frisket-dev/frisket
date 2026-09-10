from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.store.cells import apply_edits
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.querysets import resolve_sheet_filter_rows
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
    _write,
)


def _reject_clear(project, *, row_id: int, column_id: int) -> int:
    with project.db:
        op_id = project.append_op(
            "review.decision",
            {
                "action_id": "review.decision",
                "params": {"decision": "reject_clear"},
            },
            label="reject generated value",
            commit=False,
        )
        insert_edits(
            project.db,
            op_id=op_id,
            edits=[EditCellWrite(row_id=row_id, column_id=column_id, value=None)],
        )
    return op_id


def test_selected_replacement_supersedes_only_exact_reject_clear_cell(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    try:
        generations, _first = _publish_initial_generation(
            project, sheet_id, column_id, row_ids
        )
        _reject_clear(project, row_id=row_ids[0], column_id=column_id)
        apply_edits(
            project,
            [{"row_id": row_ids[1], "column_id": column_id, "value": "manual"}],
        )
        # The JSON shape alone is not authority: only the exact op kind is a
        # reject_clear overlay that a later explicit replacement may suppress.
        apply_edits(
            project,
            [
                {
                    "row_id": row_ids[2],
                    "column_id": column_id,
                    "value": "predicate decoy",
                }
            ],
            spec={
                "action_id": "review.decision",
                "params": {"decision": "reject_clear"},
            },
        )
        assert project.get_values(sheet_id, column_id, row_ids=row_ids) == {
            row_ids[0]: None,
            row_ids[1]: "manual",
            row_ids[2]: "predicate decoy",
            row_ids[3]: "first:3",
        }

        replacement = _start_claimed_run(
            project,
            sheet_id=sheet_id,
            output_column_id=column_id,
            row_ids=[row_ids[0]],
            label="selected explicit replacement",
            replace_existing=True,
        )
        _declare(generations, replacement, column_id, write_mode="replace_scope")
        _write(
            project,
            replacement,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": column_id,
                    "value": "replacement",
                    "publication_effect": "publish_value",
                }
            ],
        )
        assert _seal(generations, replacement, column_id) == 1
        _release(project, replacement)

        live = project.get_values(sheet_id, column_id, row_ids=row_ids)
        assert live == {
            row_ids[0]: "replacement",
            row_ids[1]: "manual",
            row_ids[2]: "predicate decoy",
            row_ids[3]: "first:3",
        }
        filtered = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"generated": {"eq": "replacement"}}),
        )
        assert filtered.row_ids == [row_ids[0]]
        sorted_rows = resolve_sheet_filter_rows(
            project,
            sheet_id,
            sort=json.dumps([{"column": "generated", "dir": "asc"}]),
        )
        assert sorted_rows.row_ids == sorted(row_ids, key=lambda row_id: live[row_id])

        assert project.undo() == replacement.op_id
        assert project.get_values(sheet_id, column_id, row_ids=row_ids) == {
            row_ids[0]: None,
            row_ids[1]: "manual",
            row_ids[2]: "predicate decoy",
            row_ids[3]: "first:3",
        }
        assert (
            resolve_sheet_filter_rows(
                project,
                sheet_id,
                filter_=json.dumps({"generated": {"eq": "replacement"}}),
            ).row_ids
            == []
        )
    finally:
        project.close()

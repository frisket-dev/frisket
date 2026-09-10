from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from executor_harness import CatalogEntry, ExecutorCase, Gate, UndoRerun
from frisket.engine.store import Project
from action_test_helpers import run_typed_map_request


def _row_add_action(
    *,
    sheet_id: int,
    cells: dict[str, Any] | None = None,
    key: str = "row_add@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "row.add",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "cells": cells or {},
        },
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("People")
    columns = {
        "name": project.add_column(sheet_id, "name", type="text"),
        "role": project.add_column(sheet_id, "role", type="text"),
    }
    project.add_rows(sheet_id, [{"name": "Ada", "role": "analyst"}], columns)
    return {"sheet_id": sheet_id, "name_column_id": columns["name"]}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _row_add_action(
        sheet_id=seeded["sheet_id"], cells={"name": "Grace", "role": "engineer"}
    )


def _conflicting_cells_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary add, different cells.
    return _row_add_action(sheet_id=seeded["sheet_id"], cells={"name": "Katherine"})


def _unknown_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _row_add_action(
        sheet_id=seeded["sheet_id"],
        cells={"missing": "value"},
        key="unknown_column@sha256:v1",
    )


def _hidden_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _row_add_action(
        sheet_id=seeded["sheet_id"],
        cells={"name": "Hidden"},
        key="hidden_sheet@sha256:v1",
    )


def _hide_sheet(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (seeded["sheet_id"],))
    project.db.commit()


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    assert len(result.op_ids) == 1
    assert result.outputs[0].kind == "rows"
    assert result.outputs[0].name == "added_rows"
    assert len(result.outputs[0].row_ids) == 1
    added_row_id = result.outputs[0].row_ids[0]

    assert project.row_count(sheet_id) == 2
    assert (
        project.get_values(sheet_id, seeded["name_column_id"], row_ids=[added_row_id])[
            added_row_id
        ]
        == "Grace"
    )

    op = project.db.execute(
        "SELECT * FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    assert op is not None
    assert op["kind"] == "add_row"
    assert json.loads(op["spec"])["action_id"] == "row.add"
    assert json.loads(op["undo_info"]) == {"created_rows": [added_row_id]}

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "row.add"
    assert receipt.op_ids == list(result.op_ids)
    assert receipt.outputs[0].name == "added_rows"
    assert receipt.outputs[0].ref["kind"] == "added_rows"
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "added_row",
        "source_cell",
    }


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    added_row_id = first.outputs[0].row_ids[0]
    assert project.row_count(seeded["sheet_id"]) == 1
    hidden = project.db.execute(
        "SELECT hidden FROM rows WHERE id=?", (added_row_id,)
    ).fetchone()
    assert hidden is not None
    assert hidden["hidden"] == 1


CASES = [
    ExecutorCase(
        kind="row.add",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_sheet_columns",
                    "write_source_row",
                    "write_add_row_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_row_ref",
                    "sheet_not_found",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("sheet_id", "cells"),
            description_contains="source row",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "idempotency_conflict",
                _conflicting_cells_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate("unknown_column", _unknown_column_action, "invalid_row_ref"),
            Gate(
                "hidden_sheet",
                _hidden_sheet_action,
                "sheet_not_found",
                prepare=_hide_sheet,
            ),
        ),
        expect_counts={"rows": 1, "cells": 2, "ops": 1, "receipts": 1},
        check_state=_check_state,
        undo=UndoRerun(check_undone=_check_undone),
        request_style="typed",
    )
]


def test_row_add_rolls_back_mutation_when_receipt_insert_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "row-add-rollback.frisket")
    try:
        seeded = _seed(project, tmp_path)
        before_rows = project.row_count(seeded["sheet_id"])
        before_ops = int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0])

        def fail_insert(*args, **kwargs):
            raise RuntimeError("receipt unavailable")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_insert)
        result = run_typed_map_request(
            project,
            _row_add_action(
                sheet_id=seeded["sheet_id"],
                cells={"name": "Grace"},
                key="row-add-receipt-failure@sha256:stable",
            ),
            project_id="row-add-rollback",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert project.row_count(seeded["sheet_id"]) == before_rows
        assert (
            int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0])
            == before_ops
        )
        assert project.db.execute("SELECT 1 FROM receipts").fetchone() is None
    finally:
        project.close()

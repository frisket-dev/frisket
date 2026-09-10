from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from executor_harness import CatalogEntry, ExecutorCase, Gate, UndoRerun
from frisket.engine.store import Project


def _column_patch_action(
    *,
    column_id: int,
    format_: str | None,
    sheet_id: int | None = None,
    key: str = "column_patch@sha256:v1",
) -> dict[str, Any]:
    params: dict[str, Any] = {"column_id": column_id, "format": format_}
    if sheet_id is not None:
        params["sheet_id"] = sheet_id
    return {
        "action_id": "column.patch",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Notes")
    return {
        "sheet_id": sheet_id,
        "note_column_id": project.add_column(sheet_id, "note", type="text"),
    }


def _column_format(project: Project, column_id: int) -> str | None:
    row = project.db.execute(
        "SELECT format FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    assert row is not None
    return row["format"]


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _column_patch_action(
        column_id=seeded["note_column_id"],
        sheet_id=seeded["sheet_id"],
        format_="markdown",
    )


def _invalid_format_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _column_patch_action(
        column_id=seeded["note_column_id"],
        format_="sparkles",
        key="column_patch@sha256:invalid-format",
    )


def _conflicting_format_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary patch, different format value.
    return _column_patch_action(column_id=seeded["note_column_id"], format_=None)


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    note_column_id = seeded["note_column_id"]
    assert len(result.op_ids) == 1
    assert result.outputs[0].kind == "column"
    assert result.outputs[0].name == "note"
    assert result.outputs[0].column_id == note_column_id
    assert result.outputs[0].ref["kind"] == "column_patch"
    assert result.outputs[0].ref["format_before"] is None
    assert result.outputs[0].ref["format_after"] == "markdown"
    assert _column_format(project, note_column_id) == "markdown"

    op = project.db.execute(
        "SELECT kind, spec, undo_info FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    assert op is not None
    assert op["kind"] == "column.patch"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "column.patch"
    assert op_spec["params"]["format"] == "markdown"
    assert op_spec["params"]["params_hash"].startswith("sha256:")
    assert json.loads(op["undo_info"]) == {
        "column_formats": {str(note_column_id): None},
        "column_formats_after": {str(note_column_id): "markdown"},
    }

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "column.patch"
    assert receipt.op_ids == list(result.op_ids)
    assert receipt.outputs[0].name == "note"
    assert receipt.outputs[0].ref["kind"] == "column_patch"
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "column_format_transition",
    }


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    assert _column_format(project, seeded["note_column_id"]) is None


def _clear_format_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _column_patch_action(
        column_id=seeded["note_column_id"],
        sheet_id=seeded["sheet_id"],
        format_=None,
        key="column_patch_clear@sha256:v1",
    )


def _check_rerun(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    # Clearing an already-cleared format is a no-op transition, not an error.
    del first
    assert second.outputs[0].ref["format_before"] is None
    assert second.outputs[0].ref["format_after"] is None
    assert _column_format(project, seeded["note_column_id"]) is None


CASES = [
    ExecutorCase(
        kind="column.patch",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_column_metadata",
                    "update_column_format",
                    "write_column_patch_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "invalid_column_ref",
                    "invalid_column_format",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("column_id", "sheet_id", "format"),
            description_contains="display format",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_column_format",
                _invalid_format_action,
                "invalid_column_format",
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_format_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"ops": 1, "receipts": 1},
        check_state=_check_state,
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_clear_format_action,
            check_rerun=_check_rerun,
        ),
        request_style="typed",
    )
]

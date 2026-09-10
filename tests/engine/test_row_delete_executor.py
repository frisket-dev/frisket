from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    UndoRerun,
    operation_action,
)
from frisket.engine.store import Project
from helpers import run_cli
from action_test_helpers import write_json


def _row_delete_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    key: str = "row_delete@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "row.delete",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("People")
    columns = {"name": project.add_column(sheet_id, "name", type="text")}
    project.add_rows(
        sheet_id,
        [{"name": "Ada"}, {"name": "Grace"}, {"name": "Katherine"}],
        columns,
    )
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchall()
    ]
    return {
        "sheet_id": sheet_id,
        "row_ids": row_ids,
        # the primary delete targets the first and third rows
        "target_rows": [row_ids[0], row_ids[2]],
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _row_delete_action(
        sheet_id=seeded["sheet_id"], row_ids=seeded["target_rows"]
    )


def _empty_rows_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _row_delete_action(
        sheet_id=seeded["sheet_id"], row_ids=[], key="row_delete@sha256:empty"
    )


def _conflicting_rows_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary delete, different rows.
    return _row_delete_action(
        sheet_id=seeded["sheet_id"], row_ids=[seeded["row_ids"][1]]
    )


def _rehide_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Deleting an already-hidden row is rejected as an invalid ref.
    return _row_delete_action(
        sheet_id=seeded["sheet_id"],
        row_ids=[seeded["target_rows"][0]],
        key="rehide@sha256:v1",
    )


def _hidden_flags(project: Project, row_ids: list[int]) -> set[int]:
    return {
        int(
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (row_id,)
            ).fetchone()["hidden"]
        )
        for row_id in row_ids
    }


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    target_rows = seeded["target_rows"]
    assert len(result.op_ids) == 1
    assert result.outputs[0].kind == "rows"
    assert result.outputs[0].name == "deleted_rows"
    assert result.outputs[0].row_ids == target_rows
    assert result.outputs[0].ref["deleted"] == 2
    assert result.outputs[0].ref["total"] == 1

    # rows are hidden, not destroyed
    assert project.row_count(seeded["sheet_id"]) == 1
    assert _hidden_flags(project, target_rows) == {1}

    op = project.db.execute(
        "SELECT * FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    assert op is not None
    assert op["kind"] == "delete_rows"
    assert json.loads(op["spec"])["action_id"] == "row.delete"
    assert json.loads(op["undo_info"]) == {"deleted_rows": target_rows}

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "row.delete"
    assert receipt.op_ids == list(result.op_ids)
    assert receipt.outputs[0].name == "deleted_rows"
    assert receipt.outputs[0].ref["kind"] == "deleted_rows"
    assert {item.ref["kind"] for item in receipt.evidence} == {"deleted_row"}


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    assert project.row_count(seeded["sheet_id"]) == 3
    assert _hidden_flags(project, seeded["target_rows"]) == {0}
    seeded["delete_op_id"] = first.op_ids[0]


def _redo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return operation_action(
        "operation.redo",
        key="redo_row_delete@sha256:v1",
        expected_op_id=seeded["delete_op_id"],
    )


def _check_redo(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first, second
    assert project.row_count(seeded["sheet_id"]) == 1
    assert _hidden_flags(project, seeded["target_rows"]) == {1}


CASES = [
    ExecutorCase(
        kind="row.delete",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_sheet_rows",
                    "hide_source_rows",
                    "write_delete_rows_op",
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
            input_schema_properties=("sheet_id", "row_ids"),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate("empty_row_ids", _empty_rows_action, "invalid_params"),
            Gate(
                "idempotency_conflict",
                _conflicting_rows_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate(
                "rehide_hidden_row",
                _rehide_action,
                "invalid_row_ref",
                after_primary_run=True,
            ),
        ),
        expect_counts={"ops": 1, "receipts": 1},
        check_state=_check_state,
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_redo_action,
            check_rerun=_check_redo,
        ),
        request_style="typed",
    )
]


def _seed_import_action() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "People",
        "params": {
            "columns": [{"name": "name", "type": "text"}],
            "rows": [
                {"name": "Ada"},
                {"name": "Grace"},
                {"name": "Katherine"},
            ],
            "source": {
                "kind": "inline",
                "label": "row delete seed",
                "fingerprint": "sha256:row-delete-seed",
            },
        },
        "idempotency_key": "row_delete_seed@sha256:v1",
    }


def _run(project_path: Path, spec_path: Path) -> Any:
    return run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-row-delete",
        str(spec_path),
    )


def test_row_delete_handles_more_rows_than_sqlite_variable_chunk(
    tmp_path: Path,
) -> None:
    # A multi-select delete larger than the 900-id IN() chunk must not blow the
    # SQLite per-statement variable limit; it should resolve + soft-delete cleanly.
    from frisket.contracts.action import ActionResult

    project_path = tmp_path / "bulk-delete.frisket"
    project = Project.create(project_path, name="Bulk Delete")
    project.close()

    bulk = {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Many",
        "params": {
            "columns": [{"name": "name", "type": "text"}],
            "rows": [{"name": f"r{i}"} for i in range(1000)],
            "source": {
                "kind": "inline",
                "label": "bulk seed",
                "fingerprint": "sha256:bulk-seed",
            },
        },
        "idempotency_key": "bulk_seed@sha256:v1",
    }
    bulk_path = write_json(tmp_path, "bulk.json", bulk)
    assert _run(project_path, bulk_path).returncode == 0

    project = Project(project_path)
    try:
        sheet_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='Many'").fetchone()[
                "id"
            ]
        )
        row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                (sheet_id,),
            ).fetchall()
        ]
        assert len(row_ids) == 1000
    finally:
        project.close()

    delete_path = write_json(
        tmp_path,
        "bulk-delete.json",
        _row_delete_action(
            sheet_id=sheet_id, row_ids=row_ids, key="bulk_del@sha256:v1"
        ),
    )
    result = _run(project_path, delete_path)
    assert result.returncode == 0, result.stderr
    parsed = ActionResult.model_validate(json.loads(result.stdout))
    assert parsed.status == "completed"
    assert parsed.outputs[0].ref["deleted"] == 1000

    project = Project(project_path)
    try:
        assert project.row_count(sheet_id) == 0
    finally:
        project.close()


def test_row_delete_rejects_rows_on_another_sheet(tmp_path: Path) -> None:
    from frisket.contracts.action import ActionResult

    project_path = tmp_path / "cross-sheet.frisket"
    project = Project.create(project_path, name="Cross Sheet")
    project.close()

    seed_path = write_json(tmp_path, "seed.json", _seed_import_action())
    assert _run(project_path, seed_path).returncode == 0

    other = {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Other",
        "params": {
            "columns": [{"name": "name", "type": "text"}],
            "rows": [{"name": "Mae"}],
            "source": {
                "kind": "inline",
                "label": "other seed",
                "fingerprint": "sha256:other-seed",
            },
        },
        "idempotency_key": "other_seed@sha256:v1",
    }
    other_path = write_json(tmp_path, "other.json", other)
    assert _run(project_path, other_path).returncode == 0

    project = Project(project_path)
    try:
        people_id = int(
            project.db.execute("SELECT id FROM sheets WHERE name='People'").fetchone()[
                "id"
            ]
        )
        other_row_id = int(
            project.db.execute(
                "SELECT r.id FROM rows r JOIN sheets s ON s.id=r.sheet_id "
                "WHERE s.name='Other'"
            ).fetchone()["id"]
        )
    finally:
        project.close()

    # a row id from the Other sheet is not deletable through the People target
    spec_path = write_json(
        tmp_path,
        "cross.json",
        _row_delete_action(
            sheet_id=people_id, row_ids=[other_row_id], key="cross@sha256:v1"
        ),
    )
    result = _run(project_path, spec_path)
    assert result.returncode == 1
    parsed = ActionResult.model_validate(json.loads(result.stdout))
    assert parsed.errors[0].code == "invalid_row_ref"

    project = Project(project_path)
    try:
        # the cross-sheet row was untouched
        assert (
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (other_row_id,)
            ).fetchone()["hidden"]
            == 0
        )
    finally:
        project.close()

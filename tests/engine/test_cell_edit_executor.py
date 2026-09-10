from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, UndoRerun
from frisket.engine.store import Project


def _cell_edit_action(
    *,
    row_id: int,
    column_id: int,
    value: Any,
    key: str = "cell_edit@sha256:v1",
) -> dict[str, Any]:
    return _cell_edit_batch_action(
        [{"row_id": row_id, "column_id": column_id, "value": value}], key=key
    )


def _cell_edit_batch_action(edits: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    return {
        "action_id": "cell.edit",
        "scope": {"kind": "project"},
        "params": {"edits": edits},
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
    row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchone()["id"]
    )
    return {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "name_column_id": columns["name"],
    }


def _live_name(project: Project, seeded: dict[str, Any]) -> Any:
    values = project.get_values(
        seeded["sheet_id"], seeded["name_column_id"], row_ids=[seeded["row_id"]]
    )
    return values[seeded["row_id"]]


def _write_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("ops", "edits", "receipts")
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cell_edit_action(
        row_id=seeded["row_id"], column_id=seeded["name_column_id"], value="Alicia"
    )


def _conflicting_value_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary edit, different value.
    return _cell_edit_action(
        row_id=seeded["row_id"], column_id=seeded["name_column_id"], value="Ada"
    )


def _hidden_row_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cell_edit_action(
        row_id=seeded["row_id"],
        column_id=seeded["name_column_id"],
        value="Hidden",
        key="hidden_cell_edit@sha256:v1",
    )


def _hide_row(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (seeded["row_id"],))
    project.db.commit()


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert len(result.op_ids) == 1
    edit_op_id = result.op_ids[0]
    assert _live_name(project, seeded) == "Alicia"

    op = project.db.execute("SELECT * FROM ops WHERE id=?", (edit_op_id,)).fetchone()
    assert op is not None
    assert op["kind"] == "edit"
    assert json.loads(op["spec"])["action_id"] == "cell.edit"

    overlay = project.db.execute(
        "SELECT * FROM edits WHERE op_id=?", (edit_op_id,)
    ).fetchone()
    assert overlay is not None
    assert overlay["row_id"] == seeded["row_id"]
    assert overlay["column_id"] == seeded["name_column_id"]
    assert json.loads(overlay["value"]) == "Alicia"

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "cell.edit"
    assert receipt.op_ids == [edit_op_id]
    assert receipt.outputs[0].name == "manual_edits"
    assert receipt.evidence[0].ref["kind"] == "manual_edit_overlay"


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    # The overlay is retracted: the live value is the source value again.
    assert _live_name(project, seeded) == "Ada"


def _rerun_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _cell_edit_action(
        row_id=seeded["row_id"],
        column_id=seeded["name_column_id"],
        value="Alicia",
        key="cell_edit@sha256:rerun",
    )


def _check_rerun(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first, second
    assert _live_name(project, seeded) == "Alicia"


CASES = [
    ExecutorCase(
        kind="cell.edit",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_source_cell",
                    "write_edit_overlay",
                    "write_edit_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "cell_target_not_found",
                    "column_value_validation_failed",
                    "output_column_busy",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("edits",),
            description_contains="manual edit",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "idempotency_conflict",
                _conflicting_value_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate(
                "cell_target_not_found",
                _hidden_row_action,
                "cell_target_not_found",
                prepare=_hide_row,
            ),
        ),
        expect_counts={"ops": 1, "edits": 1, "receipts": 1},
        check_state=_check_state,
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_rerun_action,
            check_rerun=_check_rerun,
        ),
        request_style="typed",
    )
]


def test_migrated_cell_and_column_legacy_envelopes_are_rejected() -> None:
    from frisket.contracts.action_validation import validate_action_spec

    for kind, params in (
        (
            "cell.edit",
            {
                "edits": [
                    {
                        "target": {
                            "kind": "cell",
                            "row_id": 1,
                            "column_id": 2,
                        },
                        "value": None,
                    }
                ]
            },
        ),
        (
            "cell.edit_query",
            {"query": {}, "column_id": 2, "value": None},
        ),
        (
            "column.set_type",
            {
                "target": {"kind": "column", "column_id": 2},
                "type": "category",
            },
        ),
    ):
        validation = validate_action_spec(
            {
                "schema_version": "frisket.action.v2",
                "kind": kind,
                "capabilities": ["project:write"],
                "params": params,
                "idempotency_key": f"legacy-{kind}@sha256:rejected",
            }
        )
        assert validation.ok is False
        assert validation.error is not None
        assert validation.error.code == "unsupported_action_kind"


def test_cell_edit_batch_is_atomic_and_nullable(tmp_path: Path) -> None:
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "cell-edit-batch.frisket")
    try:
        sheet_id = project.add_sheet("Values")
        columns = {
            "number": project.add_column(sheet_id, "number", "integer"),
            "note": project.add_column(sheet_id, "note", "text"),
        }
        [row_id] = project.add_rows(
            sheet_id, [{"number": 7, "note": "before"}], columns
        )
        before = _write_counts(project)
        invalid = _cell_edit_batch_action(
            [
                {"row_id": row_id, "column_id": columns["note"], "value": None},
                {
                    "row_id": row_id,
                    "column_id": columns["number"],
                    "value": "not-an-integer",
                },
            ],
            key="cell-edit-atomic@sha256:invalid",
        )
        failed = run_action_spec(project, invalid, project_id="cell-edit-batch")
        assert failed.status == "failed"
        assert failed.errors[0].code == "column_value_validation_failed"
        assert project.get_values(sheet_id, columns["note"], row_ids=[row_id]) == {
            row_id: "before"
        }
        assert _write_counts(project) == before

        completed = run_action_spec(
            project,
            _cell_edit_batch_action(
                [
                    {
                        "row_id": row_id,
                        "column_id": columns["note"],
                        "value": None,
                    },
                    {
                        "row_id": row_id,
                        "column_id": columns["number"],
                        "value": None,
                    },
                ],
                key="cell-edit-atomic@sha256:null",
            ),
            project_id="cell-edit-batch",
        )
        assert completed.status == "completed", completed.errors
        assert project.get_values(sheet_id, columns["note"], row_ids=[row_id]) == {
            row_id: None
        }
        assert project.get_values(sheet_id, columns["number"], row_ids=[row_id]) == {
            row_id: None
        }
    finally:
        project.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_cell_edit_rejects_nonfinite_json_without_writes(
    tmp_path: Path, value: float
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "cell-edit-nonfinite.frisket")
    try:
        seeded = _seed(project, tmp_path)
        before = _write_counts(project)
        result = run_action_spec(
            project,
            _cell_edit_action(
                row_id=seeded["row_id"],
                column_id=seeded["name_column_id"],
                value=value,
                key=f"cell-edit-nonfinite@sha256:{repr(value)}",
            ),
            project_id="cell-edit-nonfinite",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert _write_counts(project) == before
    finally:
        project.close()


def test_cell_edit_replay_precedes_live_target_lookup(tmp_path: Path) -> None:
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "cell-edit-replay.frisket")
    try:
        seeded = _seed(project, tmp_path)
        before = _write_counts(project)
        action = _make_action(seeded)
        first = run_action_spec(project, action, project_id="cell-edit-replay")
        assert first.status == "completed", first.errors
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (seeded["row_id"],))
        project.db.commit()

        replay = run_action_spec(project, action, project_id="cell-edit-replay")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        assert replay.outputs == first.outputs
        assert _write_counts(project) == {
            **before,
            "ops": before["ops"] + 1,
            "edits": before["edits"] + 1,
            "receipts": before["receipts"] + 1,
        }
    finally:
        project.close()


def _cell_edit_query_action(
    *, sheet_id: int, column_id: int, key: str
) -> dict[str, Any]:
    return {
        "action_id": "cell.edit_query",
        "scope": {"kind": "project"},
        "params": {
            "query": {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet_id},
                "filter": {"status": {"eq": "open"}},
                "sort": [{"column": "sequence", "dir": "asc"}],
            },
            "column_id": column_id,
            "value": "closed",
        },
        "idempotency_key": key,
    }


@pytest.mark.parametrize("max_rows", [True, -1, 1.5, "2"])
def test_cell_edit_query_limits_validate_max_rows(max_rows: object) -> None:
    from frisket.engine.executor import CellEditQueryLimits

    with pytest.raises(
        ValueError, match="max_rows must be a non-negative integer or None"
    ):
        CellEditQueryLimits(max_rows=max_rows)  # type: ignore[arg-type]


def _operation_action(kind: str, *, expected_op_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {"expected_op_id": expected_op_id},
        "idempotency_key": key,
    }


def test_cell_edit_query_updates_1001_selected_rows_with_exact_replay_and_undo(
    tmp_path: Path,
) -> None:
    """Selector-backed edits scale independently from inline edit payloads."""
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "query-edit.frisket", name="Query edit")
    try:
        sheet_id = project.add_sheet("Tasks")
        columns = {
            "sequence": project.add_column(sheet_id, "sequence", "integer"),
            "status": project.add_column(sheet_id, "status", "text"),
        }
        selected_row_ids = project.add_rows(
            sheet_id,
            [{"sequence": index, "status": "open"} for index in range(1_001)],
            columns,
        )
        action = _cell_edit_query_action(
            sheet_id=sheet_id,
            column_id=columns["status"],
            key="cell_edit_query@sha256:1001",
        )

        result = run_action_spec(project, action, project_id="query-edit")

        assert result.status == "completed", result.errors
        assert result.outputs[0].row_ids == selected_row_ids
        assert project.get_values(
            sheet_id, columns["status"], row_ids=selected_row_ids
        ) == dict.fromkeys(selected_row_ids, "closed")
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM edits WHERE op_id=?", (result.op_ids[0],)
            ).fetchone()[0]
            == 1_001
        )

        replay = run_action_spec(project, action, project_id="query-edit")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert replay.op_ids == result.op_ids
        assert replay.outputs == result.outputs
        assert replay.outputs[0].sheet_id == sheet_id
        assert replay.outputs[0].column_id == columns["status"]

        undone = run_action_spec(
            project,
            _operation_action(
                "operation.undo",
                expected_op_id=result.op_ids[0],
                key="operation_undo_query_edit@sha256:1001",
            ),
            project_id="query-edit",
        )
        assert undone.status == "completed", undone.errors
        assert project.get_values(
            sheet_id, columns["status"], row_ids=selected_row_ids
        ) == dict.fromkeys(selected_row_ids, "open")
    finally:
        project.close()


def test_cell_edit_query_row_limit_refuses_before_values_or_targets_materialize(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from frisket.engine.executor import CellEditQueryLimits, ExecutorDeps
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "limited-query-edit.frisket", name="Query edit")
    try:
        sheet_id = project.add_sheet("Tasks")
        columns = {
            "sequence": project.add_column(sheet_id, "sequence", "integer"),
            "status": project.add_column(sheet_id, "status", "text"),
        }
        project.add_rows(
            sheet_id,
            [{"sequence": index, "status": "open"} for index in range(3)],
            columns,
        )
        before = _write_counts(project)

        def values_must_not_be_read(*_args, **_kwargs):
            raise AssertionError("over-limit query must not materialize cell values")

        monkeypatch.setattr(project, "get_values_with_refs", values_must_not_be_read)
        result = run_action_spec(
            project,
            _cell_edit_query_action(
                sheet_id=sheet_id,
                column_id=columns["status"],
                key="cell_edit_query@sha256:limited",
            ),
            project_id="limited-query-edit",
            deps=ExecutorDeps(cell_edit_query_limits=CellEditQueryLimits(max_rows=2)),
        )

        assert result.status == "failed"
        assert result.errors[0].code == "query_rowset_too_large"
        assert result.errors[0].message == (
            "cell.edit_query exceeds the deployment row limit of 2"
        )
        assert result.errors[0].details == {"row_count": 3, "max_rows": 2}
        assert _write_counts(project) == before
    finally:
        project.close()


def test_cell_edit_query_row_limit_admits_the_exact_total(tmp_path: Path) -> None:
    from frisket.engine.executor import CellEditQueryLimits, ExecutorDeps
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "exact-query-edit.frisket", name="Query edit")
    try:
        sheet_id = project.add_sheet("Tasks")
        columns = {
            "sequence": project.add_column(sheet_id, "sequence", "integer"),
            "status": project.add_column(sheet_id, "status", "text"),
        }
        row_ids = project.add_rows(
            sheet_id,
            [{"sequence": index, "status": "open"} for index in range(2)],
            columns,
        )

        result = run_action_spec(
            project,
            _cell_edit_query_action(
                sheet_id=sheet_id,
                column_id=columns["status"],
                key="cell_edit_query@sha256:exact",
            ),
            project_id="exact-query-edit",
            deps=ExecutorDeps(cell_edit_query_limits=CellEditQueryLimits(max_rows=2)),
        )

        assert result.status == "completed", result.errors
        assert result.outputs[0].row_ids == row_ids
    finally:
        project.close()


def test_cell_edit_query_rejects_claimed_column_without_writes(tmp_path: Path) -> None:
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.output_claims import OutputColumnClaimStore

    project = Project.create(tmp_path / "claimed-query-edit.frisket")
    try:
        sheet_id = project.add_sheet("Tasks")
        columns = {
            "sequence": project.add_column(sheet_id, "sequence", "integer"),
            "status": project.add_column(sheet_id, "status", "text"),
        }
        project.add_rows(sheet_id, [{"sequence": 1, "status": "open"}], columns)
        before = _write_counts(project)
        _claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["status"],
            action_kind="map.ask",
            claim_token="claim:query-edit-test",
            lease_seconds=None,
        )
        assert conflict is None

        result = run_action_spec(
            project,
            _cell_edit_query_action(
                sheet_id=sheet_id,
                column_id=columns["status"],
                key="cell-edit-query-claimed@sha256:v1",
            ),
            project_id="claimed-query-edit",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "output_column_busy"
        assert _write_counts(project) == before
    finally:
        project.close()


def test_inline_cell_edit_retains_1000_edit_payload_limit() -> None:
    from pydantic import ValidationError

    from frisket.actions.mutations import CellEditParams

    exact = CellEditParams.model_validate(
        {
            "edits": [
                {"row_id": row_id, "column_id": 1, "value": "closed"}
                for row_id in range(1, 1_001)
            ]
        }
    )
    assert len(exact.edits) == 1_000

    with pytest.raises(ValidationError):
        CellEditParams.model_validate(
            {
                "edits": [
                    {"row_id": row_id, "column_id": 1, "value": "closed"}
                    for row_id in range(1, 1_002)
                ]
            }
        )


def test_adjacent_destructive_payload_limits_remain_exact() -> None:
    """Scaling selector work does not widen unrelated destructive payloads."""
    from pydantic import ValidationError

    from frisket.actions.mutations import RowDeleteParams
    from frisket.actions.system import validate_root_action

    assert (
        len(
            RowDeleteParams.model_validate(
                {"sheet_id": 1, "row_ids": list(range(1, 10_001))}
            ).row_ids
        )
        == 10_000
    )
    with pytest.raises(ValidationError):
        RowDeleteParams.model_validate(
            {"sheet_id": 1, "row_ids": list(range(1, 10_002))}
        )

    action = {
        "action_id": "media.enclosure_materialize",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": 1,
            "row_ids": list(range(1, 501)),
        },
        "params": {},
        "idempotency_key": "enclosure-exact-limit",
    }
    exact = validate_root_action(action)
    over = validate_root_action(
        {
            **action,
            "scope": {**action["scope"], "row_ids": list(range(1, 502))},
            "idempotency_key": "enclosure-over-limit",
        }
    )

    assert exact.ok is True, exact.error
    assert over.ok is False
    assert over.error is not None
    assert over.error.code == "invalid_action_request"
    assert "1 to 500 selected rows" in over.error.message

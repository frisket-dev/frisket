from __future__ import annotations

from pathlib import Path
from typing import Any

from action_test_helpers import run_typed_map_request, typed_map_request
from frisket.engine.executor.actions import run_action_spec as run_legacy_action_spec
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore


PROJECT_ID = "project-output-claims"


def _seed_project(tmp_path: Path) -> tuple[Project, int, list[int], dict[str, int]]:
    project = Project.create(tmp_path / "output-claims.frisket", name="Output Claims")
    sheet_id = project.add_sheet("Notes")
    columns = {
        "name": project.add_column(sheet_id, "name", type="text"),
        "note": project.add_column(sheet_id, "note", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"name": "Ada", "note": "met the mayor"},
            {"name": "Grace", "note": "filed a complaint"},
        ],
        columns,
    )
    return project, sheet_id, row_ids, columns


def _map_template_action(
    sheet_id: int,
    *,
    key: str,
    output_name: str = "rendered_note",
) -> dict[str, Any]:
    return typed_map_request(
        "map.template",
        sheet_id,
        params={"template": {"text": "{{name}}: {{note}}"}},
        output_names={"rendered": output_name},
        idempotency_key=key,
    )


def run_action_spec(
    project: Project,
    action: dict[str, Any],
    *,
    project_id: str,
):
    if "action_id" in action:
        return run_typed_map_request(project, action, project_id=project_id)
    return run_legacy_action_spec(project, action, project_id=project_id)


def _cell_edit_action(
    *,
    row_id: int,
    column_id: int,
    value: Any,
    key: str,
) -> dict[str, Any]:
    return {
        "action_id": "cell.edit",
        "scope": {"kind": "project"},
        "params": {
            "edits": [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": value,
                }
            ]
        },
        "idempotency_key": key,
    }


def _column_patch_action(*, column_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "column.patch",
        "scope": {"kind": "project"},
        "params": {"column_id": column_id, "format": "markdown"},
        "idempotency_key": key,
    }


def _column_set_type_action(*, column_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "column.set_type",
        "scope": {"kind": "project"},
        "params": {"column_id": column_id, "type": "text"},
        "idempotency_key": key,
    }


def _operation_action(kind: str, *, expected_op_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {"expected_op_id": expected_op_id},
        "idempotency_key": key,
    }


def _run_backfill_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "rendered_note"},
        "idempotency_key": key,
    }


def _regex_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "code": "result = row['note']",
            "return_schema": {"type": "string"},
            "output_routes": [
                {
                    "name": "rendered_note",
                    "path": "$",
                    "target": {
                        "kind": "column",
                        "type": "text",
                    },
                }
            ],
        },
        "idempotency_key": key,
    }


def _output_column_id(project: Project, *, sheet_id: int, name: str) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=?", (sheet_id, name)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _active_claim_count(project: Project, output_name: str) -> int:
    return int(
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims "
            "WHERE output_name=? AND status='active'",
            (output_name,),
        ).fetchone()[0]
    )


def _claim_output(
    project: Project,
    *,
    sheet_id: int,
    output_name: str = "rendered_note",
    token: str = "claim:test-running",
) -> str:
    _claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=[output_name],
        action_kind="map.template",
        claim_token=token,
        lease_seconds=None,
        details={"fixture": "active-running-output"},
    )
    assert conflict is None
    assert _active_claim_count(project, output_name) == 1
    return token


def test_output_claim_group_supports_multiple_output_names(tmp_path: Path) -> None:
    project, sheet_id, _row_ids, _columns = _seed_project(tmp_path)
    try:
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["first_output", "second_output"],
            action_kind="map.multi_output_fixture",
            claim_token="claim:multi-output",
            lease_seconds=None,
        )
        assert conflict is None
        assert [row["output_name"] for row in claims] == [
            "first_output",
            "second_output",
        ]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE claim_token=? AND status='active'",
                ("claim:multi-output",),
            ).fetchone()[0]
            == 2
        )

        _claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["second_output"],
            action_kind="map.other",
            claim_token="claim:conflicting-output",
            lease_seconds=None,
        )
        assert conflict is not None
        assert conflict["output_name"] == "second_output"

        OutputColumnClaimStore(project).release(
            claim_token="claim:multi-output", status="released"
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE claim_token=? AND status='active'",
                ("claim:multi-output",),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_output_claim_blocks_competing_run_and_releases_after_success(
    tmp_path: Path,
) -> None:
    project, sheet_id, _row_ids, _columns = _seed_project(tmp_path)
    try:
        token = _claim_output(project, sheet_id=sheet_id)

        blocked = run_action_spec(
            project,
            _map_template_action(sheet_id, key="map_template_busy@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert blocked.status == "failed"
        assert blocked.errors[0].code == "output_column_busy"
        assert blocked.errors[0].field == "output_names"
        assert blocked.errors[0].details["output_name"] == "rendered_note"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
                ("map_template_busy@sha256:v1",),
            ).fetchone()[0]
            == 0
        )

        OutputColumnClaimStore(project).release(claim_token=token, status="failed")
        completed = run_action_spec(
            project,
            _map_template_action(sheet_id, key="map_template_after_claim@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert completed.status == "completed"
        assert completed.receipt_id is not None
        assert completed.run_id is not None
        assert _active_claim_count(project, "rendered_note") == 0
        assert (
            _output_column_id(project, sheet_id=sheet_id, name="rendered_note")
            == completed.outputs[0].column_id
        )
        claim_rows = project.db.execute(
            "SELECT status, run_id, receipt_id FROM output_column_claims "
            "WHERE output_name='rendered_note'"
        ).fetchall()
        assert sorted(row["status"] for row in claim_rows) == ["failed", "released"]
        completed_claim = next(
            row for row in claim_rows if row["receipt_id"] == completed.receipt_id
        )
        assert completed_claim["status"] == "released"
        assert int(completed_claim["run_id"]) == completed.run_id
    finally:
        project.close()


def test_output_claim_blocks_manual_and_metadata_edits(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_ids, _columns = _seed_project(tmp_path)
    try:
        generated = run_action_spec(
            project,
            _map_template_action(sheet_id, key="map_template_for_edits@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert generated.status == "completed"
        output_column_id = _output_column_id(
            project, sheet_id=sheet_id, name="rendered_note"
        )
        _claim_output(project, sheet_id=sheet_id)

        edit = run_action_spec(
            project,
            _cell_edit_action(
                row_id=row_ids[0],
                column_id=output_column_id,
                value="manual override",
                key="cell_edit_claimed_output@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert edit.status == "failed"
        assert edit.errors[0].code == "output_column_busy"
        assert edit.errors[0].details["column_id"] == output_column_id

        patch = run_action_spec(
            project,
            _column_patch_action(
                column_id=output_column_id,
                key="column_patch_claimed_output@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert patch.status == "failed"
        assert patch.errors[0].code == "output_column_busy"

        set_type = run_action_spec(
            project,
            _column_set_type_action(
                column_id=output_column_id,
                key="column_set_type_claimed_output@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert set_type.status == "failed"
        assert set_type.errors[0].code == "output_column_busy"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
                ("column_set_type_claimed_output@sha256:v1",),
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM edits WHERE column_id=?", (output_column_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_output_claim_blocks_run_backfill_on_claimed_column(tmp_path: Path) -> None:
    project, sheet_id, _row_ids, _columns = _seed_project(tmp_path)
    try:
        generated = run_action_spec(
            project,
            _regex_action(sheet_id, key="regex_for_backfill@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert generated.status == "completed"
        _claim_output(project, sheet_id=sheet_id)

        backfill = run_action_spec(
            project,
            _run_backfill_action(sheet_id, key="run_backfill_claimed_output@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert backfill.status == "failed"
        assert backfill.errors[0].code == "output_column_busy", backfill.errors[
            0
        ].message
        # The shared claim owner reports the semantic backfill target field.
        assert backfill.errors[0].field == "params.column"
        assert backfill.errors[0].details["output_name"] == "rendered_note"
    finally:
        project.close()


def test_output_claim_blocks_operation_transitions_that_touch_claimed_column(
    tmp_path: Path,
) -> None:
    project, sheet_id, row_ids, _columns = _seed_project(tmp_path)
    try:
        generated = run_action_spec(
            project,
            _map_template_action(sheet_id, key="map_template_for_undo@sha256:v1"),
            project_id=PROJECT_ID,
        )
        assert generated.status == "completed"
        assert generated.op_ids
        map_op_id = generated.op_ids[0]
        output_column_id = _output_column_id(
            project, sheet_id=sheet_id, name="rendered_note"
        )

        map_claim = _claim_output(
            project, sheet_id=sheet_id, token="claim:map-undo-running"
        )
        undo_map = run_action_spec(
            project,
            _operation_action(
                "operation.undo",
                expected_op_id=map_op_id,
                key="undo_claimed_map@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert undo_map.status == "failed"
        assert undo_map.errors[0].code == "output_column_busy"
        assert undo_map.errors[0].details["op_id"] == map_op_id
        assert project.op_cursor == map_op_id
        OutputColumnClaimStore(project).release(claim_token=map_claim, status="failed")

        edit = run_action_spec(
            project,
            _cell_edit_action(
                row_id=row_ids[0],
                column_id=output_column_id,
                value="manual before later run",
                key="cell_edit_before_claim@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert edit.status == "completed"
        assert edit.op_ids
        edit_op_id = edit.op_ids[0]
        _claim_output(
            project,
            sheet_id=sheet_id,
            token="claim:manual-overlay-undo-running",
        )

        undo_edit = run_action_spec(
            project,
            _operation_action(
                "operation.undo",
                expected_op_id=edit_op_id,
                key="undo_claimed_edit@sha256:v1",
            ),
            project_id=PROJECT_ID,
        )
        assert undo_edit.status == "failed"
        assert undo_edit.errors[0].code == "output_column_busy"
        assert undo_edit.errors[0].details["op_id"] == edit_op_id
        op = project.db.execute(
            "SELECT status FROM ops WHERE id=?", (edit_op_id,)
        ).fetchone()
        assert op is not None
        assert op["status"] == "applied"
        assert project.op_cursor == edit_op_id
    finally:
        project.close()

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, operation_action
from frisket.engine.store import Project

_CATALOG = CatalogEntry(
    execution_mode="whole_project",
    async_mode="sync",
    writes_project=True,
    receipt_policy="writes_receipt",
    required_capabilities=("project:write",),
    side_effects=frozenset(
        {
            "read_operation_cursor",
            "transition_operation_status",
            "move_op_cursor",
            "apply_undo_info",
            "write_receipt",
        }
    ),
    error_codes=frozenset(
        {
            "operation_unavailable",
            "operation_mismatch",
            "irreversible_barrier",
            "project_state_corrupt",
            "idempotency_conflict",
        }
    ),
    cost_policy_kind="none",
)


def _map_named_results_action(sheet_id: int) -> dict[str, Any]:
    code = "\n".join(
        [
            "words = row['transcript'].split()",
            "person = words[0]",
            "result = {",
            "    'entities': [{'name': person, 'title': 'speaker'}],",
            "}",
        ]
    )
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["title", "transcript"],
            "code": code,
            "return_schema": {
                "type": "object",
                "required": ["entities"],
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "title"],
                            "properties": {
                                "name": {"type": "string"},
                                "title": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "output_routes": [
                {
                    "name": "entities",
                    "path": "$.entities",
                    "target": {
                        "kind": "named_result",
                        "schema": "entity_list",
                        "may_feed": ["derive.table_from_list"],
                    },
                },
            ],
        },
        "idempotency_key": "operation_source_map@sha256:v1",
    }


def _seed_chain(project: Project, tmp_path: Path) -> dict[str, Any]:
    """import.rows -> map.python (named result) -> derive.table_from_list, all
    in-process, so the undoable target is a materialized child sheet."""
    from frisket.engine.executor import actions as executor_actions

    del tmp_path
    seed_import = {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Transcripts",
        "params": {
            "columns": [
                {"name": "title", "type": "text"},
                {"name": "transcript", "type": "text"},
            ],
            "rows": [
                {
                    "title": "Episode 1",
                    "transcript": "Alice founded Newsroom Labs in Brooklyn",
                },
                {
                    "title": "Episode 2",
                    "transcript": "Bob joined Civic Data in Queens",
                },
            ],
            "source": {
                "kind": "inline",
                "label": "operation seed",
                "fingerprint": "sha256:operation-seed",
            },
        },
        "idempotency_key": "operation_seed@sha256:v1",
    }
    imported = executor_actions.run_action_spec(
        project, seed_import, project_id="project-operation"
    )
    assert imported.status == "completed", imported.errors
    sheet_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Transcripts'").fetchone()[
            "id"
        ]
    )
    mapped = executor_actions.run_action_spec(
        project, _map_named_results_action(sheet_id), project_id="project-operation"
    )
    assert mapped.status == "completed", mapped.errors
    entities_column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='__result_entities'",
            (sheet_id,),
        ).fetchone()["id"]
    )
    derived = executor_actions.run_action_spec(
        project,
        {
            "action_id": "derive.table_from_list",
            "scope": {"kind": "project"},
            "sheet_name": "Entities",
            "params": {
                "source": {
                    "kind": "named_result",
                    "sheet_id": sheet_id,
                    "column_id": entities_column_id,
                    "run_id": mapped.run_id,
                    "route": "entities",
                    "schema": "entity_list",
                },
                "item_schema": {
                    "type": "object",
                    "required": ["name", "title"],
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                    },
                },
                "columns": [
                    {"name": "name", "path": "$.name", "type": "text"},
                    {"name": "title", "path": "$.title", "type": "text"},
                ],
            },
            "idempotency_key": "operation_derive_entities@sha256:v1",
        },
        project_id="project-operation",
    )
    assert derived.status == "completed", derived.errors
    derive_op_id = derived.op_ids[0]
    assert derive_op_id == 3
    child_sheet_id = next(
        output.sheet_id for output in derived.outputs if output.kind == "sheet"
    )
    child_row_ids = next(
        output.row_ids for output in derived.outputs if output.kind == "rows"
    )
    assert project.op_cursor == derive_op_id
    return {
        "derive_op_id": derive_op_id,
        "child_sheet_id": child_sheet_id,
        "child_row_ids": child_row_ids,
    }


def _seed_chain_undone(project: Project, tmp_path: Path) -> dict[str, Any]:
    from frisket.engine.executor import actions as executor_actions

    seeded = _seed_chain(project, tmp_path)
    undone = executor_actions.run_action_spec(
        project,
        _undo_action(seeded),
        project_id="project-operation",
    )
    assert undone.status == "completed", undone.errors
    return seeded


def _undo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return operation_action(
        "operation.undo",
        key="operation_undo_derive@sha256:v1",
        expected_op_id=seeded["derive_op_id"],
    )


def _redo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return operation_action(
        "operation.redo",
        key="operation_redo_derive@sha256:v1",
        expected_op_id=seeded["derive_op_id"],
    )


def _mismatch_undo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return operation_action(
        "operation.undo",
        key="operation_undo_mismatch@sha256:v1",
        expected_op_id=999,
    )


def _redo_without_undo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    action = operation_action(
        "operation.redo",
        key="operation_redo_unavailable@sha256:v1",
        expected_op_id=0,
    )
    del action["params"]["expected_op_id"]
    return action


def _undo_conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    # Same key as the primary undo, different expected op.
    return operation_action(
        "operation.undo",
        key="operation_undo_derive@sha256:v1",
        expected_op_id=2,
    )


def _mismatch_redo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return operation_action(
        "operation.redo",
        key="operation_redo_mismatch@sha256:v1",
        expected_op_id=999,
    )


def _redo_conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return operation_action(
        "operation.redo",
        key="operation_redo_derive@sha256:v1",
        expected_op_id=2,
    )


def _operation_receipt(project: Project, result: Any) -> Any:
    from frisket.contracts.action import Receipt

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] is None
    return Receipt.model_validate(json.loads(receipt_row["body"]))


def _check_undo_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    derive_op_id = seeded["derive_op_id"]
    assert result.op_ids == [derive_op_id]
    transition = next(output for output in result.outputs if output.kind == "operation")
    assert transition.ref["kind"] == "operation_status_transition"
    assert transition.ref["direction"] == "undo"
    assert transition.ref["op_id"] == derive_op_id
    assert transition.ref["status_before"] == "applied"
    assert transition.ref["status_after"] == "undone"

    assert project.op_cursor == derive_op_id - 1
    op = project.db.execute(
        "SELECT status FROM ops WHERE id=?", (derive_op_id,)
    ).fetchone()
    assert op["status"] == "undone"
    child = project.db.execute(
        "SELECT hidden, name FROM sheets WHERE id=?", (seeded["child_sheet_id"],)
    ).fetchone()
    assert int(child["hidden"]) == 1
    assert child["name"] == f"Entities (undone:{seeded['child_sheet_id']})"
    hidden_rows = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? AND hidden=1 ORDER BY position",
        (seeded["child_sheet_id"],),
    ).fetchall()
    assert [int(row["id"]) for row in hidden_rows] == seeded["child_row_ids"]

    receipt = _operation_receipt(project, result)
    assert receipt.action_kind == "operation.undo"
    assert receipt.op_ids == [derive_op_id]
    assert receipt.inputs[0].ref["kind"] == "target_operation"
    assert receipt.outputs[0].ref["kind"] == "operation_status_transition"
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "operation_cursor",
        "operation_undo_info",
        "operation_snapshot",
    }


def _check_redo_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    derive_op_id = seeded["derive_op_id"]
    assert result.op_ids == [derive_op_id]
    transition = next(output for output in result.outputs if output.kind == "operation")
    assert transition.ref["direction"] == "redo"
    assert transition.ref["status_before"] == "undone"
    assert transition.ref["status_after"] == "applied"

    assert project.op_cursor == derive_op_id
    op = project.db.execute(
        "SELECT status FROM ops WHERE id=?", (derive_op_id,)
    ).fetchone()
    assert op["status"] == "applied"
    child = project.db.execute(
        "SELECT hidden, name FROM sheets WHERE id=?", (seeded["child_sheet_id"],)
    ).fetchone()
    assert int(child["hidden"]) == 0
    assert child["name"] == "Entities"
    visible_rows = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position",
        (seeded["child_sheet_id"],),
    ).fetchall()
    assert [int(row["id"]) for row in visible_rows] == seeded["child_row_ids"]

    receipt = _operation_receipt(project, result)
    assert receipt.action_kind == "operation.redo"


CASES = [
    ExecutorCase(
        kind="operation.undo",
        catalog=_CATALOG,
        seed=_seed_chain,
        make_action=_undo_action,
        gates=(
            Gate("operation_mismatch", _mismatch_undo_action, "operation_mismatch"),
            Gate(
                "redo_before_any_undo",
                _redo_without_undo_action,
                "operation_unavailable",
            ),
            Gate(
                "idempotency_conflict",
                _undo_conflict_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"receipts": 1},
        check_state=_check_undo_state,
        request_style="typed",
    ),
    ExecutorCase(
        kind="operation.redo",
        catalog=_CATALOG,
        seed=_seed_chain_undone,
        make_action=_redo_action,
        gates=(
            Gate("operation_mismatch", _mismatch_redo_action, "operation_mismatch"),
            Gate(
                "idempotency_conflict",
                _redo_conflict_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"receipts": 1},
        check_state=_check_redo_state,
        request_style="typed",
    ),
]


def test_undo_on_empty_project_is_unavailable(tmp_path: Path) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "empty.frisket", name="Empty Operation")
    try:
        action = operation_action(
            "operation.undo", key="empty_undo@sha256:v1", expected_op_id=0
        )
        del action["params"]["expected_op_id"]
        result = executor_actions.run_action_spec(
            project, action, project_id="project-empty-operation"
        )
        assert result.status == "failed"
        assert result.errors[0].code == "operation_unavailable"
    finally:
        project.close()


def test_corrupt_undo_info_rolls_back_and_reports_corrupt_state(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "corrupt.frisket", name="Corrupt Operation")
    try:
        cur = project.db.cursor()
        cur.execute(
            "INSERT INTO ops (kind, label, spec, undo_info, status, barrier) "
            "VALUES (?, ?, ?, ?, 'applied', 0)",
            (
                "corrupt.fixture",
                "corrupt fixture",
                "{}",
                json.dumps({"created_rows": ["not-an-id"]}),
            ),
        )
        corrupt_op_id = int(cur.lastrowid)
        cur.execute(
            "UPDATE meta SET value=? WHERE key='op_cursor'",
            (str(corrupt_op_id),),
        )
        project.db.commit()
        before = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("ops", "receipts")
        }

        action = operation_action(
            "operation.undo", key="corrupt_undo@sha256:v1", expected_op_id=0
        )
        del action["params"]["expected_op_id"]
        result = executor_actions.run_action_spec(
            project, action, project_id="project-corrupt-operation"
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_state_corrupt"
        op = project.db.execute(
            "SELECT status FROM ops WHERE id=?", (corrupt_op_id,)
        ).fetchone()
        assert op["status"] == "applied"
        assert project.op_cursor == corrupt_op_id
        after = {
            table: int(
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("ops", "receipts")
        }
        assert after == before
    finally:
        project.close()


def test_typed_undo_replays_omitted_and_explicit_default_params(tmp_path: Path) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "default-replay.frisket")
    try:
        _seed_chain(project, tmp_path)
        omitted = {
            "action_id": "operation.undo",
            "scope": {"kind": "project"},
            "params": {},
            "idempotency_key": "operation-default-replay",
        }
        first = executor_actions.run_action_spec(
            project, omitted, project_id="project-operation-default-replay"
        )
        explicit = executor_actions.run_action_spec(
            project,
            {**omitted, "params": {"expected_op_id": None}},
            project_id="project-operation-default-replay",
        )

        assert first.status == explicit.status == "completed"
        assert explicit.receipt_id == first.receipt_id
        assert explicit.outputs == first.outputs
    finally:
        project.close()


@pytest.mark.parametrize("expected_op_id", [True, "1"])
def test_operation_expected_op_id_is_strict(
    tmp_path: Path, expected_op_id: object
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "strict-operation-id.frisket")
    try:
        result = executor_actions.run_action_spec(
            project,
            {
                "action_id": "operation.undo",
                "scope": {"kind": "project"},
                "params": {"expected_op_id": expected_op_id},
                "idempotency_key": "operation-strict-id",
            },
            project_id="project-operation-strict-id",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert project.db.execute("SELECT 1 FROM receipts").fetchone() is None
    finally:
        project.close()


def test_operation_mode_is_not_a_typed_param(tmp_path: Path) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "operation-mode.frisket")
    try:
        result = executor_actions.run_action_spec(
            project,
            {
                "action_id": "operation.undo",
                "scope": {"kind": "project"},
                "params": {"mode": "step"},
                "idempotency_key": "operation-mode",
            },
            project_id="project-operation-mode",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
    finally:
        project.close()


def test_undo_and_receipt_roll_back_without_manifest_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import actions as executor_actions
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "operation-rollback.frisket")
    try:
        seeded = _seed_chain(project, tmp_path)
        refresh_calls = 0

        def fail_receipt(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("receipt unavailable")

        def count_refresh(_project: Project) -> int:
            nonlocal refresh_calls
            refresh_calls += 1
            return 0

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt)
        monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)
        before_receipts = int(
            project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        )
        result = executor_actions.run_action_spec(
            project, _undo_action(seeded), project_id="project-operation-rollback"
        )

        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert project.op_cursor == seeded["derive_op_id"]
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE id=?", (seeded["derive_op_id"],)
            ).fetchone()["status"]
            == "applied"
        )
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (seeded["child_sheet_id"],)
            ).fetchone()["hidden"]
            == 0
        )
        assert (
            int(project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
            == before_receipts
        )
        assert refresh_calls == 0
    finally:
        project.close()


def test_completed_undo_survives_refresh_failure_and_replay_heals_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "operation-refresh.frisket")
    try:
        seeded = _seed_chain(project, tmp_path)
        project._write_manifest(pending_review_count=99)
        original_refresh = Project.refresh_pending_review_summary
        refresh_calls = 0

        def fail_once(project_: Project) -> int:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                raise OSError("manifest temporarily unavailable")
            return original_refresh(project_)

        monkeypatch.setattr(Project, "refresh_pending_review_summary", fail_once)
        action = _undo_action(seeded)
        first = executor_actions.run_action_spec(
            project, action, project_id="project-operation-refresh"
        )
        stale_manifest = json.loads((project.path / "manifest.json").read_text())
        replay = executor_actions.run_action_spec(
            project, action, project_id="project-operation-refresh"
        )
        healed_manifest = json.loads((project.path / "manifest.json").read_text())

        assert first.status == replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert stale_manifest["pending_review_count"] == 99
        assert healed_manifest["pending_review_count"] == 2
        assert refresh_calls == 2
    finally:
        project.close()

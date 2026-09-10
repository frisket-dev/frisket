from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate
from frisket.engine.store import Project
from helpers import run_cli
from action_test_helpers import write_json


def _import_rows_action(
    *,
    sheet_name: str = "Rows",
    idempotency_key: str | None = "import_rows@sha256:stable",
    output_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    action = {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "output_names": output_names or {},
        "params": {
            "columns": [
                {"name": "summary", "type": "text"},
                {"name": "source_url", "type": "url"},
                {
                    "name": "score",
                    "type": "integer",
                    "hidden": True,
                    "format": "integer",
                },
            ],
            "rows": [
                {
                    "summary": "City hall awarded a no-bid contract.",
                    "source_url": "https://example.com/story/1",
                    "score": 9,
                },
                {
                    "summary": "Routine road work finished early.",
                    "source_url": "https://example.com/story/2",
                    "score": 2,
                },
            ],
            "source": {
                "kind": "inline",
                "label": "seed fixture",
                "fingerprint": "sha256:stable",
            },
        },
        "idempotency_key": idempotency_key or "placeholder",
    }
    if idempotency_key is None:
        action.pop("idempotency_key")
    return action


def _append_rows_action(sheet_id: int, *, column_type: str = "text") -> dict[str, Any]:
    return {
        "action_id": "import.append_rows",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "columns": [{"name": "summary", "type": column_type}],
            "rows": [
                {"summary": 4 if column_type == "integer" else "Appended investigation"}
            ],
            "source": {"kind": "inline", "label": "paste"},
        },
        "idempotency_key": "import-append@sha256:stable",
    }


def test_import_append_rows_is_atomic_idempotent_and_refuses_incompatible_mapping(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "append.frisket", name="Append")
    created = run_action_spec(project, _import_rows_action(), project_id="project")
    sheet_id = int(created.outputs[0].sheet_id)
    # A root sheet may retain its creation op without being derived.
    project.db.execute(
        "UPDATE sheets SET parent_op_id=? WHERE id=?", (created.op_ids[0], sheet_id)
    )
    project.db.commit()
    appended = run_action_spec(
        project, _append_rows_action(sheet_id), project_id="project"
    )
    assert appended.status == "completed", appended.errors
    from frisket.actions.registry import ACTION_REGISTRY

    output_schema = ACTION_REGISTRY.get("import.append_rows").catalog_entry()[
        "output_schema"
    ]
    assert "row_count" in output_schema["required"]
    assert "appended" not in output_schema["properties"]
    assert appended.outputs[0].ref["row_count"] == 1
    destination = project.db.execute(
        "SELECT parent_sheet_id,parent_op_id FROM sheets WHERE id=?", (sheet_id,)
    ).fetchone()
    assert destination["parent_sheet_id"] is None
    assert destination["parent_op_id"] == created.op_ids[0]
    append_op = project.db.execute(
        "SELECT kind,spec FROM ops WHERE id=?", (appended.op_ids[0],)
    ).fetchone()
    assert append_op["kind"] == "import.append_rows"
    append_spec = json.loads(append_op["spec"])
    assert append_spec["action_id"] == "import.append_rows"
    assert append_spec["scope"] == {"kind": "sheet_rows", "sheet_id": sheet_id}
    assert "rows" not in append_spec["params"]
    assert append_spec["params"]["row_count"] == 1
    assert append_spec["params"]["params_hash"].startswith("sha256:")
    assert project.row_count(sheet_id) == 3
    replay = run_action_spec(
        project, _append_rows_action(sheet_id), project_id="project"
    )
    assert replay.receipt_id == appended.receipt_id
    assert replay.outputs[0].row_ids == appended.outputs[0].row_ids
    assert project.row_count(sheet_id) == 3

    incompatible = _append_rows_action(sheet_id, column_type="integer")
    incompatible["idempotency_key"] = "import-append@sha256:incompatible"
    refused = run_action_spec(project, incompatible, project_id="project")
    assert refused.status == "failed"
    assert refused.errors[0].code == "incompatible_append_mapping"
    assert project.row_count(sheet_id) == 3
    assert project.undo() == appended.op_ids[0]
    assert project.row_count(sheet_id) == 2
    undone_replay = run_action_spec(
        project, _append_rows_action(sheet_id), project_id="project"
    )
    assert undone_replay.status == "failed"
    assert undone_replay.errors[0].code == "stale_replay"
    assert project.row_count(sheet_id) == 2


def test_import_append_rows_enforces_per_request_limit_atomically(
    tmp_path: Path,
) -> None:
    from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "append-limit.frisket", name="Append limit")
    created = run_action_spec(project, _import_rows_action(), project_id="project")
    sheet_id = int(created.outputs[0].sheet_id)
    request = _append_rows_action(sheet_id)
    request["params"]["rows"] = [{"summary": "one"}, {"summary": "two"}]
    refused = run_action_spec(
        project,
        request,
        project_id="project",
        deps=ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=1)),
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "import_workload_limit_exceeded"
    assert project.row_count(sheet_id) == 2
    request["params"]["rows"] = [{"summary": "one"}]
    allowed = run_action_spec(
        project,
        request,
        project_id="project",
        deps=ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=1)),
    )
    assert allowed.status == "completed", allowed.errors
    assert project.row_count(sheet_id) == 3  # Not a per-sheet storage quota.


def test_import_append_rows_refuses_derived_destination_before_writing(tmp_path):
    from frisket.engine.executor.actions import run_action_spec

    with closing(
        Project.create(tmp_path / "derived.frisket", name="Derived")
    ) as project:
        created = run_action_spec(project, _import_rows_action(), project_id="project")
        sheet_id = created.outputs[0].sheet_id
        parent_id = project.add_sheet("Parent")
        project.db.execute(
            "UPDATE sheets SET parent_sheet_id=?,parent_op_id=? WHERE id=?",
            (parent_id, created.op_ids[0], sheet_id),
        )
        project.db.commit()
        before = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        refused = run_action_spec(
            project, _append_rows_action(sheet_id), project_id="project"
        )
        assert refused.status == "failed"
        assert refused.errors[0].code == "derived_append_unsupported"
        assert project.row_count(sheet_id) == 2
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == before


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project, tmp_path
    return {}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _import_rows_action()


def _missing_idempotency_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _import_rows_action(idempotency_key=None)


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # New key, same name: refuse before publication with no extra writes.
    del seeded
    return _import_rows_action(idempotency_key="import_rows@sha256:duplicate-sheet")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    del seeded
    assert result.run_id is None
    assert result.job_id is None
    assert result.receipt_id.startswith("receipt_")
    assert result.op_ids == [1]
    assert [output.kind for output in result.outputs] == [
        "sheet",
        "column",
        "column",
        "column",
        "rows",
    ]

    sheet = project.db.execute("SELECT * FROM sheets WHERE name='Rows'").fetchone()
    assert sheet is not None
    assert project.row_count(int(sheet["id"])) == 2
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet["id"],),
        ).fetchall()
    }
    assert set(columns) == {"summary", "source_url", "score"}
    assert columns["summary"]["type"] == "text"
    assert columns["source_url"]["type"] == "link"
    assert columns["score"]["type"] == "integer"
    assert columns["score"]["hidden"] == 1
    assert columns["score"]["format"] == "integer"
    values = project.get_values(int(sheet["id"]), int(columns["source_url"]["id"]))
    assert list(values.values()) == [
        "https://example.com/story/1",
        "https://example.com/story/2",
    ]

    op = project.db.execute("SELECT * FROM ops WHERE id=1").fetchone()
    assert op is not None
    assert op["kind"] == "import.rows"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "import.rows"
    assert op_spec["scope"] == {"kind": "project"}
    assert op_spec["sheet_name"] == "Rows"
    assert "rows" not in op_spec["params"]
    assert op_spec["import_row_count"] == 2
    assert op_spec["params_hash"].startswith("sha256:")
    undo = json.loads(op["undo_info"])
    assert undo == {"created_sheets": [sheet["id"]]}
    original_rows = [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY id", (sheet["id"],)
        )
    ]
    assert project.undo() == op["id"]
    assert (
        project.db.execute(
            "SELECT hidden FROM sheets WHERE id=?", (sheet["id"],)
        ).fetchone()[0]
        == 1
    )
    assert project.redo() == op["id"]
    assert (
        project.db.execute(
            "SELECT hidden FROM sheets WHERE id=?", (sheet["id"],)
        ).fetchone()[0]
        == 0
    )
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM rows WHERE sheet_id=? ORDER BY id", (sheet["id"],)
        )
    ] == original_rows
    assert {
        row["name"]: row["hidden"]
        for row in project.db.execute(
            "SELECT name, hidden FROM columns WHERE sheet_id=?", (sheet["id"],)
        )
    } == {"summary": 0, "source_url": 0, "score": 1}
    assert (
        project.get_values(int(sheet["id"]), int(columns["source_url"]["id"])) == values
    )

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["action_kind"] == "import.rows"
    assert receipt_row["idempotency_key"] == "import_rows@sha256:stable"
    assert receipt_row["status"] == "completed"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.schema_version == "frisket.receipt.v1"
    assert receipt.receipt_id == result.receipt_id
    assert receipt.action_kind == "import.rows"
    assert receipt.op_ids == [1]
    assert receipt.idempotency_key == "import_rows@sha256:stable"
    assert receipt.outputs[0].ref["kind"] == "materialized_sheet"
    assert receipt.outputs[0].ref["sheet_id"] == sheet["id"]
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "source_rows",
        "source_cell",
    }
    assert receipt.errors == []


CASES = [
    ExecutorCase(
        kind="import.rows",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_params",
                    "duplicate_column_name",
                    "row_shape_mismatch",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("columns", "rows", "source"),
            output_schema_properties=(),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "missing_idempotency_key",
                _missing_idempotency_action,
                "invalid_action_request",
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sheets": 1, "columns": 3, "rows": 2, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_import_rows_cli_error_output_stays_clean(tmp_path: Path) -> None:
    """CLI failure surface: exit code 1, parseable failed ActionResult, the
    conflict code echoed to stderr, and no raw sqlite UNIQUE noise leaking."""
    from frisket.contracts.action import ActionResult

    project_path = tmp_path / "executor.frisket"
    Project.create(project_path, name="V1 executor").close()

    spec_path = write_json(tmp_path, "import_rows.json", _import_rows_action())
    first = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-1",
        str(spec_path),
    )
    assert first.returncode == 0, first.stderr

    conflict_path = write_json(
        tmp_path,
        "conflict.json",
        _import_rows_action(sheet_name="Rows Copy"),
    )
    conflict = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-1",
        str(conflict_path),
    )
    assert conflict.returncode == 1
    conflict_result = ActionResult.model_validate(json.loads(conflict.stdout))
    assert conflict_result.status == "failed"
    assert conflict_result.errors[0].code == "idempotency_conflict"
    assert "idempotency_conflict" in conflict.stderr

    duplicate_path = write_json(
        tmp_path,
        "duplicate_sheet.json",
        _import_rows_action(idempotency_key="import_rows@sha256:duplicate-sheet"),
    )
    duplicate = run_cli(
        "action",
        "run",
        "--project",
        str(project_path),
        "--project-id",
        "project-1",
        str(duplicate_path),
    )
    assert duplicate.returncode == 1
    duplicate_result = ActionResult.model_validate(json.loads(duplicate.stdout))
    assert duplicate_result.errors[0].code == "duplicate_sheet_name"
    assert duplicate_result.errors[0].field == "sheet_name"
    assert "UNIQUE" not in duplicate.stdout
    assert "UNIQUE" not in duplicate.stderr


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**63, 1.0, True])
def test_import_rows_rejects_values_outside_core_int64_contract(value: Any) -> None:
    from frisket.actions.system import validate_root_action

    action = _import_rows_action()
    action["params"]["rows"][0]["score"] = value
    result = validate_root_action(action)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"


def test_import_rows_default_allows_more_than_10000_rows(tmp_path: Path) -> None:
    """The direct Solo action path has no deployment total-row ceiling."""
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "large-inline.frisket", name="Large inline")
    try:
        action = _import_rows_action(idempotency_key="import_rows@sha256:10001")
        action["params"]["rows"] = [
            {
                "summary": f"Record {index}",
                "source_url": f"https://example.com/{index}",
                "score": index,
            }
            for index in range(10_001)
        ]

        result = run_action_spec(project, action, project_id="large-inline")

        assert result.status == "completed", result.errors
        sheet = project.db.execute("SELECT id FROM sheets WHERE name='Rows'").fetchone()
        assert sheet is not None
        assert project.row_count(int(sheet["id"])) == 10_001
        receipt = project.db.execute(
            "SELECT action_kind, body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt is not None
        assert receipt["action_kind"] == "import.rows"
        # A 10k-row inline request may be materialized locally, but its durable
        # receipt must still retain a compact action summary rather than every
        # user value.
        body = str(receipt["body"])
        assert len(body.encode("utf-8")) < 100_000
        assert "Record 10000" not in body
        op = project.db.execute(
            "SELECT spec FROM ops WHERE id=?", (result.op_ids[0],)
        ).fetchone()
        assert op is not None
        assert json.loads(op["spec"])["import_row_count"] == 10_001
        assert "rows" not in json.loads(op["spec"])["params"]
        replayed = run_action_spec(project, action, project_id="large-inline")
        original_rows = next(
            output for output in result.outputs if output.kind == "rows"
        )
        replayed_rows = next(
            output for output in replayed.outputs if output.kind == "rows"
        )
        assert replayed.status == "completed"
        assert replayed.receipt_id == result.receipt_id
        assert replayed_rows.row_ids == original_rows.row_ids
    finally:
        project.close()


def test_import_rows_hosted_limit_is_injected_and_never_partially_publishes(
    tmp_path: Path,
) -> None:
    """Finite hosted policy is opt-in and rejects before a project mutation."""
    from frisket.engine.executor.action_inventory import (
        ExecutorDeps,
        ImportWorkloadLimits,
    )
    from frisket.engine.executor.actions import run_action_spec

    project = Project.create(tmp_path / "hosted-limit.frisket", name="Hosted limit")
    try:
        exact = _import_rows_action(idempotency_key="import_rows@sha256:hosted-exact")
        exact["params"]["rows"] = exact["params"]["rows"][:2]
        deps = ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=2))
        completed = run_action_spec(project, exact, project_id="hosted", deps=deps)
        assert completed.status == "completed", completed.errors

        over = _import_rows_action(idempotency_key="import_rows@sha256:hosted-over")
        over["sheet_name"] = "Must not publish"
        over["params"]["rows"] = [
            *over["params"]["rows"],
            {
                "summary": "third",
                "source_url": "https://example.com/third",
                "score": 3,
            },
        ]
        failed = run_action_spec(project, over, project_id="hosted", deps=deps)

        assert failed.status == "failed"
        assert failed.errors[0].code == "import_workload_limit_exceeded"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[0] == 3
        assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 2
        assert project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 6
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    finally:
        project.close()

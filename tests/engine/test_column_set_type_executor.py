from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from action_test_helpers import run_typed_map_request
from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    UndoRerun,
    operation_action,
)
from frisket.authoring import column_types
from frisket.engine.store import Project
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


def _set_type_action(
    *,
    column_id: int,
    type_: str,
    sheet_id: int | None = None,
    key: str = "column_set_type@sha256:v1",
) -> dict[str, Any]:
    params: dict[str, Any] = {"column_id": column_id, "type": type_}
    if sheet_id is not None:
        params["sheet_id"] = sheet_id
    return {
        "action_id": "column.set_type",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Events")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "published": project.add_column(sheet_id, "published", type="text"),
        "bad_date": project.add_column(sheet_id, "bad_date", type="text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Launch",
                "published": "2026-06-14",
                "bad_date": "not a date",
            },
            {
                "title": "Follow-up",
                "published": "2026-06-15T09:30:00Z",
                "bad_date": "still not a date",
            },
        ],
        columns,
    )
    return {
        "sheet_id": sheet_id,
        "columns": columns,
        "row_ids": row_ids,
        "seeded_op_id": project.op_cursor,
    }


def _column_type(project: Project, column_id: int) -> str:
    row = project.db.execute(
        "SELECT type FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    assert row is not None
    return str(row["type"])


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _set_type_action(
        column_id=seeded["columns"]["published"],
        sheet_id=seeded["sheet_id"],
        type_="date",
    )


def _unknown_type_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _set_type_action(
        column_id=seeded["columns"]["published"],
        type_="not_registered",
        key="column_set_type_unknown@sha256:v1",
    )


def _missing_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _set_type_action(
        column_id=999_999,
        type_="date",
        key="column_set_type_missing_column@sha256:v1",
    )


def _wrong_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _set_type_action(
        column_id=seeded["columns"]["published"],
        sheet_id=seeded["sheet_id"] + 1,
        type_="date",
        key="column_set_type_wrong_sheet@sha256:v1",
    )


def _hidden_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _set_type_action(
        column_id=seeded["columns"]["published"],
        type_="date",
        key="column_set_type_hidden_column@sha256:v1",
    )


def _hide_published_column(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE columns SET hidden=1 WHERE id=?",
        (seeded["columns"]["published"],),
    )
    project.db.commit()


def _hidden_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _set_type_action(
        column_id=seeded["columns"]["published"],
        type_="date",
        key="column_set_type_hidden_sheet@sha256:v1",
    )


def _hide_sheet(project: Project, seeded: dict[str, Any]) -> None:
    project.db.execute(
        "UPDATE sheets SET hidden=1 WHERE id=?",
        (seeded["sheet_id"],),
    )
    project.db.commit()


def _conflicting_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary action, different target column.
    return _set_type_action(column_id=seeded["columns"]["title"], type_="category")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    published = seeded["columns"]["published"]
    assert len(result.op_ids) == 1
    output = next(item for item in result.outputs if item.kind == "column")
    assert output.column_id == published
    assert output.sheet_id == seeded["sheet_id"]
    assert output.ref["kind"] == "column_type_update"
    assert output.ref["type_before"] == "text"
    assert output.ref["type_after"] == "date"
    assert _column_type(project, published) == "date"

    op = project.db.execute(
        "SELECT kind, spec, undo_info FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    assert op["kind"] == "column.set_type"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "column.set_type"
    assert op_spec["params"]["type"] == "date"
    assert op_spec["params"]["params_hash"].startswith("sha256:")
    undo_info = json.loads(op["undo_info"])
    assert undo_info["column_types"][str(published)] == "text"
    assert undo_info["column_types_after"][str(published)] == "date"

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "column.set_type"
    assert receipt.op_ids == result.op_ids
    assert receipt.inputs[0].name == "target_column"
    assert receipt.inputs[0].ref == {
        "kind": "target_column",
        "sheet_id": seeded["sheet_id"],
        "sheet_name": "Events",
        "column_id": published,
        "column_name": "published",
        "type_before": "text",
    }
    assert receipt.outputs[0].name == "published"
    assert receipt.outputs[0].ref["kind"] == "column_type_update"
    evidence = {item.ref["kind"]: item.ref for item in receipt.evidence}
    assert {"column_type_transition", "classified_column_values"} <= set(evidence)
    assert evidence["classified_column_values"] == {
        "kind": "classified_column_values",
        "sheet_id": seeded["sheet_id"],
        "column_id": published,
        "type": "date",
        "row_ids": seeded["row_ids"],
        "row_count": 2,
        "invalid_row_ids": [],
        "invalid_count": 0,
    }


def _check_undone(project: Project, seeded: dict[str, Any], first: Any) -> None:
    del first
    assert _column_type(project, seeded["columns"]["published"]) == "text"


def _redo_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return operation_action(
        "operation.redo",
        key="column_set_type_redo@sha256:v1",
        expected_op_id=seeded["seeded_op_id"] + 1,
    )


def _check_redo(
    project: Project, seeded: dict[str, Any], first: Any, second: Any
) -> None:
    del first, second
    assert _column_type(project, seeded["columns"]["published"]) == "date"


CASES = [
    ExecutorCase(
        kind="column.set_type",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_column_ref",
                    "invalid_column_type",
                    "invalid_temporal_value",
                    "timeline_not_found",
                    "timeline_stale",
                    "timeline_duration_required",
                    "ambiguous_time_mapping",
                    "range_out_of_bounds",
                    "output_column_busy",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            side_effects=frozenset(
                {
                    "read_column_values",
                    "update_column_type",
                    "write_column_type_op",
                    "write_receipt",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=("column_id", "sheet_id", "type"),
            output_schema_properties=(
                "sheet_id",
                "column_id",
                "type_before",
                "type_after",
            ),
            description_contains="visible column",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_column_type",
                _unknown_type_action,
                "invalid_column_type",
            ),
            Gate(
                "invalid_column_ref",
                _missing_column_action,
                "invalid_column_ref",
            ),
            Gate("wrong_sheet", _wrong_sheet_action, "invalid_column_ref"),
            Gate(
                "hidden_column",
                _hidden_column_action,
                "invalid_column_ref",
                prepare=_hide_published_column,
            ),
            Gate(
                "hidden_sheet",
                _hidden_sheet_action,
                "invalid_column_ref",
                prepare=_hide_sheet,
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_column_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"columns": 0, "ops": 1, "receipts": 1},
        check_state=_check_state,
        undo=UndoRerun(
            check_undone=_check_undone,
            rerun_action=_redo_action,
            check_rerun=_check_redo,
        ),
        request_style="typed",
    )
]


def test_url_alias_normalizes_to_link_and_replays_as_same_request(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "alias.frisket", name="Alias")
    try:
        sheet_id = project.add_sheet("Links")
        column_id = project.add_column(sheet_id, "url", type="text")
        first = run_typed_map_request(
            project,
            _set_type_action(
                column_id=column_id,
                type_="url",
                key="column_set_type_url_alias@sha256:v1",
            ),
            project_id="alias",
        )
        replay = run_typed_map_request(
            project,
            _set_type_action(
                column_id=column_id,
                type_="link",
                key="column_set_type_url_alias@sha256:v1",
            ),
            project_id="alias",
        )

        assert first.status == replay.status == "completed"
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        assert _column_type(project, column_id) == "link"
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 1
    finally:
        project.close()


def test_set_type_requires_plugin_enablement_then_accepts_valid_values(
    tmp_path: Path,
) -> None:
    plugin_type = "strict_code"
    column_types.register_column_type(
        plugin_type,
        validate=lambda value: isinstance(value, str) and value.startswith("CODE-"),
        presentation={"renderer": "text"},
        description="Test code",
        plugin="demo.strict_code",
    )
    project = Project.create(tmp_path / "plugin-type.frisket", name="Plugin type")
    try:
        sheet_id = project.add_sheet("Codes")
        column_id = project.add_column(sheet_id, "code", type="text")
        project.add_rows(sheet_id, [{"code": "CODE-1"}], {"code": column_id})

        disabled = run_typed_map_request(
            project,
            _set_type_action(
                column_id=column_id,
                type_=plugin_type,
                key="column_set_type_plugin_disabled@sha256:v1",
            ),
            project_id="plugin-type",
        )
        assert disabled.status == "failed"
        assert disabled.errors[0].code == "invalid_column_type"
        assert _column_type(project, column_id) == "text"

        activate_runtime_plugin_for_project(
            project,
            tmp_path / "plugin-package",
            plugin_id="demo.strict_code",
            runtime_bindings={},
            project_id="plugin-type",
            column_types=[plugin_type],
        )
        enabled = run_typed_map_request(
            project,
            _set_type_action(
                column_id=column_id,
                type_=plugin_type,
                key="column_set_type_plugin_enabled@sha256:v1",
            ),
            project_id="plugin-type",
        )
        assert enabled.status == "completed", enabled.errors
        assert _column_type(project, column_id) == plugin_type
    finally:
        project.close()
        column_types.unregister_column_type(plugin_type)


def test_set_type_validates_plugin_stars_values(tmp_path: Path) -> None:
    column_types.register_column_type(
        "stars",
        validate=lambda value: (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 5
        ),
        presentation={"renderer": "stars", "base": "number", "plugin": "demo.stars"},
        description="Numeric rating from 0 to 5 displayed as stars.",
        plugin="demo.stars",
    )
    try:
        project_path = tmp_path / "stars-column-type.frisket"
        project = Project.create(project_path, name="Stars Column Type")
        sheet_id = project.add_sheet("Ratings")
        rating_id = project.add_column(sheet_id, "rating", type="number")
        row_ids = project.add_rows(
            sheet_id,
            [{"rating": 4.5}, {"rating": 6.0}],
            {"rating": rating_id},
        )
        activate_runtime_plugin_for_project(
            project,
            tmp_path / "plugin-package",
            plugin_id="demo.stars",
            runtime_bindings={},
            project_id="project-stars-column-type",
            column_types=["stars"],
        )

        from frisket.engine.executor import run_action_spec

        result = run_action_spec(
            project,
            _set_type_action(
                column_id=rating_id,
                sheet_id=sheet_id,
                type_="stars",
                key="stars_set_type@sha256:v1",
            ),
            project_id="project-stars-column-type",
        )
        assert result.status == "completed"
        assert _column_type(project, rating_id) == "stars"
        assert project.get_values(sheet_id, rating_id) == {
            row_ids[0]: 4.5,
            row_ids[1]: None,
        }
        assert project.get_values(sheet_id, rating_id, preserve_invalid=True) == {
            row_ids[0]: 4.5,
            row_ids[1]: 6.0,
        }
        project.close()
    finally:
        column_types.unregister_column_type("stars")


def test_set_type_validates_plugin_case_id_values(tmp_path: Path) -> None:
    column_types.register_column_type(
        "case_id",
        validate=lambda value: (
            isinstance(value, str)
            and len(value) == len("CASE-0000")
            and value.startswith("CASE-")
            and value.removeprefix("CASE-").isdigit()
        ),
        parse=lambda value: f"CASE-{int(str(value).split('-', 1)[1]):04d}",
        presentation={
            "base": "text",
            "owner": "plugin",
            "plugin": "demo.case_files",
        },
        description="Canonical case identifier stored as CASE-0000.",
        plugin="demo.case_files",
    )
    try:
        project_path = tmp_path / "case-id-column-type.frisket"
        project = Project.create(project_path, name="Case ID Column Type")
        sheet_id = project.add_sheet("Cases")
        case_id = project.add_column(sheet_id, "case_id", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"case_id": "CASE-0001"}, {"case_id": "case-0002"}],
            {"case_id": case_id},
        )
        activate_runtime_plugin_for_project(
            project,
            tmp_path / "plugin-package",
            plugin_id="demo.case_files",
            runtime_bindings={},
            project_id="project-case-id-column-type",
            column_types=["case_id"],
        )

        from frisket.engine.executor import run_action_spec

        result = run_action_spec(
            project,
            _set_type_action(
                column_id=case_id,
                sheet_id=sheet_id,
                type_="case_id",
                key="case_id_set_type@sha256:v1",
            ),
            project_id="project-case-id-column-type",
        )
        assert result.status == "completed"
        assert _column_type(project, case_id) == "case_id"
        assert project.get_values(sheet_id, case_id) == {
            row_ids[0]: "CASE-0001",
            row_ids[1]: None,
        }
        assert project.get_values(sheet_id, case_id, preserve_invalid=True) == {
            row_ids[0]: "CASE-0001",
            row_ids[1]: "case-0002",
        }
        project.close()
    finally:
        column_types.unregister_column_type("case_id")


def test_set_type_and_receipt_roll_back_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "receipt-rollback.frisket", name="Rollback")
    try:
        seeded = _seed(project, tmp_path)
        seeded_op_count = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]

        def fail_receipt(*args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("receipt write failed")

        monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt)
        result = run_typed_map_request(
            project,
            _make_action(seeded),
            project_id="column-set-type-receipt-rollback",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert _column_type(project, seeded["columns"]["published"]) == "text"
        assert (
            project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
            == seeded_op_count
        )
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        project.close()

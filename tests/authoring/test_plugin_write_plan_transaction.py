"""Transaction-scheme proof for ``apply_write_plan`` atomicity.

Validation-time rejection proves that an invalid plan writes nothing. This
suite covers the harder case: a failure that fires MID-APPLY, after real rows
are already inserted, rolls the whole plan back to ZERO partial state — for
both transaction scaffolds:

* idle connection -> ``BEGIN IMMEDIATE`` / rollback;
* caller already inside a transaction -> ``SAVEPOINT`` / ``ROLLBACK TO SAVEPOINT``,
  leaving the caller's outer transaction intact and committable.
"""

from __future__ import annotations

from unittest import mock

import pytest

from frisket.contracts.plugin_write_plan import (
    PROJECT_WRITES_CAPABILITY,
    WRITE_PLAN_SCHEMA_VERSION,
)
from frisket.engine.executor import plugin_write_apply
from frisket.engine.executor.plugin_write_apply import (
    WritePlanApplyError,
    apply_write_plan,
)
from frisket.engine.store.project import Project

_BOOM = "BOOM-mid-apply"


def _project(tmp_path) -> Project:
    return Project.create(tmp_path / "t.frisket", name="t")


def _plan_with_boom_cell() -> dict:
    # Valid plan (schema + refs); the cell value _BOOM is our injection trigger.
    return {
        "schema_version": WRITE_PLAN_SCHEMA_VERSION,
        "ops": [
            {"op": "create_sheet", "sheet_ref": "s1", "name": "People"},
            {"op": "create_column", "sheet_ref": "s1", "column_ref": "c1", "name": "n"},
            {"op": "append_rows", "sheet_ref": "s1", "rows": [{"c1": _BOOM}]},
        ],
    }


def _dumps_that_explodes_on_boom():
    """Wrap the module's json.dumps so it raises only when serializing the _BOOM
    cell value — i.e. AFTER the sheet, column, and row rows are already inserted."""
    real = plugin_write_apply.json.dumps

    def fake(obj, *args, **kwargs):
        if obj == _BOOM:
            raise RuntimeError("injected mid-apply failure")
        return real(obj, *args, **kwargs)

    return fake


def test_mid_apply_failure_rolls_back_begin_immediate(tmp_path) -> None:
    project = _project(tmp_path)
    cursor_before = project.get_meta("op_cursor")
    ops_before = project.db.execute("SELECT COUNT(*) AS n FROM ops").fetchone()["n"]

    with mock.patch.object(
        plugin_write_apply.json, "dumps", _dumps_that_explodes_on_boom()
    ):
        with pytest.raises(WritePlanApplyError):
            apply_write_plan(
                project,
                _plan_with_boom_cell(),
                plugin_id="frisket.ftm",
                manifest_sha="sha",
                declared_capabilities=(PROJECT_WRITES_CAPABILITY,),
            )

    # Zero partial state: no sheet, no rows, no op row, op_cursor unmoved.
    assert project.sheets() == []
    assert project.db.execute("SELECT COUNT(*) AS n FROM rows").fetchone()["n"] == 0
    assert (
        project.db.execute("SELECT COUNT(*) AS n FROM ops").fetchone()["n"]
        == ops_before
    )
    assert project.get_meta("op_cursor") == cursor_before
    assert project.db.in_transaction is False


def test_mid_apply_failure_rolls_back_savepoint_and_keeps_outer_txn(tmp_path) -> None:
    project = _project(tmp_path)
    db = project.db

    # Caller opens its OWN transaction and does prior work the plan must not disturb.
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO sheets (name, position) VALUES ('Prior', 1)")
    assert db.in_transaction is True

    with mock.patch.object(
        plugin_write_apply.json, "dumps", _dumps_that_explodes_on_boom()
    ):
        with pytest.raises(WritePlanApplyError):
            apply_write_plan(
                project,
                _plan_with_boom_cell(),
                plugin_id="frisket.ftm",
                manifest_sha="sha",
                declared_capabilities=(PROJECT_WRITES_CAPABILITY,),
            )

    # The savepoint rolled back the plan but left the outer transaction alive.
    assert db.in_transaction is True
    # The plan's sheet is gone; the caller's prior 'Prior' sheet survives.
    names = [r["name"] for r in db.execute("SELECT name FROM sheets ORDER BY id")]
    assert names == ["Prior"]
    assert db.execute("SELECT COUNT(*) AS n FROM rows").fetchone()["n"] == 0
    # The outer transaction is still committable.
    db.commit()
    assert [r["name"] for r in project.sheets()] == ["Prior"]


def test_success_after_injection_removed_applies_cleanly(tmp_path) -> None:
    """Control: the same shape without the injection applies and is one-step undoable."""
    project = _project(tmp_path)
    receipt = apply_write_plan(
        project,
        {
            "schema_version": WRITE_PLAN_SCHEMA_VERSION,
            "ops": [
                {"op": "create_sheet", "sheet_ref": "s1", "name": "People"},
                {
                    "op": "create_column",
                    "sheet_ref": "s1",
                    "column_ref": "c1",
                    "name": "n",
                },
                {"op": "append_rows", "sheet_ref": "s1", "rows": [{"c1": "Ada"}]},
            ],
        },
        plugin_id="frisket.ftm",
        manifest_sha="sha",
        declared_capabilities=(PROJECT_WRITES_CAPABILITY,),
    )
    assert receipt.counts == {"sheets": 1, "columns": 1, "rows": 1}
    sheet_id = project.sheets()[0]["id"]
    assert project.row_count(sheet_id) == 1

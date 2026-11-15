"""Restoring retained copies requires their destinations, never their sources."""

import pytest

import test_execution_lifecycle_recovery as recovery
from frisket.engine.store.op_log import RestorationUnavailable, step_operation

work = recovery.work


def _step(project, direction, op_id):
    with project.transaction_scope():
        return step_operation(project, direction, op_id=op_id)


def _copy_into_existing(work):
    project, sheet, _, rows, _, _, run = work
    original = recovery.start(run)
    assert original.status == "completed", original.errors
    initial = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Copy"},
        key="initial-copy",
    )
    assert initial.status == "completed", initial.errors
    target = next(
        column["id"] for column in project.columns(sheet) if column["name"] == "Copy"
    )
    copied = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Copy"},
        replace_existing=True,
        expected_output_columns={"Copy": target},
    )
    assert copied.status == "completed", copied.errors
    op = project.op_cursor
    assert project.db.execute(
        "SELECT count(*) FROM copied_result_outcomes WHERE op_id=?", (op,)
    ).fetchone()[0] == len(rows)
    return original, target, op


@pytest.mark.parametrize("destination", ["row", "column", "sheet"])
def test_restore_copied_change_requires_original_destination(work, destination):
    project, sheet, _, rows, _, _, run = work
    _, target, op = _copy_into_existing(work)
    saved = dict(project.get_values(sheet, target))
    _step(project, "revert", op)
    if destination == "row":
        deleted = run(
            "row.delete",
            {"sheet_id": sheet, "row_ids": [rows[0]]},
            scope={"kind": "project"},
        )
        assert deleted.status == "completed", deleted.errors
    elif destination == "column":
        creation = project.db.execute(
            "SELECT MIN(op_id) FROM copied_result_outcomes WHERE column_id=?", (target,)
        ).fetchone()[0]
        _step(project, "revert", creation)
    else:
        project.delete_sheet(sheet)
    deletion = project.op_cursor
    with pytest.raises(RestorationUnavailable, match="destination.*unavailable"):
        _step(project, "restore", op)
    assert (
        project.db.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()[0]
        == "undone"
    )
    assert project.op_cursor == deletion
    if destination == "column":
        _step(project, "restore", creation)
    else:
        _step(project, "revert", deletion)
    _step(project, "restore", op)
    assert dict(project.get_values(sheet, target)) == saved


def test_restore_copied_change_keeps_later_edit_precedence(work):
    project, sheet, _, rows, _, _, _ = work
    _, target, op = _copy_into_existing(work)
    saved = dict(project.get_values(sheet, target))
    _step(project, "revert", op)
    project.apply_edits([{"row_id": rows[0], "column_id": target, "value": "later"}])
    _step(project, "restore", op)
    assert dict(project.get_values(sheet, target)) == {**saved, rows[0]: "later"}


def test_restore_copied_change_does_not_require_source_column(work):
    project, sheet, _, _, _, _, _ = work
    original, target, op = _copy_into_existing(work)
    saved = dict(project.get_values(sheet, target))
    _step(project, "revert", op)
    source_op = project.db.execute(
        "SELECT op_id FROM runs WHERE id=?", (original.run_id,)
    ).fetchone()[0]
    _step(project, "revert", source_op)
    assert "Answer" not in {column["name"] for column in project.columns(sheet)}
    _step(project, "restore", op)
    assert dict(project.get_values(sheet, target)) == saved

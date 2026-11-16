from contextlib import closing

import pytest


from frisket.engine.store import Project
from frisket.engine.store.changes import start_change
from frisket.engine.store.column_descriptors import (
    descriptor_identity,
    project_column_descriptors,
)
from frisket.engine.store.op_log import append_op, operation_target, step_operation


def _step(project, direction, op_id=None):
    project.db.execute("BEGIN IMMEDIATE")
    try:
        transition = step_operation(project, direction, op_id=op_id)
        project.db.commit()
        return transition
    except BaseException:
        project.db.rollback()
        raise


def _descriptor(project, column, value):
    project.db.execute("BEGIN IMMEDIATE")
    old = project.get_column(column)["type"]
    op = append_op(
        project,
        "column.set_type",
        undo_info={
            "column_types": {str(column): old},
            "column_types_after": {str(column): value},
        },
        commit=False,
    )
    start_change(project.db, op)
    project_column_descriptors(project.db, [column])
    project.db.commit()
    return op


def test_descriptor_identity_tracks_effective_contribution_and_selective_revert(
    tmp_path,
):
    with closing(Project.create(tmp_path / "descriptors.frisket")) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "value")
        first = _descriptor(project, column, "number")
        first_identity = descriptor_identity(project, column)
        second = _descriptor(project, column, "number")
        assert descriptor_identity(project, column) != first_identity
        cursor = project.op_cursor
        _step(project, "revert", first)
        assert project.op_cursor == cursor
        assert descriptor_identity(project, column)["owners"]["type"] == second
        _step(project, "revert", second)
        assert project.get_column(column)["type"] == "text"
        _step(project, "restore", first)
        assert descriptor_identity(project, column) == first_identity
        assert project.op_cursor == cursor


def test_navigation_uses_first_mutation_order_and_retains_selective_restore(tmp_path):
    with closing(Project.create(tmp_path / "order.frisket")) as project:
        from frisket.engine.store.cell_writes import EditCellWrite, insert_edits

        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "value")
        row = project.add_rows(sheet, [{"value": "original"}], {"value": column})[0]
        early_admission = project.append_op("prepared")
        later_admission = project.append_op("prepared")
        for op in (later_admission, early_admission):
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                insert_edits(
                    project.db, op_id=op, edits=[EditCellWrite(row, column, str(op))]
                )
                start_change(project.db, op)
        assert operation_target(project, "undo")["id"] == early_admission
        _step(project, "undo")
        assert operation_target(project, "undo")["id"] == later_admission
        assert operation_target(project, "redo")["id"] == early_admission
        queued = project.append_op("prepared")
        assert operation_target(project, "redo")["id"] == early_admission
        with project.db:
            project.db.execute("UPDATE ops SET label=label WHERE id=?", (queued,))
            start_change(project.db, queued)
        assert operation_target(project, "redo") is None
        assert operation_target(project, "restore", op_id=early_admission) is not None
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE id=?", (early_admission,)
            ).fetchone()[0]
            == "undone"
        )


def test_pre_cutover_history_is_visible_but_not_executable(tmp_path):
    with closing(Project.create(tmp_path / "historical.frisket")) as project:
        historical = project.append_op("old")
        with project.db:
            project.db.execute(
                "UPDATE meta SET value=? WHERE key='op_cursor'", (str(historical),)
            )
        assert len(project.history()) == 1
        assert operation_target(project, "undo") is None
        assert operation_target(project, "revert", op_id=historical) is None


def test_selective_row_membership_uses_latest_active_change(tmp_path):
    from frisket.engine.store.structural_changes import project_structural_changes

    with closing(Project.create(tmp_path / "rows.frisket")) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "value")
        row = project.add_rows(sheet, [{"value": "x"}], {"value": column})[0]
        ids = []
        for field in ("deleted_rows", "restored_rows", "deleted_rows"):
            with project.db:
                op = append_op(project, field, undo_info={field: [row]}, commit=False)
                start_change(project.db, op)
                project_structural_changes(project.db, op)
            ids.append(op)
        _step(project, "revert", ids[0])
        assert project.row_count(sheet) == 0, "later deletion still owns membership"
        _step(project, "revert", ids[2])
        assert project.row_count(sheet) == 1, "intervening restoration wins"


def test_sheet_delete_retains_derived_data_and_atomic_name_conflict(tmp_path):
    from frisket.engine.store.op_log import RestorationUnavailable

    with closing(Project.create(tmp_path / "sheets.frisket")) as project:
        source = project.add_sheet("source")
        derived = project.add_sheet("derived", parent_sheet_id=source)
        project.delete_sheet(source)
        deleted = project.op_cursor
        assert [s["id"] for s in project.sheets()] == [derived]
        replacement = project.add_sheet("source")
        with pytest.raises(RestorationUnavailable, match="already exists"):
            _step(project, "undo")
        assert project.op_cursor == deleted
        assert [s["id"] for s in project.sheets()] == [derived, replacement]
        with project.db:
            project.db.execute(
                "UPDATE sheets SET name='replacement' WHERE id=?", (replacement,)
            )
        _step(project, "undo")
        assert source in [s["id"] for s in project.sheets()]


def test_typed_selective_actions_keep_navigation_and_return_actual_state(tmp_path):
    from frisket.engine.executor.actions import run_action_spec

    with closing(Project.create(tmp_path / "typed.frisket")) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "value")
        row = project.add_rows(sheet, [{"value": "original"}], {"value": column})[0]
        edit = project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "first"}]
        )
        project.apply_edits([{"row_id": row, "column_id": column, "value": "last"}])
        cursor = project.op_cursor
        for kind in ("revert", "restore"):
            result = run_action_spec(
                project,
                {
                    "action_id": f"operation.{kind}",
                    "scope": {"kind": "project"},
                    "params": {"op_id": edit},
                    "idempotency_key": f"operation-{kind}",
                },
                project_id="test",
            )
            assert result.status == "completed", result.errors
            assert project.op_cursor == cursor
            assert project.get_values(sheet, column)[row] == "last"


def test_creation_restore_conflict_does_not_partially_restore_other_columns(tmp_path):
    from frisket.engine.store.op_log import RestorationUnavailable

    with closing(Project.create(tmp_path / "conflict.frisket")) as project:
        sheet = project.add_sheet("data")
        a = project.add_column(sheet, "summary")
        b = project.add_column(sheet, "citation")
        with project.db:
            op = append_op(
                project, "create", undo_info={"created_columns": [a, b]}, commit=False
            )
            start_change(project.db, op)
        _step(project, "undo")
        project.add_column(sheet, "summary")
        with pytest.raises(RestorationUnavailable, match="already exists"):
            _step(project, "restore", op)
        assert project.get_column(a)["active"] == 0
        assert project.get_column(b)["active"] == 0


def test_direct_descriptor_setters_share_change_identity_and_undo(tmp_path):
    with closing(Project.create(tmp_path / "setter.frisket")) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "value")
        original = descriptor_identity(project, column)
        project.set_column_type(column, "number")
        changed = descriptor_identity(project, column)
        assert changed["owners"]["type"] == project.op_cursor
        project.undo()
        assert descriptor_identity(project, column) == original
        project.redo()
        assert descriptor_identity(project, column) == changed
        with pytest.raises(ValueError, match="requires a Change owner"):
            project.set_column_type(column, "text", initialize=True)


def test_nested_typed_edit_and_receipt_roll_back_with_outer_transaction(tmp_path):
    from frisket.engine.executor.actions import run_action_spec

    with closing(Project.create(tmp_path / "nested.frisket")) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "value")
        row = project.add_rows(sheet, [{"value": "original"}], {"value": column})[0]
        before = len(project.history())
        project.db.execute("BEGIN IMMEDIATE")
        result = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [{"row_id": row, "column_id": column, "value": "changed"}]
                },
                "idempotency_key": "nested-edit",
            },
            project_id="test",
        )
        assert result.status == "completed", result.errors
        assert project.db.in_transaction
        project.db.rollback()
        assert project.get_values(sheet, column)[row] == "original"
        assert len(project.history()) == before
        assert not project.db.execute(
            "SELECT 1 FROM receipts WHERE idempotency_key=?", ("nested-edit",)
        ).fetchone()

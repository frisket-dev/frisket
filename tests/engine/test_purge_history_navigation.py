"""Permanent removal disables only the history that needs removed material."""

from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.op_log import operation_target, step_operation
from frisket.engine.store.purge import (
    PurgedMaterialUnavailable,
    begin_purge,
    finish_purge,
)


def test_purged_creation_does_not_block_unrelated_undo_redo(tmp_path):
    path = tmp_path / "navigation.frisket"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("other")
        column = project.add_column(sheet, "text")
        row = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
        edit = project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "changed"}]
        )
        docs = project.add_sheet("documents")
        content = project.add_column(docs, "content")
        doomed = project.add_rows(docs, [{"content": "secret"}], {"content": content})[
            0
        ]
        creation = project.op_cursor
    with closing(Project(path, maintenance=True)) as project:
        finish_purge(project, begin_purge(project, row_ids=[doomed]))
    with closing(Project(path)) as project:
        undone = []
        while (target := operation_target(project, "undo")) is not None:
            undone.append(project.undo())
            if target["id"] == edit:
                break
        assert edit in undone
        assert creation not in undone
        assert project.get_values(sheet, column)[row] == "original"
        assert project.redo() == edit
        assert project.get_values(sheet, column)[row] == "changed"
        # The skip is navigation only: explicit transitions remain fenced.
        with pytest.raises(PurgedMaterialUnavailable, match="permanently purged"):
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                step_operation(project, "revert", op_id=creation)
        assert (
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (doomed,)
            ).fetchone()[0]
            == 1
        )


def test_purged_undone_creation_does_not_block_later_redo(tmp_path):
    path = tmp_path / "redo.frisket"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("data")
        column = project.add_column(sheet, "text")
        first = project.add_rows(sheet, [{"text": "secret"}], {"text": column})[0]
        first_change = project.op_cursor
        second = project.add_rows(sheet, [{"text": "retained"}], {"text": column})[0]
        second_change = project.op_cursor
        assert project.undo() == second_change
        assert project.undo() == first_change
    with closing(Project(path, maintenance=True)) as project:
        finish_purge(project, begin_purge(project, row_ids=[first]))
    with closing(Project(path)) as project:
        assert operation_target(project, "redo")["id"] == second_change
        assert project.redo() == second_change
        assert project.get_values(sheet, column) == {second: "retained"}
        with pytest.raises(PurgedMaterialUnavailable, match="permanently purged"):
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                step_operation(project, "restore", op_id=first_change)


@pytest.mark.parametrize("purge_after_undo", [False, True])
def test_partly_purged_append_preserves_navigation_and_never_restores_purged_row(
    tmp_path, purge_after_undo
):
    path = tmp_path / "partial-append.frisket"
    with closing(Project.create(path)) as project:
        other = project.add_sheet("Other")
        text = project.add_column(other, "text")
        row = project.add_rows(other, [{"text": "original"}], {"text": text})[0]
        edit = project.apply_edits(
            [{"row_id": row, "column_id": text, "value": "edited"}]
        )
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "content")
        doomed, kept = project.add_rows(
            sheet, [{"content": "secret"}, {"content": "retained"}], {"content": column}
        )
        append = project.op_cursor
        if purge_after_undo:
            assert project.undo() == append
            assert project.undo() == edit
    with closing(Project(path, maintenance=True)) as project:
        finish_purge(project, begin_purge(project, row_ids=[doomed]))
    with closing(Project(path)) as project:
        if not purge_after_undo:
            assert project.undo() == append
            assert project.visible_row_ids(sheet) == []
            assert project.undo() == edit
        assert project.get_values(other, text)[row] == "original"
        assert project.redo() == edit
        assert project.redo() == append
        assert project.get_values(other, text)[row] == "edited"
        assert project.get_values(sheet, column) == {kept: "retained"}
        assert (
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (doomed,)
            ).fetchone()[0]
            == 1
        )
        # Selective history toggles the same retained effects without changing
        # normal navigation or granting publication on the closed owner.
        with project.transaction_scope():
            step_operation(project, "revert", op_id=append)
        assert project.visible_row_ids(sheet) == []
        with project.transaction_scope():
            step_operation(project, "restore", op_id=append)
        assert project.visible_row_ids(sheet) == [kept]
        assert (
            project.db.execute(
                "SELECT publication_closed FROM ops WHERE id=?", (append,)
            ).fetchone()[0]
            == 1
        )


def test_partly_purged_keyed_refresh_keeps_surviving_cell_writes_undoable(tmp_path):
    from tests.engine.test_multi_parent_sheet_refresh import _seed_join, _run, _refresh

    path = tmp_path / "partial-refresh.frisket"
    state = _seed_join(path)
    with closing(state["project"]) as project:
        child = state["child_id"]
        previous_rows = project.visible_row_ids(child)
        column = next(
            c["id"] for c in project.columns(child) if c["name"] == "population"
        )
        kept = previous_rows[0]
        original = project.get_values(child, column)[kept]
        project.apply_edits(
            [
                {
                    "row_id": state["right_rows"][0],
                    "column_id": state["right_cols"]["population"],
                    "value": 9000,
                }
            ]
        )
        project.add_rows(
            state["right_id"],
            [{"state_fips": 1, "population": 10000}],
            state["right_cols"],
        )
        refreshed = _run(project, _refresh(sheet_id=child, key="refresh-before-purge"))
        assert refreshed.status == "completed", refreshed.errors
        refresh_op = refreshed.op_ids[0]
        doomed = next(
            row for row in project.visible_row_ids(child) if row not in previous_rows
        )
        assert project.get_values(child, column)[kept] == 9000
    with closing(Project(path, maintenance=True)) as project:
        finish_purge(project, begin_purge(project, row_ids=[doomed]))
    with closing(Project(path)) as project:
        assert project.undo() == refresh_op
        assert project.get_values(child, column)[kept] == original
        assert project.redo() == refresh_op
        assert project.get_values(child, column)[kept] == 9000
        assert doomed not in project.visible_row_ids(child)


@pytest.mark.parametrize("purge_after_undo", [False, True])
def test_partly_purged_sheet_creator_keeps_exact_restore_name_guard(
    tmp_path, purge_after_undo
):
    from frisket.engine.store.streaming_import import StreamingSheetWriter
    from frisket.engine.store.op_log import RestorationUnavailable

    path = tmp_path / "partial-sheet.frisket"
    with closing(Project.create(path)) as project:
        writer = StreamingSheetWriter.start(
            project,
            sheet_name="Documents",
            columns=[{"name": "text", "type": "text"}],
            project_id="test",
            action_kind="import.csv",
            idempotency_key="initial",
            params_hash="sha256:initial",
            action_id="act:initial",
            receipt_id="receipt:initial",
            source_ref={"kind": "file", "logical_path": "records.csv"},
        )
        writer.append_rows([{"text": "secret"}, {"text": "retained"}])
        writer.publish()
        sheet, creation = writer.sheet_id, project.op_cursor
        doomed, kept = project.visible_row_ids(sheet)
        if purge_after_undo:
            assert project.undo() == creation
    with closing(Project(path, maintenance=True)) as project:
        finish_purge(project, begin_purge(project, row_ids=[doomed]))
    with closing(Project(path)) as project:
        if not purge_after_undo:
            assert project.undo() == creation
        assert sheet not in {item["id"] for item in project.sheets()}
        replacement = project.add_sheet("Documents")
        with pytest.raises(RestorationUnavailable, match="already exists"):
            with project.transaction_scope():
                step_operation(project, "restore", op_id=creation)
        assert {item["id"] for item in project.sheets()} == {replacement}
        project.delete_sheet(replacement)
        with project.transaction_scope():
            step_operation(project, "restore", op_id=creation)
        assert project.visible_row_ids(sheet) == [kept]
        assert project.db.execute(
            "SELECT active,hidden FROM columns WHERE sheet_id=?", (sheet,)
        ).fetchone()[:] == (1, 0)


@pytest.mark.parametrize("purge_after_undo", [False, True])
def test_partly_purged_group_creation_restores_retained_generated_rows(
    tmp_path, purge_after_undo
):
    from frisket.engine.executor.group_summary_action import (
        run_typed_group_summary_action,
    )
    from tests.engine.test_reduce_group_summary_executor import (
        _seed,
        _reduce_action,
        _bound_request,
        _summary_router,
    )

    path = tmp_path / "groups.frisket"
    with closing(Project.create(path)) as project:
        seeded = _seed(project, tmp_path)
        router, _adapter = _summary_router()
        result = run_typed_group_summary_action(
            project,
            "p",
            _bound_request(_reduce_action(seeded["sheet_id"])),
            router,
        )
        assert result.status == "completed", result.errors
        sheet = result.outputs[0].sheet_id
        doomed, kept = project.visible_row_ids(sheet)
        creator = project.op_cursor
        column = next(c["id"] for c in project.columns(sheet) if c["name"] == "summary")
        expected = project.get_values(sheet, column)[kept]
        if purge_after_undo:
            assert project.undo() == creator
    with closing(Project(path, maintenance=True)) as project:
        finish_purge(project, begin_purge(project, row_ids=[doomed]))
    with closing(Project(path)) as project:
        if not purge_after_undo:
            assert project.undo() == creator
        assert project.redo() == creator
        assert project.visible_row_ids(sheet) == [kept]
        assert project.get_values(sheet, column)[kept] == expected
        assert (
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (doomed,)
            ).fetchone()[0]
            == 1
        )
        assert (
            project.db.execute(
                "SELECT publication_closed FROM ops WHERE id=?", (creator,)
            ).fetchone()[0]
            == 1
        )

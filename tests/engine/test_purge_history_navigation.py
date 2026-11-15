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

from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore


def test_undo_redo_preserves_internal_hidden_column(tmp_path):
    with closing(Project.create(tmp_path / "hidden.frisket")) as project:
        sheet_id = project.add_sheet("data")
        column_id = project.add_column(sheet_id, "internal", hidden=True)
        op_id = project.append_op("fixture")
        project.set_undo_info(op_id, {"created_columns": [column_id]})
        assert project.undo() == op_id
        assert project.get_column(column_id)["active"] == 0
        assert project.redo() == op_id
        column = project.get_column(column_id)
        assert (column["active"], column["hidden"]) == (1, 1)
        assert project.columns(sheet_id) == []
        assert [c["id"] for c in project.columns(sheet_id, include_hidden=True)] == [
            column_id
        ]


def test_redo_cannot_take_name_reserved_by_queued_action(tmp_path):
    with closing(Project.create(tmp_path / "claimed.frisket")) as project:
        sheet_id = project.add_sheet("data")
        column_id = project.add_column(sheet_id, "summary")
        op_id = project.append_op("fixture")
        project.set_undo_info(op_id, {"created_columns": [column_id]})
        project.undo()
        claims = OutputColumnClaimStore(project)
        reserved, conflict = claims.acquire(
            sheet_id=sheet_id,
            output_names=["summary"],
            action_kind="map.summarize",
            claim_token="pending-output",
        )
        assert conflict is None
        assert reserved[0]["column_id"] is None
        with pytest.raises(ValueError, match="output_column_busy"):
            project.redo()
        assert project.get_column(column_id)["active"] == 0
        assert (
            project.db.execute(
                "SELECT status FROM ops WHERE id=?", (op_id,)
            ).fetchone()[0]
            == "undone"
        )
        claims.release(claim_token="pending-output")
        assert project.redo() == op_id
        assert project.get_column(column_id)["active"] == 1

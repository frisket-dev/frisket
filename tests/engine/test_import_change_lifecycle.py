"""Root-table publication owns history even without source-cell payloads."""

from contextlib import closing

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.changes import ChangePublicationClosed, ChangeStore
from frisket.engine.store.op_log import operation_target, step_operation


@pytest.mark.parametrize("rows", [[], [{"value": None}], [{"value": "hello"}]])
def test_root_import_is_one_closed_restorable_change(tmp_path, rows):
    with closing(Project.create(tmp_path / "import.frisket")) as project:
        result = run_action_spec(
            project,
            {
                "action_id": "import.rows",
                "scope": {"kind": "project"},
                "sheet_name": "Imported",
                "params": {
                    "columns": [{"name": "value", "type": "text"}],
                    "rows": rows,
                },
                "idempotency_key": "root-import-history",
            },
            project_id="project",
        )
        assert result.status == "completed", result.errors
        op_id = result.op_ids[0]
        sheet_id = result.outputs[0].sheet_id
        op = project.db.execute(
            "SELECT change_order,publication_closed FROM ops WHERE id=?", (op_id,)
        ).fetchone()
        assert op["change_order"] is not None
        assert op["publication_closed"] == 1
        assert operation_target(project, "undo")["id"] == op_id
        assert {
            row[0]
            for row in project.db.execute(
                "SELECT kind FROM structural_changes WHERE op_id=?", (op_id,)
            )
        } == {"sheet", "column", *({"row"} if rows else set())}
        with pytest.raises(ChangePublicationClosed):
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                ChangeStore(project).start(op_id)
        for direction, hidden in (("undo", 1), ("redo", 0)):
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                step_operation(project, direction)
            assert (
                project.db.execute(
                    "SELECT hidden FROM sheets WHERE id=?", (sheet_id,)
                ).fetchone()[0]
                == hidden
            )
        column = project.columns(sheet_id)[0]
        assert column["name"] == "value"
        assert list(project.get_values(sheet_id, column["id"]).values()) == [
            row["value"] for row in rows
        ]

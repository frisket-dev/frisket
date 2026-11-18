"""Root-table publication owns history even without source-cell payloads."""

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.changes import ChangePublicationClosed, ChangeStore
from frisket.engine.store.op_log import operation_target, step_operation
from frisket.engine.store.streaming_import import StreamingSheetWriter


def test_null_only_append_pages_have_restorable_membership(make_project):
    project = make_project("null-append")
    sheet = project.add_sheet("Existing")
    column = project.add_column(sheet, "value")
    original = project.add_rows(sheet, [{"value": "old"}], {"value": column})
    writer = StreamingSheetWriter.start_append_session(
        project,
        session_id="null-append",
        writer_authority="writer",
        sheet_id=sheet,
        columns=[{"name": "value", "type": "text"}],
        project_id="project",
        action_kind="import.append_rows",
        idempotency_key="null-append",
        params_hash="hash",
        action_id="action",
        receipt_id="receipt",
        source_ref={"kind": "file", "label": "nulls.csv"},
    )
    added = []
    for cursor in range(2):
        added += writer.append_page(
            [{"value": None}], expected_cursor=cursor, next_cursor=cursor + 1
        )
    writer.finalize_session(expected_cursor=2, enumeration_complete=True)
    op_id = operation_target(project, "undo")["id"]
    assert [
        row[0]
        for row in project.db.execute(
            "SELECT object_id FROM structural_changes "
            "WHERE op_id=? AND kind='row' ORDER BY object_id",
            (op_id,),
        )
    ] == added
    assert project.undo() == op_id
    assert project.visible_row_ids(sheet) == original
    assert project.redo() == op_id
    assert project.visible_row_ids(sheet) == original + added
    assert project.get_values(sheet, column, added) == dict.fromkeys(added)


@pytest.mark.parametrize("rows", [[], [{"value": None}], [{"value": "hello"}]])
def test_root_import_is_one_closed_restorable_change(make_project, rows):
    project = make_project("import")
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

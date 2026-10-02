from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.server.app import create_app
from tests.http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_column_add_as_v1_action,
    post_column_patch_as_v1_action,
    post_column_set_type_as_v1_action,
    post_operation_undo_as_v1_action,
)


def test_importing_sheet_mutations_are_http_conflicts(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"), raise_server_exceptions=False)
    created = client.post("/api/projects", json={"name": "Import conflicts"})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    project = client.app.state.workspace.get(project_id)

    unrelated = project.add_sheet("Unrelated")
    deleted = client.delete(f"/api/projects/{project_id}/sheets/{unrelated}")
    assert deleted.status_code == 200, deleted.text

    writer = StreamingSheetWriter.start_session(
        project,
        session_id="import:http-conflict",
        writer_authority="claim:http-conflict",
        sheet_name="Importing",
        columns=[{"name": "value", "type": "text"}],
        project_id=project_id,
        action_kind="import.files",
        idempotency_key="import:http-conflict",
        params_hash="sha256:http-conflict",
        action_id="act:http-conflict",
        receipt_id="receipt:http-conflict",
        source_ref={"kind": "import_inventory", "ref": "import:http-conflict"},
    )
    row_id = writer.append_page(
        [{"value": "visible"}], expected_cursor=0, next_cursor=1
    )[0]
    column_id = int(project.columns(writer.sheet_id)[0]["id"])

    browsed = client.get(
        f"/api/projects/{project_id}/sheets/{writer.sheet_id}/data"
    )
    assert browsed.status_code == 200, browsed.text

    deleted = client.delete(
        f"/api/projects/{project_id}/sheets/{writer.sheet_id}"
    )
    assert deleted.status_code == 409, deleted.text
    assert deleted.json() == {
        "detail": "This sheet is read-only while its import is in progress."
    }

    edited = post_cell_edit_as_v1_action(
        client,
        project_id,
        [{"row_id": row_id, "column_id": column_id, "value": "blocked"}],
    )
    assert edited.status_code == 409, edited.text

    undone = post_operation_undo_as_v1_action(
        client, project_id, expected_op_id=writer._op_id
    )
    assert undone.status_code == 409, undone.text

    patched = post_column_patch_as_v1_action(
        client, project_id, column_id, {"format": "plain_text"}
    )
    assert patched.status_code == 409, patched.text
    retyped = post_column_set_type_as_v1_action(
        client, project_id, column_id, "integer"
    )
    assert retyped.status_code == 409, retyped.text
    added = post_column_add_as_v1_action(
        client, project_id, writer.sheet_id, "Blocked"
    )
    assert added.status_code == 409, added.text

    unrelated = project.add_sheet("Still mutable")
    allowed = post_column_add_as_v1_action(client, project_id, unrelated, "Allowed")
    assert allowed.status_code == 200, allowed.text
    assert project.get_values(writer.sheet_id, column_id)[row_id] == "visible"

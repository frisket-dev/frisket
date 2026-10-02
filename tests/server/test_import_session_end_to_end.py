"""Real multipart intake, queued typed publication, browsing and cancellation."""

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import HandlerRegistry, Worker
from frisket.engine.jobs.import_files import register_import_files_handler
from frisket.engine.jobs.queue import BLOB_METADATA_KIND
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.import_intake import import_intake_dir
from frisket.engine.store.import_inventory import ImportInventory
from frisket.server.app import create_app


@pytest.mark.parametrize("finish", ["complete", "keep", "remove"])
def test_http_import_worker_and_user_resolution(tmp_path, finish):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Evidence"}).json()["id"]
    ws = app.state.workspace
    registry = HandlerRegistry()
    register_import_files_handler(registry, workspace_root=ws.root, queue=ws.queue)
    worker = Worker(ws.queue, registry)
    base = f"/api/projects/{pid}/import/files/sessions"
    response = client.post(base, json={"sheet_name": "Documents"})
    assert response.status_code == 200, response.text
    ref = response.json()["import_ref"]
    session_url = f"{base}/{ref}"
    response = client.post(
        f"{session_url}/files",
        files=[
            ("files", ("one.pdf", b"original one", "application/pdf")),
            ("logical_paths", (None, "folder/one.pdf")),
            ("files", ("two.pdf", b"original two", "application/pdf")),
            ("logical_paths", (None, "folder/two.pdf")),
            ("batch_id", (None, "first")),
        ],
    )
    assert response.status_code == 200, response.text
    assert worker.run_once()
    status = client.get(session_url).json()
    assert status["committed_rows"] == 2, status
    sheet = status["sheet_id"]
    assert client.get(f"/api/projects/{pid}/sheets/{sheet}/data").status_code == 200
    assert client.delete(f"/api/projects/{pid}/sheets/{sheet}").status_code == 409

    if finish == "complete":
        response = client.post(f"{session_url}/seal")
        assert response.status_code == 200, response.text
        assert worker.run_once()
        expected_state = "completed"
    else:
        assert not ws.queue.list_jobs(status="queued")
        response = client.post(f"{session_url}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "cancelled"
        response = client.post(f"{session_url}/resolve", json={"decision": finish})
        assert response.status_code == 200, response.text
        expected_state = "kept" if finish == "keep" else "removed"
        if finish == "keep":
            assert any(
                job.kind == BLOB_METADATA_KIND
                for job in ws.queue.list_jobs(status="queued")
            )

    status = client.get(session_url).json()
    assert status["state"] == expected_state
    project = ws.get(pid)
    with ImportInventory(
        import_intake_dir(project.path, ref) / "inventory.db"
    ) as inventory:
        assert inventory.totals() == {"count": 2, "bytes": 24}
        assert inventory.through == 2
        assert inventory.sealed
        assert inventory.page(limit=1) == []
    assert project.row_count(sheet) == (0 if finish == "remove" else 2)
    receipt_id = project.db.execute(
        "SELECT receipt_id FROM import_sessions WHERE id=?", (ref,)
    ).fetchone()[0]
    receipt = ReceiptStore(project).find_by_id(receipt_id)
    assert (
        receipt.status
        == {"remove": "cancelled", "keep": "partial", "complete": "completed"}[finish]
    )
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert client.delete(f"/api/projects/{pid}/sheets/{sheet}").status_code == 200

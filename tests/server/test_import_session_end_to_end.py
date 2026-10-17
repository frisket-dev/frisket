"""Real multipart intake, queued typed publication, browsing and cancellation."""

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.engine.jobs.queue import BLOB_METADATA_KIND, IMPORT_FILES_PAGE_KIND
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.import_intake import import_intake_dir
from frisket.engine.store.import_inventory import ImportInventory
from frisket.project_settings import patch_project_settings
from frisket.server.app import create_app
from frisket.server.route_errors import RouteError
from frisket.server.services.search_preparation import SearchPreparationService
from http_test_helpers import drain_queue


@pytest.mark.parametrize(
    ("finish", "transient_failure", "enabled_late"),
    [
        pytest.param("complete", False, False, id="complete"),
        pytest.param("keep", False, False, id="keep"),
        pytest.param("remove", False, False, id="remove"),
        pytest.param("complete", True, False, id="complete-after-transient"),
        pytest.param("keep", True, False, id="keep-after-transient"),
        pytest.param("complete", True, True, id="enabled-after-import-start"),
    ],
)
def test_http_import_worker_and_user_resolution(
    tmp_path, monkeypatch, finish, transient_failure, enabled_late
):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Evidence"}).json()["id"]
    drain_queue(client, worker_id="import-session-setup")
    ws = app.state.workspace
    project = ws.get(pid)
    if enabled_late:
        patch_project_settings(project, {"prepare_new_files_automatically": False})
    worker = Worker(ws.queue, ws.registry)
    planning_unavailable = transient_failure
    if transient_failure:
        original_plan = SearchPreparationService._plan

        def plan(self, *args, **kwargs):
            if planning_unavailable:
                raise RouteError(503, "Preparation is temporarily unavailable")
            return original_plan(self, *args, **kwargs)

        monkeypatch.setattr(SearchPreparationService, "_plan", plan)
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
        # Search maintenance may run between publication and finalization.
        for _ in range(5):
            if client.get(session_url).json()["state"] == "completed":
                break
            if not worker.run_once():
                assert transient_failure
                break
        expected_state = "completed"
    else:
        assert not any(
            job.kind == IMPORT_FILES_PAGE_KIND
            for job in ws.queue.list_jobs(status="queued")
        )
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

    service = SearchPreparationService(ws)
    receipt_id = project.db.execute(
        "SELECT receipt_id FROM import_sessions WHERE id=?", (ref,)
    ).fetchone()[0]
    if enabled_late:
        assert (
            service.store.for_receipt(
                storage_org_id=None,
                project_id=pid,
                receipt_id=receipt_id,
                include_pending=True,
            )
            is None
        )
        patch_project_settings(project, {"prepare_new_files_automatically": True})
        reserve = SearchPreparationService.reserve_import

        def unavailable_reservation(*_args, **_kwargs):
            raise OSError("temporary reservation failure")

        monkeypatch.setattr(
            SearchPreparationService, "reserve_import", unavailable_reservation
        )
        refused = client.get(session_url)
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"].startswith("Import saved;")
        assert ReceiptStore(project).find_by_id(receipt_id).status == "completed"
        monkeypatch.setattr(SearchPreparationService, "reserve_import", reserve)
    if transient_failure:
        terminal = client.get(session_url)
        assert terminal.status_code == 200, terminal.text
        assert terminal.json()["state"] == expected_state
        assert terminal.json()["search_preparation_batch_id"] is None
        assert terminal.json()["search_preparation_status"] == "pending"
        pending = service.store.for_receipt(
            storage_org_id=None,
            project_id=pid,
            receipt_id=receipt_id,
            include_pending=True,
        )
        assert (
            pending is not None and pending.import_ref == ref and pending.plan is None
        )
        assert pending.settled_at is None
        assert (
            client.get(f"/api/projects/{pid}/search-preparation").json()["batches"]
            == []
        )
        planning_unavailable = False
        # Recovery owns the follow-up even after the importing client stops
        # polling. Its ordinary receipt lookup can discover the later batch.
        service.recover()
        assert service.batch_for_receipt(pid, receipt_id) == pending.id

    status = client.get(session_url).json()
    assert status["state"] == expected_state
    if finish != "remove":
        assert status["search_preparation_status"] == "available"
        preparation_id = status["search_preparation_batch_id"]
        assert preparation_id
        preparation_url = f"/api/projects/{pid}/search-preparation"
        failed = client.get(f"{preparation_url}/{preparation_id}")
        assert failed.status_code == 200, failed.text
        batch = failed.json()
        assert batch["status"] == "failed"
        assert batch["sheet_id"] == sheet
        assert batch["errors"] == [
            "PDF text preview could not be read. Check the PDF and selected page."
        ]
        assert batch["groups"] == batch["models"] == batch["jobs"] == []
        # Repeated reads retain the committed import and one visible failed
        # follow-up; corrupt bytes never cause an OCR or model setup admission.
        for _ in range(2):
            repeated = client.get(session_url).json()
            assert repeated["state"] == expected_state
            assert repeated["search_preparation_batch_id"] == preparation_id
            assert repeated["search_preparation_status"] == "available"
            listed = client.get(preparation_url).json()["batches"]
            assert [(item["id"], item["status"]) for item in listed] == [
                (preparation_id, "failed")
            ]
        assert client.get("/api/providers/models/pulls").json()["pulls"] == []
        manual = client.post(preparation_url, json={"sheet_id": sheet})
        assert manual.status_code == 422
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


@pytest.mark.parametrize("automatic,with_files", [(False, True), (True, False)])
def test_disabled_preparation_and_empty_import_publish_no_preparation(
    tmp_path, automatic, with_files
):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    ws = app.state.workspace
    pid = client.post("/api/projects", json={"name": "No follow-up"}).json()["id"]
    patch_project_settings(ws.get(pid), {"prepare_new_files_automatically": automatic})
    base = f"/api/projects/{pid}/import/files/sessions"
    created = client.post(base, json={"sheet_name": "Documents"})
    assert created.status_code == 200, created.text
    session_url = f"{base}/{created.json()['import_ref']}"
    if with_files:
        uploaded = client.post(
            f"{session_url}/files",
            files=[
                ("files", ("one.bin", b"original", "application/octet-stream")),
                ("logical_paths", (None, "one.bin")),
                ("batch_id", (None, "first")),
            ],
        )
        assert uploaded.status_code == 200, uploaded.text
    sealed = client.post(f"{session_url}/seal")
    assert sealed.status_code == 200, sealed.text
    worker = Worker(ws.queue, ws.registry)
    for _ in range(16):
        status = client.get(session_url)
        assert status.status_code == 200, status.text
        if status.json()["state"] == "completed":
            break
        assert worker.run_once()
    else:
        pytest.fail("import did not complete within bounded queue work")
    assert status.json()["committed_rows"] == (1 if with_files else 0)
    assert status.json()["search_preparation_batch_id"] is None
    assert status.json()["search_preparation_status"] == "none"
    preparation = SearchPreparationService(ws)
    preparation.recover()
    assert preparation.store.list_active_page(storage_org_id=None) == []
    assert client.get(f"/api/projects/{pid}/search-preparation").json()["batches"] == []
    assert client.get("/api/providers/models/pulls").json()["pulls"] == []
    assert not any(job.kind == "action.run" for job in ws.queue.list_jobs())

from fastapi.testclient import TestClient

from frisket.engine.executor import ImportWorkloadLimits
from frisket.engine.jobs.queue import IMPORT_FILES_PAGE_KIND
from frisket.server.app import create_app


class _RejectingImportAdmission:
    def __init__(self) -> None:
        self.reject = False

    def try_acquire(self):
        if self.reject:
            return None
        return _ImportPermit()


class _ImportPermit:
    def release(self) -> None:
        pass


def test_import_session_routes_admit_status_and_enqueue_bounded_pages(tmp_path):
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=2),
        )
    )
    project_id = client.post("/api/projects", json={"name": "Documents"}).json()["id"]
    base = f"/api/projects/{project_id}/import/files/sessions"

    created = client.post(base, json={"sheet_name": "Evidence"})
    assert created.status_code == 200, created.text
    status = created.json()
    ref = status["import_ref"]
    assert status == {
        "import_ref": ref,
        "state": "admitting",
        "admitted_files": 0,
        "admitted_bytes": 0,
        "through": 0,
        "committed_rows": 0,
        "committed_bytes": 0,
        "sheet_id": None,
        "sheet_name": "Evidence",
        "cancel_requested": False,
        "sealed": False,
        "error": None,
    }
    assert client.get(base).json() == {"sessions": [status]}
    assert client.get(f"{base}/{ref}").json() == status

    uploaded = client.post(
        f"{base}/{ref}/files",
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("logical_paths", (None, "folder/one.txt")),
            ("batch_id", (None, "batch-one")),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["admitted_files"] == 1
    assert uploaded.json()["through"] == 1

    sealed = client.post(f"{base}/{ref}/seal")
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["sealed"] is True
    jobs = [
        job
        for job in client.app.state.workspace.queue.list_jobs()
        if job.kind == IMPORT_FILES_PAGE_KIND
    ]
    assert sorted((job.payload["through"], job.payload["sealed"]) for job in jobs) == [
        (1, False),
        (1, True),
    ]
    assert all(job.payload["project_id"] == project_id for job in jobs)
    assert all(job.payload["import_ref"] == ref for job in jobs)


def test_import_session_create_uses_trusted_workload_limit(tmp_path):
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        )
    )
    project_id = client.post("/api/projects", json={"name": "Limited"}).json()["id"]
    base = f"/api/projects/{project_id}/import/files/sessions"
    ref = client.post(base, json={"sheet_name": "Evidence"}).json()["import_ref"]

    response = client.post(
        f"{base}/{ref}/files",
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("logical_paths", (None, "one.txt")),
            ("files", ("two.txt", b"two", "text/plain")),
            ("logical_paths", (None, "two.txt")),
            ("batch_id", (None, "too-many")),
        ],
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "upload file count exceeds deployment limit"}


def test_import_session_controls_bypass_saturated_import_admission(tmp_path):
    admission = _RejectingImportAdmission()
    client = TestClient(create_app(tmp_path / "workspace", import_admission=admission))
    project_id = client.post("/api/projects", json={"name": "Control"}).json()["id"]
    base = f"/api/projects/{project_id}/import/files/sessions"
    ref = client.post(base, json={"sheet_name": "Evidence"}).json()["import_ref"]
    admission.reject = True

    uploaded = client.post(
        f"{base}/{ref}/files",
        files=[
            ("files", ("one.txt", b"one", "text/plain")),
            ("logical_paths", (None, "one.txt")),
            ("batch_id", (None, "batch-one")),
        ],
    )
    assert uploaded.status_code == 429, uploaded.text

    cancelled = client.post(f"{base}/{ref}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    resolved = client.post(f"{base}/{ref}/resolve", json={"decision": "keep"})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "kept"

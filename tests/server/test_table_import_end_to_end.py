"""Exercise the application wiring with real staged bytes and queue workers."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.server.app import create_app
from frisket.server.services.table_imports.service import TableImportService
from http_test_helpers import drain_queue


def _review_columns(client, draft_url, draft):
    response = client.patch(
        f"{draft_url}/columns",
        json={
            "expected_revision": draft["revision"],
            "columns": [{"source_index": 0, "included": True, "target_type": "text"}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _set_destination(client, draft_url, draft, name):
    return client.patch(
        f"{draft_url}/destination",
        json={
            "expected_revision": draft["revision"],
            "destination": {"kind": "new", "sheet_name": name},
            "mappings": [
                {"source_index": 0, "target": {"kind": "new", "name": "name"}}
            ],
            "dedup": {"enabled": False},
        },
    )


@pytest.mark.parametrize(
    "input_kind,existing_name", [("paste", "Pasted rows"), ("file", "people")]
)
def test_default_table_destination_avoids_existing_sheet(
    tmp_path, input_kind, existing_name
):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Repeated import"}).json()["id"]
    drain_queue(client, worker_id="table-setup")
    project = app.state.workspace.get(pid)
    original_id = project.add_sheet(existing_name)
    column_id = project.add_column(original_id, "name")
    project.add_rows(original_id, [{"name": "Original"}], {"name": column_id})
    base = f"/api/projects/{pid}"
    original = client.get(f"{base}/sheets/{original_id}/data").json()

    response = client.post(
        f"{base}/import/table-imports/{input_kind}",
        files={"file": ("people.csv", b"name\nImported\n")},
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    draft_url = f"{base}/import/table-imports/{draft['draft_id']}"
    drain_queue(client, worker_id="table-analysis")
    draft = client.get(draft_url).json()
    name = draft["destination"]["configuration"]["sheet_name"]
    assert name != existing_name
    assert name.startswith(existing_name)
    draft = _review_columns(client, draft_url, draft)
    response = _set_destination(client, draft_url, draft, name)
    assert response.status_code == 200, response.text
    request = {
        "expected_revision": response.json()["revision"],
        "idempotency_key": "import-once",
    }
    response = client.post(f"{draft_url}/commit", json=request)
    assert response.status_code == 200, response.text
    imported = response.json()
    drain_queue(client, worker_id="table-commit")
    assert imported["sheet_id"] != original_id
    assert imported["sheet_name"] == name
    data = client.get(f"{base}/sheets/{imported['sheet_id']}/data").json()
    assert len(data["rows"]) == 1
    assert list(data["rows"][0]["cells"].values()) == ["Imported"]
    assert client.get(f"{base}/sheets/{original_id}/data").json() == original
    replay = client.post(f"{draft_url}/commit", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == imported
    assert len(project.sheets()) == 2


@pytest.mark.parametrize(
    "collision_timing", ["after_freeze", "previous_failed_attempt"]
)
def test_table_name_collision_preserves_original_and_import_identity(
    tmp_path, monkeypatch, collision_timing
):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app, raise_server_exceptions=False)
    pid = client.post("/api/projects", json={"name": "Name conflict"}).json()["id"]
    drain_queue(client, worker_id="table-setup")
    project = app.state.workspace.get(pid)
    base = f"/api/projects/{pid}"
    original_id = None

    def add_competing_sheet():
        nonlocal original_id
        original_id = project.add_sheet("Reviewed import")
        column_id = project.add_column(original_id, "name")
        project.add_rows(original_id, [{"name": "Original"}], {"name": column_id})

    response = client.post(
        f"{base}/import/table-imports/paste",
        files={"file": ("people.csv", b"name\nImported\n")},
    )
    assert response.status_code == 201, response.text
    draft_url = f"{base}/import/table-imports/{response.json()['draft_id']}"
    drain_queue(client, worker_id="table-analysis")
    draft = _review_columns(client, draft_url, client.get(draft_url).json())
    response = _set_destination(client, draft_url, draft, "Reviewed import")
    assert response.status_code == 200, response.text
    request = {
        "expected_revision": response.json()["revision"],
        "idempotency_key": "same-import",
    }

    if collision_timing == "after_freeze":
        freeze = TableImportService.freeze_for_commit

        def freeze_then_claim_name(self, *args, **kwargs):
            frozen = freeze(self, *args, **kwargs)
            add_competing_sheet()
            return frozen

        with monkeypatch.context() as patch:
            patch.setattr(
                TableImportService, "freeze_for_commit", freeze_then_claim_name
            )
            response = client.post(f"{draft_url}/commit", json=request)
    else:
        add_competing_sheet()

        def legacy_collision(*args, **kwargs):
            # Reproduce the original failure after reservation, before publication.
            raise sqlite3.IntegrityError("UNIQUE constraint failed: sheets.name")

        with monkeypatch.context() as patch:
            patch.setattr(StreamingSheetWriter, "start_session", legacy_collision)
            failed = client.post(f"{draft_url}/commit", json=request)
        assert failed.status_code == 500, failed.text
        assert len(project.sheets()) == 1
        response = client.post(f"{draft_url}/commit", json=request)

    assert response.status_code == 200, response.text
    imported = response.json()
    drain_queue(client, worker_id="table-commit")
    assert imported["sheet_id"] != original_id
    assert imported["sheet_name"] != "Reviewed import"
    assert imported["sheet_name"].startswith("Reviewed import")
    original = client.get(f"{base}/sheets/{original_id}/data").json()
    assert len(original["rows"]) == 1
    assert list(original["rows"][0]["cells"].values()) == ["Original"]
    data = client.get(f"{base}/sheets/{imported['sheet_id']}/data").json()
    assert len(data["rows"]) == 1
    assert list(data["rows"][0]["cells"].values()) == ["Imported"]
    replay = client.post(f"{draft_url}/commit", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == imported
    assert len(project.sheets()) == 2
    assert project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1


def test_reviewed_table_reaches_grid_errors_and_one_undo(tmp_path):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Table evidence"}).json()["id"]
    drain_queue(client, worker_id="table-setup")
    base = f"/api/projects/{pid}"
    response = client.post(
        f"{base}/import/table-imports/file",
        files={"file": ("amounts.csv", b"id,amount\n001,12.5\n002,bad,extra,more\n")},
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    draft_url = f"{base}/import/table-imports/{draft['draft_id']}"
    drain_queue(client, worker_id="table-analysis")
    draft = client.get(draft_url).json()
    assert draft["analysis"]["complete"]
    assert draft["analysis"]["exact_rows"] == 2
    assert app.state.workspace.get(pid).sheets() == []
    response = client.patch(
        f"{draft_url}/columns",
        json={
            "expected_revision": draft["revision"],
            "columns": [
                {"source_index": 0, "included": True, "target_type": "text"},
                {"source_index": 1, "included": True, "target_type": "number"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    response = client.patch(
        f"{draft_url}/destination",
        json={
            "expected_revision": draft["revision"],
            "destination": {"kind": "new", "sheet_name": "Amounts"},
            "mappings": [
                {
                    "source_index": column["source_index"],
                    "target": {"kind": "new", "name": column["name"]},
                }
                for column in draft["columns"]
                if column["included"]
            ],
            "dedup": {"enabled": False},
        },
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    request = {"expected_revision": draft["revision"], "idempotency_key": "import-once"}
    response = client.post(f"{draft_url}/commit", json=request)
    assert response.status_code == 200, response.text
    imported = response.json()
    replay = client.post(f"{draft_url}/commit", json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == imported
    worker = Worker(app.state.workspace.queue, app.state.workspace.registry)
    session_url = f"{base}/import/files/sessions/{imported['import_ref']}"
    for _ in range(12):
        status = client.get(session_url).json()
        if status["state"] == "completed":
            break
        assert worker.run_once(), status
    assert status["state"] == "completed", status
    assert status["committed_rows"] == 2
    response = client.get(f"{base}/sheets/{imported['sheet_id']}/data")
    assert response.status_code == 200, response.text
    sheet = response.json()
    names = {str(column["id"]): column["name"] for column in sheet["columns"]}
    values = [
        {names[key]: value for key, value in row["cells"].items()}
        for row in sheet["rows"]
    ]
    assert [row["id"] for row in values] == ["001", "002"]
    assert [row["amount"] for row in values] == [12.5, None]
    assert values[1]["extra values"] == ["extra", "more"]
    jobs = client.get(f"{base}/actions/jobs").json()["jobs"]
    job = next(job for job in jobs if job.get("import_errors"))
    assert sum(group["count"] for group in job["import_errors"]) > 0
    project = app.state.workspace.get(pid)
    assert project.undo() is not None
    assert project.sheets() == []

"""Exercise the application wiring with real staged bytes and queue workers."""

from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.server.app import create_app
from http_test_helpers import drain_queue


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

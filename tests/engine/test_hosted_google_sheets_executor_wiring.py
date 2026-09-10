from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.testclient import TestClient

from frisket.engine.executor import ExecutorDeps
from frisket.engine.jobs.worker import Worker
from frisket.server.app import create_app
from frisket.engine.store.receipts import ReceiptStore


class FakeGoogleSheetsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def export_tabs(
        self,
        *,
        connection: dict[str, Any],
        destination: dict[str, Any],
        tabs: list[dict[str, Any]],
        write_policy: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "connection": connection,
                "destination": destination,
                "tabs": tabs,
                "write_policy": write_policy,
            }
        )
        return {
            "spreadsheet_id": "wired-1",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/wired-1",
            "updated_tabs": [
                {
                    "title": tab["title"],
                    "sheet_id": tab["sheet_id"],
                    "row_count": len(tab["rows"]),
                    "column_count": len(tab["columns"]),
                }
                for tab in tabs
            ],
        }


def test_create_app_executor_deps_factory_wires_google_sheets_action(tmp_path) -> None:
    fake_client = FakeGoogleSheetsClient()
    seen: list[tuple[str, str | None]] = []

    def deps_factory(project_id: str, request: Request | None) -> ExecutorDeps:
        seen.append(
            (project_id, str(request.url.path) if request is not None else None)
        )
        return ExecutorDeps(
            connected_account_resolver=lambda provider, connection_id: {
                "id": connection_id,
                "provider": provider,
                "external_subject": "google-user-1",
                "external_email": "reporter@example.com",
                "refresh_token": "refresh-secret",
                "scopes": [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            },
            google_sheets_client=fake_client,
        )

    client = TestClient(
        create_app(tmp_path / "workspace", executor_deps_factory=deps_factory)
    )
    pid = client.post("/api/projects", json={"name": "Hosted Sheets"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name,status\nAda,ready\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    action = {
        "action_id": "export.google_sheets",
        "scope": {"kind": "project"},
        "output_names": {},
        "params": {
            "connection_id": "google_user_1",
            "source": {"kind": "current_sheet", "sheet_id": sheet_id},
            "destination": {
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Hosted export",
            },
            "write_policy": "replace_managed_tabs",
        },
        "idempotency_key": "hosted-google-sheets@sha256:v1",
    }
    challenge = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert challenge.status_code == 402, challenge.text
    promise_hash = challenge.json()["errors"][0]["details"]["promise_set_hash"]
    action["confirmation"] = promise_hash
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"] is None
    assert body["job_id"] is not None
    assert body["receipt_id"] is not None
    import_request_path = f"/api/projects/{pid}/import/csv"
    request_path = f"/api/projects/{pid}/actions/v1/run"
    assert seen == [
        (pid, import_request_path),
        (pid, request_path),
        (pid, request_path),
    ]

    ws = client.app.state.workspace
    assert Worker(ws.queue, ws.registry, worker_id="sheets-test").run_once() is True
    project = ws.get(pid)
    stored = ReceiptStore(project).find_by_id(body["receipt_id"])
    assert stored is not None
    receipt = stored.parsed()
    assert receipt.status == "completed"
    assert receipt.outputs[0].ref["spreadsheet_id"] == "wired-1"
    assert seen == [
        (pid, import_request_path),
        (pid, request_path),
        (pid, request_path),
        (pid, None),
    ]
    assert fake_client.calls[0]["connection"]["refresh_token"] == "refresh-secret"
    assert fake_client.calls[0]["tabs"][0]["rows"] == [["Ada", "ready"]]

    # ACTION-06C leaves the generic binding registrar as the sole registration
    # owner; the old export-specific module/import/call must be physically gone.
    import frisket.engine.jobs as jobs

    jobs_root = Path(__file__).resolve().parents[2] / "src/frisket/engine/jobs"
    assert not (jobs_root / "exports.py").exists()
    assert not hasattr(jobs, "register_export_action_job_handlers")
    for path in (jobs_root / "__init__.py", jobs_root / "worker.py"):
        # rule19: this is a source-composition/deletion contract.
        source = path.read_text(encoding="utf-8")
        assert "register_export_action_job_handlers" not in source

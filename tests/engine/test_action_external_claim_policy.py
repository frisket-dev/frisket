from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.actions.google_sheets_types import GoogleSheetsExportRequest
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.action_families.exports import (
    _google_sheets_confirmation_hash,
)
from frisket.engine.executor.action_specs import PlacementPolicy, execution_spec_for
from frisket.engine.executor.google_sheets_action import _identity
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.jobs.queue import ACTION_RUN_KIND
from frisket.engine.jobs.worker import Worker
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.server.app import create_app
from frisket.engine.store.receipts import ReceiptStore


class FakeGoogleSheetsClient:
    def __init__(
        self,
        *,
        fail: bool = False,
        error: BaseException | None = None,
    ) -> None:
        self.fail = fail
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.on_export: Any | None = None

    def export_tabs(
        self,
        *,
        connection: dict[str, Any],
        destination: dict[str, Any],
        tabs: list[dict[str, Any]],
        write_policy: str,
    ) -> dict[str, Any]:
        if self.on_export is not None:
            self.on_export()
        self.calls.append(
            {
                "connection": connection,
                "destination": destination,
                "tabs": tabs,
                "write_policy": write_policy,
            }
        )
        if self.error is not None:
            raise self.error
        if self.fail:
            raise RuntimeError("provider unavailable")
        return {
            "spreadsheet_id": destination.get("spreadsheet_id") or "sheet-created-1",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet-created-1",
            "updated_tabs": [
                {
                    "title": tab["title"],
                    "row_count": len(tab["rows"]),
                    "column_count": len(tab["columns"]),
                    "sheet_id": tab["sheet_id"],
                }
                for tab in tabs
            ],
        }


def _resolver(provider: str, connection_id: str) -> dict[str, Any] | None:
    if provider != "google" or connection_id != "conn-google-1":
        return None
    return {
        "id": connection_id,
        "provider": "google",
        "external_subject": "google-user-123",
        "external_email": "reporter@example.com",
        "refresh_token": "refresh-secret-1",
        "scopes": [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ],
    }


def _client(tmp_path: Path, fake: FakeGoogleSheetsClient) -> TestClient:
    def deps_factory(_project_id: str, _request: Any) -> ExecutorDeps:
        return ExecutorDeps(
            connected_account_resolver=_resolver,
            google_sheets_client=fake,
        )

    return TestClient(
        create_app(tmp_path / "workspace", executor_deps_factory=deps_factory)
    )


def _seed_sheet(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Sheets Export"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("rows.csv", "name,status\nAda,ready\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return pid, int(imported.json()["sheet_id"])


def _action(sheet_id: int, *, key: str = "google@sha256:v1") -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": "export.google_sheets",
        "scope": {"kind": "project"},
        "output_names": {},
        "params": {
            "connection_id": "conn-google-1",
            "source": {"kind": "current_sheet", "sheet_id": sheet_id},
            "destination": {
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Queued export",
            },
            "write_policy": "replace_managed_tabs",
        },
        "idempotency_key": key,
    }
    bound = typed_action_for_request(action)
    action["confirmation"] = _google_sheets_confirmation_hash(
        _identity(bound),
        GoogleSheetsExportRequest.model_validate(action["params"]),
        params_hash=typed_request_hash(bound),
    )
    return action


def _receipt_body(
    client: TestClient, project_id: str, receipt_id: str
) -> dict[str, Any]:
    project = client.app.state.workspace.get(project_id)
    stored = ReceiptStore(project).find_by_id(receipt_id)
    assert stored is not None
    return json.loads(stored.body)


def test_google_sheets_declares_external_claimed_queued_action_job() -> None:
    spec = execution_spec_for("export.google_sheets")
    assert spec is not None
    assert spec.lifecycle.placement is PlacementPolicy.QUEUED_ACTION_JOB
    assert spec.lifecycle.reservation.reserves_receipt is True
    assert spec.lifecycle.failure_commit.persist_failure is True
    assert spec.lifecycle.external_claim.claimed is True
    assert spec.lifecycle.external_claim.provider == "google_sheets"
    assert spec.lifecycle.external_claim.operation == "export_tabs"


def test_google_sheets_http_launch_reserves_before_provider_and_worker_finalizes(
    tmp_path: Path,
) -> None:
    fake = FakeGoogleSheetsClient()
    client = _client(tmp_path, fake)
    pid, sheet_id = _seed_sheet(client)
    action = _action(sheet_id)

    first = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert first.status_code == 200, first.text
    queued = first.json()
    assert queued["status"] == "queued"
    assert queued["run_id"] is None
    assert queued["job_id"] is not None
    assert queued["receipt_id"] is not None
    assert fake.calls == []

    duplicate = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "queued"
    assert duplicate.json()["receipt_id"] == queued["receipt_id"]
    assert duplicate.json()["job_id"] == queued["job_id"]
    assert fake.calls == []

    job = client.app.state.workspace.queue.get(queued["job_id"])
    assert job is not None
    assert job.kind == ACTION_RUN_KIND
    assert job.payload["action_job"]["action_kind"] == "export.google_sheets"
    assert "refresh-secret-1" not in json.dumps(job.payload)

    def assert_running_during_provider_call() -> None:
        assert _receipt_body(client, pid, queued["receipt_id"])["status"] == "running"

    fake.on_export = assert_running_during_provider_call
    worker = Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    )
    assert worker.run_once() is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["connection"]["refresh_token"] == "refresh-secret-1"

    receipt = _receipt_body(client, pid, queued["receipt_id"])
    assert receipt["status"] == "completed"
    assert receipt["action_kind"] == "export.google_sheets"
    assert receipt["exports"][0]["spreadsheet_id"] == "sheet-created-1"
    assert "refresh-secret-1" not in json.dumps(receipt)
    assert "refresh_token" not in json.dumps(receipt)


def test_google_sheets_provider_failure_persists_failed_claim_and_replay_is_noop(
    tmp_path: Path,
) -> None:
    fake = FakeGoogleSheetsClient(fail=True)
    client = _client(tmp_path, fake)
    pid, sheet_id = _seed_sheet(client)
    action = _action(sheet_id, key="google@sha256:provider-failure")

    launched = client.post(f"/api/projects/{pid}/actions/v1/run", json=action).json()
    assert launched["status"] == "queued"

    worker = Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    )
    assert worker.run_once() is True
    assert len(fake.calls) == 1

    receipt = _receipt_body(client, pid, launched["receipt_id"])
    assert receipt["status"] == "failed"
    assert receipt["errors"][0]["code"] == "external_effect_reconciliation_required"
    assert receipt["errors"][0]["details"]["reconciliation_required"] is True
    assert receipt["evidence"][0]["ref"]["kind"] == "attempted_google_sheets_tabs"

    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert replay.status_code == 400, replay.text
    body = replay.json()
    assert body["status"] == "failed"
    assert body["receipt_id"] == launched["receipt_id"]
    assert len(fake.calls) == 1


def test_google_sheets_provider_error_secrets_never_reach_queue_or_project(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    refresh_secret = "refresh-queued-secret"
    access_secret = "access-queued-secret"
    client_secret = "client-queued-secret"
    oversized = "q" * 10_000
    fake = FakeGoogleSheetsClient(
        error=RuntimeError(
            f"provider {refresh_secret} / {access_secret} / {client_secret} {oversized}"
        )
    )

    def secret_resolver(provider: str, connection_id: str) -> dict[str, Any] | None:
        if provider != "google" or connection_id != "conn-google-1":
            return None
        return {
            "id": connection_id,
            "provider": "google",
            "external_subject": "google-user-secret-test",
            "refresh_token": refresh_secret,
            "access_token": access_secret,
            "client_secret": client_secret,
        }

    def deps_factory(_project_id: str, _request: Any) -> ExecutorDeps:
        return ExecutorDeps(
            connected_account_resolver=secret_resolver,
            google_sheets_client=fake,
        )

    client = TestClient(
        create_app(tmp_path / "workspace", executor_deps_factory=deps_factory)
    )
    pid, sheet_id = _seed_sheet(client)
    caplog.set_level(logging.DEBUG, logger="frisket.executor")
    launched = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_action(sheet_id, key="google@sha256:queued-secret-error"),
    ).json()

    worker = Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    )
    assert worker.run_once() is True
    job = client.app.state.workspace.queue.get(launched["job_id"])
    assert job is not None
    assert job.status == "done"
    assert job.result is not None and job.result["status"] == "failed"
    receipt = _receipt_body(client, pid, launched["receipt_id"])
    project = client.app.state.workspace.get(pid)
    checkpoints = EffectCheckpointStore(project.db).list_all()

    durable_and_logged = "\n".join(
        (
            caplog.text,
            json.dumps(job.payload),
            json.dumps(job.result),
            str(job.error or ""),
            json.dumps(receipt),
            json.dumps(checkpoints),
        )
    )
    for secret in (refresh_secret, access_secret, client_secret):
        assert secret not in durable_and_logged
    assert oversized not in durable_and_logged
    assert "Traceback (most recent call last)" not in caplog.text


def test_google_sheets_worker_does_not_overwrite_terminal_receipt(
    tmp_path: Path,
) -> None:
    fake = FakeGoogleSheetsClient()
    client = _client(tmp_path, fake)
    pid, sheet_id = _seed_sheet(client)
    action = _action(sheet_id, key="google@sha256:terminal-before-worker")

    launched = client.post(f"/api/projects/{pid}/actions/v1/run", json=action).json()
    project = client.app.state.workspace.get(pid)
    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(launched["receipt_id"])
    assert stored is not None
    receipts.update_body_status(
        stored.parsed().model_copy(update={"status": "cancelled"})
    )

    worker = Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    )
    assert worker.run_once() is True
    assert fake.calls == []

    receipt = _receipt_body(client, pid, launched["receipt_id"])
    assert receipt["status"] == "cancelled"

    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["status"] == "cancelled"
    assert body["receipt_id"] == launched["receipt_id"]


def test_google_sheets_exhausted_worker_lease_terminalizes_without_provider_replay(
    tmp_path: Path,
) -> None:
    fake = FakeGoogleSheetsClient()
    client = _client(tmp_path, fake)
    pid, sheet_id = _seed_sheet(client)
    action = _action(sheet_id, key="google@sha256:lease-exhausted")

    launched = client.post(f"/api/projects/{pid}/actions/v1/run", json=action).json()
    assert launched["status"] == "queued"

    queue = client.app.state.workspace.queue
    first_claim = queue.claim("dead-sheets-worker-1", lease_seconds=-1.0)
    assert first_claim is not None and first_claim.id == launched["job_id"]
    assert first_claim.max_attempts == 2
    queue.acknowledge_handler_exit(
        first_claim.id,
        "dead-sheets-worker-1",
        authority_id=first_claim.handler_authority_id,
    )
    assert queue.recover_expired() == 1

    final_claim = queue.claim("dead-sheets-worker-2", lease_seconds=-1.0)
    assert final_claim is not None and final_claim.id == launched["job_id"]
    assert final_claim.attempts == final_claim.max_attempts == 2
    queue.acknowledge_handler_exit(
        final_claim.id,
        "dead-sheets-worker-2",
        authority_id=final_claim.handler_authority_id,
    )

    worker = Worker(queue, client.app.state.workspace.registry)
    assert worker.run_once() is False
    assert fake.calls == []

    receipt = _receipt_body(client, pid, launched["receipt_id"])
    assert receipt["status"] == "failed"
    assert receipt["errors"][0]["code"] == "action_job_lease_exhausted"

    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert replay.status_code == 400, replay.text
    body = replay.json()
    assert body["status"] == "failed"
    assert body["receipt_id"] == launched["receipt_id"]
    assert fake.calls == []


class _CrashAfterReturned(BaseException):
    pass


def test_google_sheets_returned_checkpoint_recovers_same_queue_job_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGoogleSheetsClient()
    client = _client(tmp_path, fake)
    pid, sheet_id = _seed_sheet(client)
    action = _action(sheet_id, key="google@sha256:returned-queue-crash")

    launched = client.post(f"/api/projects/{pid}/actions/v1/run", json=action).json()
    queue = client.app.state.workspace.queue
    before = queue.get(launched["job_id"])
    assert before is not None
    assert before.max_attempts == 2

    claimed = queue.claim("crashed-sheets-worker", lease_seconds=-1.0)
    assert claimed is not None and claimed.id == launched["job_id"]
    handler = client.app.state.workspace.registry.get(ACTION_RUN_KIND)
    assert handler is not None
    real_complete = EffectCheckpointStore.complete

    def complete_then_crash(store: EffectCheckpointStore, *args: Any, **kwargs: Any):
        real_complete(store, *args, **kwargs)
        raise _CrashAfterReturned

    monkeypatch.setattr(EffectCheckpointStore, "complete", complete_then_crash)
    handler_payload = dict(claimed.payload)
    handler_payload["job_id"] = claimed.id
    handler_payload["job_final_attempt"] = False
    with pytest.raises(_CrashAfterReturned):
        handler(
            handler_payload,
            JobHandlerContext.from_claimed_job(trusted_org_id=None),
        )
    monkeypatch.setattr(EffectCheckpointStore, "complete", real_complete)
    queue.acknowledge_handler_exit(
        claimed.id,
        "crashed-sheets-worker",
        authority_id=claimed.handler_authority_id,
    )

    assert len(fake.calls) == 1
    project = client.app.state.workspace.get(pid)
    stored_before = ReceiptStore(project).find_by_id(launched["receipt_id"])
    assert stored_before is not None and stored_before.status == "running"
    checkpoints = EffectCheckpointStore(project.db).list_all()
    assert len(checkpoints) == 1 and checkpoints[0]["state"] == "returned"

    assert queue.recover_expired() == 1
    requeued = queue.get(launched["job_id"])
    assert requeued is not None and requeued.status == "queued"
    worker = Worker(queue, client.app.state.workspace.registry, worker_id="recovery")
    assert worker.run_once() is True

    completed_job = queue.get(launched["job_id"])
    assert completed_job is not None
    assert completed_job.status == "done"
    assert completed_job.result["status"] == "completed"
    assert completed_job.result["receipt_id"] == launched["receipt_id"]
    receipt = _receipt_body(client, pid, launched["receipt_id"])
    assert receipt["status"] == "completed"
    assert receipt["exports"][0]["spreadsheet_id"] == "sheet-created-1"
    assert len(fake.calls) == 1

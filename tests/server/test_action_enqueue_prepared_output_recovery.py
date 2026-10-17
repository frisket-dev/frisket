from __future__ import annotations

from pathlib import Path

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


def test_prepared_recovery_rejects_recreated_output_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Prepared recovery fence")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    source_id = project.add_column(sheet_id, "note", type="text")
    project.add_rows(
        sheet_id,
        [{"note": "Call 212-555-0123"}],
        {"note": source_id},
    )
    body = {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
        },
        "output_names": {"extracted": "phone"},
        "idempotency_key": "prepared-recreated-output@1",
    }
    service = ActionRunService(workspace)
    original_enqueue = workspace.queue.enqueue

    def crash_before_enqueue(*_args, **_kwargs):
        raise RuntimeError("simulated crash before enqueue")

    monkeypatch.setattr(workspace.queue, "enqueue", crash_before_enqueue)
    with pytest.raises(RuntimeError, match="before enqueue"):
        service.run_action(project_id, body)
    monkeypatch.setattr(workspace.queue, "enqueue", original_enqueue)

    prepared_output_id = int(
        next(
            column["id"]
            for column in project.columns(sheet_id)
            if column["name"] == "phone"
        )
    )
    project.db.execute(
        "UPDATE columns SET name='phone (deleted)' WHERE id=?",
        (prepared_output_id,),
    )
    project.db.commit()
    replacement_id = project.add_column(sheet_id, "phone", type="text")
    assert replacement_id != prepared_output_id

    refused = service.run_action(project_id, body)

    assert refused.status_code == 400
    assert refused.payload["status"] == "failed"
    assert refused.payload["errors"][0]["code"] == "output_target_changed"
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )


def test_admission_lookup_keeps_receipt_without_run_and_scopes_identity(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = workspace.create("Reserved action")["id"]
    other_project_id = workspace.create("Another project")["id"]
    project = workspace.get(project_id)
    service = ActionRunService(workspace)
    identity = {"action_kind": "map.regex_extract", "idempotency_key": "reserved@1"}
    assert service.lookup_admission(project_id, **identity) is None

    ReceiptStore(project).insert_queued(
        Receipt(
            receipt_id="reserved-receipt",
            project_id=project_id,
            action_id="reserved-action",
            action_kind=identity["action_kind"],
            idempotency_key=identity["idempotency_key"],
            params_hash="reserved-params",
            status="queued",
        )
    )
    admission = service.lookup_admission(project_id, **identity)
    assert admission is not None
    assert admission.receipt.id == "reserved-receipt"
    assert admission.receipt.run_id is None and admission.receipt.status == "queued"
    assert admission.job is None
    assert service.lookup_admission(other_project_id, **identity) is None
    assert (
        service.lookup_admission(project_id, **{**identity, "action_kind": "media.ocr"})
        is None
    )
    assert (
        service.lookup_admission(
            project_id, **{**identity, "idempotency_key": "another-key"}
        )
        is None
    )
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_admission_lookup_finds_existing_scoped_job_without_recovering(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = Workspace(
        tmp_path / "workspace",
        enable_local_model_pull=False,
        queue_payload_extra={"storage_org_id": 17},
    )
    project_id = workspace.create("Prepared action")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    source_id = project.add_column(sheet_id, "note", type="text")
    project.add_rows(sheet_id, [{"note": "Call 212-555-0123"}], {"note": source_id})
    body = {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"input_columns": ["note"], "pattern": r"\d{3}-\d{3}-\d{4}"},
        "output_names": {"extracted": "phone"},
        "idempotency_key": "prepared-lookup@1",
    }
    service = ActionRunService(workspace)
    identity = {
        "action_kind": body["action_id"],
        "idempotency_key": body["idempotency_key"],
    }

    def crash_before_enqueue(*_args, **_kwargs):
        raise RuntimeError("simulated crash before enqueue")

    with monkeypatch.context() as patch:
        patch.setattr(workspace.queue, "enqueue", crash_before_enqueue)
        with pytest.raises(RuntimeError, match="before enqueue"):
            service.run_action(project_id, body)
    missing_job = service.lookup_admission(project_id, **identity)
    assert missing_job is not None and missing_job.receipt.run_id is not None
    assert missing_job.job is None
    run_id = missing_job.receipt.run_id
    # A job for another organization or project must not supply this admission.
    for foreign in (
        {"project_id": project_id, "storage_org_id": 18},
        {"project_id": "different-project", "storage_org_id": 17},
    ):
        workspace.queue.enqueue("project.run", {**foreign, "run_id": run_id})
    assert service.lookup_admission(project_id, **identity).job is None

    recovered = service.recover_prepared_action(
        project_id, receipt_id=missing_job.receipt.id
    )
    assert recovered is not None and recovered.status_code == 200
    assert recovered.payload["status"] == "queued"
    expected_job_id = recovered.payload["job_id"]
    workspace.queue.enqueue(
        "project.run",
        {"project_id": project_id, "run_id": run_id, "storage_org_id": 18},
    )
    found = service.lookup_admission(project_id, **identity)
    assert found is not None and found.job is not None
    assert found.receipt.id == missing_job.receipt.id and found.receipt.run_id == run_id
    assert found.job.id == expected_job_id
    assert found.job.project_id == project_id and found.job.storage_org_id == 17
    assert found.job.run_id == run_id and found.job.status == "queued"

from __future__ import annotations

from fastapi.testclient import TestClient
from dataclasses import dataclass
from dataclasses import replace
import pytest

from frisket.server.app import create_app
from frisket.server.services import search_preparation as preparation_module
from frisket.server.services.search_preparation import SearchPreparationService
from frisket.server.services.action_runs import ActionRunResponse
from frisket.server.workspace import Workspace
from frisket.engine.jobs.search_preparation_store import SearchPreparationStore


@dataclass(frozen=True)
class _Requirement:
    setup_ref: str = "engine-setup:rapidocr.local@1"
    capability: str = "ocr"
    display_name: str = "RapidOCR"
    runtime_ready: bool = True
    weights_ready: bool = False
    download_bytes_if_missing: int = 123
    license: str = "Apache-2.0"
    source_url: str = "https://example.invalid/rapidocr"
    runtime_remediation: str | None = None
    artifact_refs: tuple[str, ...] = ("sha256:fixture",)
    download_size_basis: str = "exact_checksum"
    runtime_installable: bool = False


def _unbound_ocr_action(workspace: Workspace):
    project_id = workspace.create("Concurrent preparation")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    service = SearchPreparationService(workspace)
    plan = service._plan(project, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    batch = service.store.create(
        workspace_root=str(workspace.root),
        storage_org_id=None,
        project_id=project_id,
        trigger_receipt_id=None,
        sheet_id=sheet_id,
        row_ids=None,
        source_cutoff=int(project.op_cursor),
        force_ocr=False,
        plan=plan,
    )
    [item] = plan["items"]
    child_key = service._child_key("ocr", item["column_id"], [item])
    service.store.reserve_child_and_advance(
        batch.id, expected_cursor=0, child_key=child_key, kind="ocr"
    )
    body = service._action_body(
        project,
        batch,
        "ocr",
        item["column_name"],
        [item],
        child_key,
    )
    return project_id, service, batch, body


def test_explicit_preparation_is_reconnectable_and_double_click_safe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Preparation"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    name_column = project.add_column(sheet_id, "Name", type="text")
    project.add_rows(sheet_id, [{"Name": "ordinary text"}], {"Name": name_column})

    body = {"sheet_id": sheet_id, "row_ids": project.visible_row_ids(sheet_id)}
    first = client.post(f"/api/projects/{project_id}/search-preparation", json=body)
    second = client.post(f"/api/projects/{project_id}/search-preparation", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    # Project writes leave lexical index work pending; the batch remains
    # preparing until the normal index worker makes the content query-visible.
    assert first.json()["status"] == "preparing"

    listed = client.get(f"/api/projects/{project_id}/search-preparation")
    assert listed.status_code == 200
    assert [batch["id"] for batch in listed.json()["batches"]] == [first.json()["id"]]
    fetched = client.get(
        f"/api/projects/{project_id}/search-preparation/{first.json()['id']}"
    )
    assert fetched.status_code == 200
    assert fetched.json() == first.json()


def test_explicit_preparation_rejects_rows_outside_visible_scope(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Preparation"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    project.add_column(sheet_id, "Name", type="text")

    response = client.post(
        f"/api/projects/{project_id}/search-preparation",
        json={"sheet_id": sheet_id, "row_ids": [999]},
    )
    assert response.status_code == 404


def test_missing_ocr_setup_never_starts_before_exact_consent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (_Requirement(),))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Images")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    setup_calls: list[tuple[str, str | None]] = []
    service = SearchPreparationService(
        workspace,
        setup_start=lambda ref, actor: setup_calls.append((ref, actor)) or 1,
    )

    batch = service.create(
        project_id,
        sheet_id=sheet_id,
        row_ids=None,
        force_ocr=False,
    )
    assert batch.status == "needs_consent"
    assert setup_calls == []
    decided = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=["engine-setup:rapidocr.local@1"],
        may_setup_models=True,
        initiated_by="owner-1",
    )
    assert decided.status == "preparing"
    assert setup_calls == [("engine-setup:rapidocr.local@1", "owner-1")]


def test_identical_decision_replays_after_setup_facts_change(
    tmp_path, monkeypatch
) -> None:
    requirements = [_Requirement()]
    monkeypatch.setattr(
        preparation_module, "_requirements", lambda: tuple(requirements)
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Decision replay")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    service = SearchPreparationService(workspace)
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    first = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="not_now",
        selected_setup_refs=[],
        may_setup_models=True,
        initiated_by="owner-1",
    )
    requirements[0] = replace(requirements[0], weights_ready=True)

    replay = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="not_now",
        selected_setup_refs=[],
        may_setup_models=True,
        initiated_by="owner-1",
    )

    assert replay.id == first.id
    assert replay.errors == []


def test_undecided_batch_recovers_when_required_engine_becomes_ready(
    tmp_path, monkeypatch
) -> None:
    requirements = [_Requirement()]
    monkeypatch.setattr(
        preparation_module, "_requirements", lambda: tuple(requirements)
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Ready elsewhere")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    service = SearchPreparationService(workspace)
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    assert service.store.list_active_page(storage_org_id=None)[0].id == batch.id

    requirements[0] = replace(requirements[0], weights_ready=True)
    service.recover()

    [child] = service.store.children(batch.id)
    assert child.receipt_id is not None and child.job_id is not None


def test_server_reconciles_completed_enclosure_from_separate_worker(
    tmp_path, monkeypatch
) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Enclosures")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    [row_id] = project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    job_id = workspace.queue.enqueue(
        "enclosure.download",
        {
            "project_id": project_id,
            "sheet_id": sheet_id,
            "row_id": row_id,
            "workspace_root": str(workspace.root),
        },
    )
    store = SearchPreparationStore(workspace.queue, workspace_root=str(workspace.root))
    pending = store.create_pending_materialization(
        workspace_root=str(workspace.root),
        storage_org_id=None,
        project_id=project_id,
        job_id=job_id,
        sheet_id=sheet_id,
        row_ids=[row_id],
    )
    claimed = workspace.queue.claim("separate-cli-worker")
    while claimed is not None and claimed.id != job_id:
        assert workspace.queue.complete(
            claimed.id, "separate-cli-worker", {"skipped_by_test": True}
        )
        claimed = workspace.queue.claim("separate-cli-worker")
    assert claimed is not None and claimed.id == job_id
    assert workspace.queue.complete(job_id, "separate-cli-worker", {"ok": True})

    SearchPreparationService(workspace).recover()

    hydrated = store.get(pending.id, project_id=project_id, storage_org_id=None)
    assert hydrated.plan is not None
    assert hydrated.plan["items"][0]["row_id"] == row_id
    assert store.children(pending.id)[0].receipt_id is not None


def test_eligible_ocr_consent_is_not_blocked_by_unavailable_transcription_runtime(
    tmp_path, monkeypatch
) -> None:
    transcription = replace(
        _Requirement(),
        setup_ref="engine-setup:parakeet-tdt.local-onnx@1",
        capability="transcription",
        display_name="Parakeet",
        runtime_ready=False,
        runtime_remediation="Install the standard local audio runtime.",
        artifact_refs=("hf:parakeet-fixture",),
    )
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (_Requirement(), transcription),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Mixed media")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    audio_column = project.add_column(sheet_id, "Audio", type="audio")
    image = project.add_blob(b"image", filename="scan.png", mime="image/png")
    audio = project.add_blob(b"audio", filename="clip.wav", mime="audio/wav")
    project.add_rows(
        sheet_id,
        [
            {
                "Image": {"blob": image, "filename": "scan.png", "mime": "image/png"},
                "Audio": {"blob": audio, "filename": "clip.wav", "mime": "audio/wav"},
            }
        ],
        {"Image": image_column, "Audio": audio_column},
    )
    setup_calls = []
    service = SearchPreparationService(
        workspace,
        setup_start=lambda ref, actor: setup_calls.append((ref, actor)) or 1,
    )

    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    assert batch.status == "needs_consent"
    assert batch.can_setup
    assert {model.purpose: model.can_setup for model in batch.models} == {
        "read_text": True,
        "transcribe": False,
    }
    decided = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[_Requirement.setup_ref],
        may_setup_models=True,
        initiated_by="owner-1",
    )
    assert decided.status == "preparing"
    assert setup_calls == [(_Requirement.setup_ref, "owner-1")]


def test_disappeared_required_setup_fails_without_enqueuing_action(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (_Requirement(),))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Missing requirement")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    service = SearchPreparationService(workspace, setup_start=lambda _ref, _actor: 17)
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[_Requirement.setup_ref],
        may_setup_models=True,
        initiated_by="owner-1",
    )

    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    service.recover()

    assert service.get(project_id, batch.id).status == "failed"
    assert service.store.children(batch.id) == []


def test_recovery_adopts_unbound_real_action_reservation(tmp_path, monkeypatch) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id, service, batch, body = _unbound_ocr_action(workspace)
    response = service.action_runs.run_action(project_id, body)
    assert response.payload["status"] == "queued"
    assert service.store.children(batch.id)[0].job_id is None

    # Simulate another process/restart advancing after action admission but
    # before the coordinator bound its child. Output columns now exist, so a
    # fresh request would have different replacement preconditions.
    service._advance(batch)

    [child] = service.store.children(batch.id)
    assert child.job_id is not None
    assert child.receipt_id is not None
    jobs = [
        job
        for job in workspace.queue.list_project_jobs(
            project_id, kind="project.run", limit=20
        )
        if job.action_kind == "media.ocr"
    ]
    assert [job.id for job in jobs] == [child.job_id]
    assert service.get(project_id, batch.id).errors == []


def test_recovery_resumes_prepared_action_with_no_queue_job(
    tmp_path, monkeypatch
) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id, service, batch, body = _unbound_ocr_action(workspace)
    original_enqueue = workspace.queue.enqueue

    def crash_before_enqueue(kind, payload, *args, **kwargs):
        if kind == "project.run" and payload.get("action_kind") == "media.ocr":
            raise RuntimeError("simulated process exit before queue enqueue")
        return original_enqueue(kind, payload, *args, **kwargs)

    monkeypatch.setattr(workspace.queue, "enqueue", crash_before_enqueue)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        service.action_runs.run_action(project_id, body)
    monkeypatch.setattr(workspace.queue, "enqueue", original_enqueue)
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )

    service._advance(batch)

    [child] = service.store.children(batch.id)
    assert child.job_id is not None and child.receipt_id is not None
    [job] = workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
    assert job.id == child.job_id
    assert service.get(project_id, batch.id).errors == []


def test_stop_does_not_resume_unbound_prepared_action(tmp_path, monkeypatch) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id, service, batch, body = _unbound_ocr_action(workspace)
    original_enqueue = workspace.queue.enqueue

    def crash_before_enqueue(kind, payload, *args, **kwargs):
        if kind == "project.run" and payload.get("action_kind") == "media.ocr":
            raise RuntimeError("simulated process exit before queue enqueue")
        return original_enqueue(kind, payload, *args, **kwargs)

    monkeypatch.setattr(workspace.queue, "enqueue", crash_before_enqueue)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        service.action_runs.run_action(project_id, body)
    monkeypatch.setattr(workspace.queue, "enqueue", original_enqueue)

    service.stop(project_id, batch.id)
    service.recover()

    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )
    assert service.get(project_id, batch.id).status == "stopped"


def test_stop_uses_action_run_cancellation_and_releases_output_claims(
    tmp_path, monkeypatch
) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id, service, batch, body = _unbound_ocr_action(workspace)
    queued = service.action_runs.run_action(project_id, body)
    [unbound] = service.store.children(batch.id)
    assert unbound.receipt_id is None and unbound.job_id is None
    project = workspace.get(project_id)
    receipt_id = str(queued.payload["receipt_id"])
    run_id = int(queued.payload["run_id"])
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims "
            "WHERE receipt_id=? AND status='active'",
            (receipt_id,),
        ).fetchone()[0]
        == 2
    )

    stopped = service.stop(project_id, batch.id)

    assert stopped.status == "stopped"
    assert (
        project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[
            "status"
        ]
        == "cancelled"
    )
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()["status"]
        == "cancelled"
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims "
            "WHERE receipt_id=? AND status='active'",
            (receipt_id,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "error_code", ["idempotency_in_progress", "output_column_busy"]
)
def test_raced_action_reservation_defers_without_poisoning_batch(
    tmp_path, monkeypatch, error_code
) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Reservation race")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )

    class ReservationInProgress:
        def run_action(self, _project_id, _body):
            return ActionRunResponse(
                status_code=200,
                payload={
                    "status": "failed",
                    "errors": [
                        {
                            "code": error_code,
                            "message": "ordinary action reservation is still publishing",
                        }
                    ],
                },
            )

    service = SearchPreparationService(workspace, action_runs=ReservationInProgress())
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)

    assert batch.errors == []
    [child] = service.store.children(batch.id)
    assert child.job_id is None and child.receipt_id is None
    assert service.store.list_active_page(storage_org_id=None)[0].id == batch.id


def test_recovery_observes_setup_completed_by_another_process(
    tmp_path, monkeypatch
) -> None:
    requirements = [_Requirement()]
    monkeypatch.setattr(
        preparation_module, "_requirements", lambda: tuple(requirements)
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Images")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    service = SearchPreparationService(workspace, setup_start=lambda _ref, _actor: 1)
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[_Requirement.setup_ref],
        may_setup_models=True,
        initiated_by="owner-1",
    )

    # A separate CLI worker makes the shared setup fact ready. A fresh server
    # coordinator observes that durable fact without any in-process callback.
    requirements[0] = replace(requirements[0], weights_ready=True)
    SearchPreparationService(workspace, setup_start=lambda _ref, _actor: 2).recover()

    resumed = service.get(project_id, batch.id)
    assert resumed.status == "preparing"
    assert len(resumed.jobs) == 1
    assert resumed.jobs[0].status == "queued"

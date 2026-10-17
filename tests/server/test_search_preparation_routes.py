from __future__ import annotations

from fastapi.testclient import TestClient
from dataclasses import dataclass
from dataclasses import replace
import pytest

from frisket.engine.jobs import model_pull_store
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
        storage_org_id=None,
        project_id=project_id,
        trigger_receipt_id=None,
        sheet_id=sheet_id,
        row_ids=None,
        force_ocr=False,
        plan=plan,
    )
    [item] = plan["items"]
    child_key = preparation_module._planned_child_key("ocr", item["column_id"], [item])
    service.store.reserve_child(batch.id, child_key=child_key, kind="ocr")
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
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )

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


def test_explicit_preparation_rejects_a_sheet_with_no_actionable_media(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Preparation"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Notes")
    text_column = project.add_column(sheet_id, "Note", type="text")
    project.add_rows(sheet_id, [{"Note": "already searchable"}], {"Note": text_column})

    response = client.post(
        f"/api/projects/{project_id}/search-preparation",
        json={"sheet_id": sheet_id},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "this sheet has no current media that needs search preparation"
    )
    listed = client.get(f"/api/projects/{project_id}/search-preparation").json()
    assert listed["batches"] == []
    assert listed["indexing"]["status"] in {"pending", "queued", "running"}


def test_preparation_list_projects_durable_indexing_work(tmp_path, monkeypatch) -> None:
    from frisket.search import drain_index

    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Indexing"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Notes")
    text_column = project.add_column(sheet_id, "Note", type="text")
    project.add_rows(sheet_id, [{"Note": "new text"}], {"Note": text_column})
    project._frisket_schedule_search_index()

    active = client.get(f"/api/projects/{project_id}/search-preparation").json()[
        "indexing"
    ]
    assert active["status"] == "queued"
    assert active["job_id"] is not None

    drain_index(project)
    assert client.app.state.workspace.queue.cancel(active["job_id"])
    semantic = client.get(f"/api/projects/{project_id}/search-preparation").json()[
        "indexing"
    ]
    assert semantic == {"status": "pending", "job_id": None, "error": None}
    repeated = client.get(f"/api/projects/{project_id}/search-preparation").json()[
        "indexing"
    ]
    assert repeated == semantic
    assert (
        client.app.state.workspace.queue.find_job_by_refs(
            "search.index", project_id=project_id
        )
        is None
    )

    queue = client.app.state.workspace.queue
    semantic_job_id = project._frisket_schedule_search_index()
    assert semantic_job_id is not None
    claimed = queue.claim("index-test")
    assert claimed is not None and claimed.id == semantic_job_id
    assert queue.fail(claimed.id, "index-test", "index failed", retry=False)
    failed = client.get(f"/api/projects/{project_id}/search-preparation").json()[
        "indexing"
    ]
    assert failed == {
        "status": "failed",
        "job_id": claimed.id,
        "error": "queue_job_failed: index failed",
    }
    assert queue.find_job_by_refs("search.index", project_id=project_id) is None


@pytest.mark.parametrize("mode", ["lexical", "semantic", "hybrid"])
def test_search_recovers_index_work_without_a_queued_job(tmp_path, monkeypatch, mode):
    # Exercise the real unavailable-model path without using a developer's cache.
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path / "model-runtime"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "model-cache"))
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = client.post("/api/projects", json={"name": "Recovery"}).json()["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Notes")
    column_id = project.add_column(sheet_id, "Note", type="text")
    project.add_rows(sheet_id, [{"Note": "blue wharf"}], {"Note": column_id})
    project._frisket_schedule_search_index()
    job = workspace.queue.find_job_by_refs("search.index", project_id=project_id)
    assert job is not None
    assert workspace.queue.cancel(job.id)
    status_url = f"/api/projects/{project_id}/search-preparation"
    assert client.get(status_url).json()["indexing"] == {
        "status": "pending",
        "job_id": None,
        "error": None,
    }
    body = {
        "scope": {"kind": "project"},
        "query": {"kind": "text", "text": "wharf", "lexical_syntax": "plain"},
        "mode": mode,
        "group_by": "source",
        "limit": 10,
        "ranking": {"rerank": "off"},
    }
    response = client.post(f"/api/projects/{project_id}/search", json=body)
    assert response.status_code == 200, response.text
    recovered = workspace.queue.find_job_by_refs("search.index", project_id=project_id)
    assert recovered is not None
    assert recovered.status == "queued"
    assert recovered.id != job.id
    assert client.get(status_url).json()["indexing"]["job_id"] == recovered.id
    # Repeated searches share the already queued work.
    repeated = client.post(f"/api/projects/{project_id}/search", json=body)
    assert repeated.status_code == 200, repeated.text
    assert (
        workspace.queue.find_job_by_refs("search.index", project_id=project_id).id
        == recovered.id
    )

    # Use the production worker handler, not a mocked transition to "ready".
    from frisket.engine.jobs import HandlerRegistry, Worker
    from frisket.engine.jobs.search_index import register_search_index_handler

    registry = HandlerRegistry()
    register_search_index_handler(
        registry,
        workspace_root=workspace.root,
        queue=workspace.queue,
        embedding_encoder=workspace.embedding_runtime,
    )
    worker = Worker(workspace.queue, registry)
    for _ in range(10):
        if not worker.run_once():
            break
    else:
        pytest.fail("indexing did not finish")
    assert workspace.queue.get(recovered.id).status == "done"
    assert client.get(status_url).json()["indexing"]["status"] == "idle"
    result = client.post(
        f"/api/projects/{project_id}/search", json={**body, "mode": "lexical"}
    )
    assert result.status_code == 200, result.text
    assert len(result.json()["groups"]) == 1
    assert (
        workspace.queue.find_job_by_refs("search.index", project_id=project_id) is None
    )


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


def test_unselected_manual_pull_is_not_attached_to_preparation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (_Requirement(),))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Manual pull")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    model_pull_store.create_or_get_active(
        workspace.queue.engine,
        workspace_root=str(workspace.root),
        model_ref=_Requirement.setup_ref,
        initiated_by="operator",
    )

    batch = SearchPreparationService(workspace).create(
        project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False
    )

    assert batch.status == "needs_consent"
    assert batch.models[0].pull_id is None


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


def test_recovery_retires_automatic_intent_with_invalid_page_selector(
    tmp_path, monkeypatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Invalid page")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    row_id = project.add_rows(sheet_id, [{}], {})[0]
    job_id = workspace.queue.enqueue(
        "enclosure.download",
        {"project_id": project_id, "workspace_root": str(workspace.root)},
    )
    store = SearchPreparationStore(workspace.queue, workspace_root=str(workspace.root))
    store.create_pending_materialization(
        storage_org_id=None,
        project_id=project_id,
        job_id=job_id,
        sheet_id=sheet_id,
        row_ids=[row_id],
    )
    service = SearchPreparationService(workspace)

    class InvalidPageSelector(Exception):
        status_code = 422

    def reject_invalid_page(_row) -> None:
        raise InvalidPageSelector

    monkeypatch.setattr(service, "_recover_pending", reject_invalid_page)
    service.recover()

    assert store.list_active_page(storage_org_id=None) == []


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


def test_empty_project_does_not_queue_or_report_search_preparation(tmp_path):
    client = TestClient(create_app(tmp_path / "workspace"))
    response = client.post("/api/projects", json={"name": "Empty"})
    assert response.status_code == 200, response.text
    project_id = response.json()["id"]
    workspace = client.app.state.workspace
    project = workspace.get(project_id)
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "body")
    url = f"/api/projects/{project_id}/search-preparation"
    for _ in range(2):
        status = client.get(url)
        assert status.status_code == 200, status.text
        assert status.json() == {
            "batches": [],
            "indexing": {"status": "idle", "job_id": None, "error": None},
        }
    assert workspace.queue.counts()["queued"] == 0

    project.add_rows(sheet, [{"body": "first searchable content"}], {"body": column})
    # Direct library writes record work; a search is a normal enqueue recovery
    # boundary (production imports also schedule at their commit boundary).
    searched = client.post(
        f"/api/projects/{project_id}/search",
        json={
            "scope": {"kind": "project"},
            "query": {"kind": "text", "text": "content", "lexical_syntax": "plain"},
            "mode": "lexical",
            "group_by": "source",
            "limit": 10,
            "ranking": {"rerank": "off"},
        },
    )
    assert searched.status_code == 200, searched.text
    status = client.get(url).json()["indexing"]
    assert status["status"] == "queued"
    assert status["job_id"] is not None

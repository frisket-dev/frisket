from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import json
import sqlite3
from dataclasses import dataclass
from dataclasses import replace
import pytest

from frisket.engine.jobs import model_pull_store
from frisket.engine.jobs.projection import job_run_id
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.pdf_text import PdfTextError, PdfTextSample
from frisket.server.app import create_app
from frisket.server.route_errors import register_route_error_handler
from frisket.server.routes.search_preparation import register_search_preparation_routes
from frisket.server.services.selector_choices_capabilities import SelectorCapabilities
from frisket.server.services import search_preparation as preparation_module
from frisket.server.services import search_preparation_planning as planning_module
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


@pytest.mark.parametrize("overflow", [False, True])
def test_completed_preparation_exposes_bounded_receipt_warnings_without_result_scan(
    tmp_path, monkeypatch, overflow
):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )
    monkeypatch.setattr(
        preparation_module, "index_work_marker", lambda *args, **kwargs: None
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id, service, batch, body = _unbound_ocr_action(workspace)
    queued = service.action_runs.run_action(project_id, body)
    [child] = service.store.children(batch.id)
    service.store.bind_child(
        child.id,
        job_id=queued.payload["job_id"],
        receipt_id=queued.payload["receipt_id"],
    )
    project = workspace.get(project_id)
    receipts = ReceiptStore(project)
    stored = receipts.find_by_id(queued.payload["receipt_id"])
    warnings = (
        [f"Page {page} failed during OCR." for page in range(120)]
        if overflow
        else ["Page 2 failed during OCR."] * 2
    )
    assert receipts.update_body_status(
        stored.parsed().model_copy(update={"status": "completed", "warnings": warnings})
    )

    def deny_result_warning_reads(action, table, column, *_):
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ
            and table == "results"
            and column == "warnings"
            else sqlite3.SQLITE_OK
        )

    project.db.set_authorizer(deny_result_warning_reads)
    try:
        terminal = service.get(project_id, batch.id)
        assert service.get(project_id, batch.id).warnings == terminal.warnings
    finally:
        project.db.set_authorizer(None)
    assert terminal.status == "complete"
    assert terminal.groups[0].status == "complete"
    assert terminal.errors == []
    assert terminal.warnings == (
        [*warnings[:99], "Additional preparation warnings omitted."]
        if overflow
        else [warnings[0]]
    )


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
    assert first.json()["status"] == "needs_consent"

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


def test_preparation_lists_independent_pdf_checkpoints_across_index_jobs(
    tmp_path, monkeypatch
):
    from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE, PDF_PAGE_RECIPE_ID
    from frisket.retrieval.sources import blob_source_revision
    from frisket.retrieval.store import RetrievalStore
    from frisket.search import drain_index

    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "PDF progress"}).json()[
        "id"
    ]
    workspace = client.app.state.workspace
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "file", type="file")
    sources = []
    for filename in ("small.pdf", "large.pdf"):
        digest = project.add_blob(
            filename.encode(), filename=filename, mime="application/pdf"
        )
        project.add_rows(sheet, [{"file": {"blob": digest}}], {"file": column})
        sources.append(blob_source_revision(blob_hash=digest))
    drain_index(project)
    with RetrievalStore(project) as store:
        for source, next_page, page_count in zip(sources, (3, 1897), (10, 24909)):
            store.begin_preparation(
                source,
                recipe_id=PDF_PAGE_RECIPE_ID,
                channel="pdf_page",
                space_id=DEFAULT_TEXT_SPACE.identity,
            )
            assert store.advance_preparation(
                source,
                recipe_id=PDF_PAGE_RECIPE_ID,
                channel="pdf_page",
                space_id=DEFAULT_TEXT_SPACE.identity,
                cursor_json=json.dumps(
                    {
                        "kind": "pdf_pages",
                        "source_id": source.source_id,
                        "recipe_id": PDF_PAGE_RECIPE_ID,
                        "next_page": next_page,
                        "page_count": page_count,
                    }
                ),
            )
    initial_job = project._frisket_schedule_search_index()
    url = f"/api/projects/{project_id}/search-preparation"
    first = client.get(url)
    assert first.status_code == 200, first.text
    indexing = first.json()["indexing"]
    assert indexing["job_id"] == initial_job
    assert not indexing["pdf_files_truncated"]
    assert {
        file["filename"]: (file["completed"], file["total"])
        for file in indexing["pdf_files"]
    } == {"small.pdf": (2, 10), "large.pdf": (1896, 24909)}

    with RetrievalStore(project) as store:
        assert store.advance_preparation(
            sources[1],
            recipe_id=PDF_PAGE_RECIPE_ID,
            channel="pdf_page",
            space_id=DEFAULT_TEXT_SPACE.identity,
            cursor_json=json.dumps(
                {
                    "kind": "pdf_pages",
                    "source_id": sources[1].source_id,
                    "recipe_id": PDF_PAGE_RECIPE_ID,
                    "next_page": 1905,
                    "page_count": 24909,
                }
            ),
        )
    next_job = workspace.queue.enqueue(
        "search.index",
        {
            "project_id": project_id,
            "workspace_root": str(workspace.root),
            "dedupe_key": "pdf-progress:next",
        },
    )
    second = client.get(url).json()["indexing"]
    assert second["job_id"] == next_job != indexing["job_id"]
    assert [file["source_id"] for file in second["pdf_files"]] == [
        file["source_id"] for file in indexing["pdf_files"]
    ]
    assert {
        file["filename"]: (file["completed"], file["total"])
        for file in second["pdf_files"]
    } == {"small.pdf": (2, 10), "large.pdf": (1904, 24909)}


def test_preparation_indexing_prefers_running_claim_over_queued_continuation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    workspace = Workspace(
        tmp_path / "workspace", queue_payload_extra={"storage_org_id": 17}
    )
    project_id = workspace.create("Indexing continuation")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Notes")
    column_id = project.add_column(sheet_id, "Note", type="text")
    project.add_rows(sheet_id, [{"Note": "new text"}], {"Note": column_id})
    running_id = project._frisket_schedule_search_index()
    queue = workspace.queue
    claimed = queue.claim("index-test")
    assert claimed is not None and claimed.id == running_id
    refs = {
        "project_id": project_id,
        "storage_org_id": 17,
        "workspace_root": str(workspace.root),
    }
    for name, value in (
        ("project_id", "another-project"),
        ("storage_org_id", 18),
        ("workspace_root", str(tmp_path / "another-workspace")),
    ):
        unrelated_id = queue.enqueue("search.index", {**refs, name: value})
        unrelated = queue.claim("another-index-test")
        assert unrelated is not None and unrelated.id == unrelated_id
    continuation_id = queue.enqueue(
        "search.index", {**refs, "dedupe_key": "search-index:continuation"}
    )
    service = SearchPreparationService(workspace)

    assert service.list(project_id).indexing.model_dump() == {
        "status": "running",
        "job_id": running_id,
        "error": None,
        "pdf_files": [],
        "pdf_files_truncated": False,
    }
    assert queue.complete(running_id, "index-test", {"complete": False})
    assert service.list(project_id).indexing.model_dump() == {
        "status": "queued",
        "job_id": continuation_id,
        "error": None,
        "pdf_files": [],
        "pdf_files_truncated": False,
    }


def test_pdf_progress_is_optional_and_skipped_when_indexing_is_idle(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    monkeypatch.setattr(preparation_module, "index_work_marker", lambda *_args: None)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Optional progress")["id"]
    service = SearchPreparationService(workspace)
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(True)
        raise sqlite3.OperationalError("progress unavailable")

    monkeypatch.setattr(preparation_module, "read_pdf_indexing_progress", unavailable)
    assert service.list(project_id).indexing.status == "idle"
    assert calls == []
    job = workspace.queue.enqueue(
        "search.index",
        {
            "project_id": project_id,
            "workspace_root": str(workspace.root),
        },
    )
    status = service.list(project_id).indexing
    assert calls == [True]
    assert status.status == "queued" and status.job_id == job
    assert status.pdf_files == [] and status.pdf_files_truncated is False


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
    assert semantic == {
        "status": "pending",
        "job_id": None,
        "error": None,
        "pdf_files": [],
        "pdf_files_truncated": False,
    }
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
        "pdf_files": [],
        "pdf_files_truncated": False,
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
        "pdf_files": [],
        "pdf_files_truncated": False,
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


@pytest.mark.parametrize(
    "media_type,mime,capability",
    [
        ("image", "image/png", "ocr"),
        ("audio", "audio/wav", "transcription"),
    ],
)
def test_manual_ready_media_requires_decision_across_recovery(
    tmp_path, monkeypatch, media_type, mime, capability
):
    if capability == "transcription":
        monkeypatch.setattr(
            "frisket.execution.definitions.parakeet_runtime_present", lambda: True
        )
        monkeypatch.setattr(
            "frisket.execution.definitions.parakeet_artifacts_present", lambda: True
        )
    requirement = replace(_Requirement(), capability=capability, weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (requirement,))
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = workspace.create("Manual review")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Files")
    column = project.add_column(sheet, "Source", type=media_type)
    digest = project.add_blob(b"media", filename="source", mime=mime)
    project.add_rows(
        sheet, [{"Source": {"blob": digest, "mime": mime}}], {"Source": column}
    )
    base = f"/api/projects/{project_id}/search-preparation"
    opened = client.post(base, json={"sheet_id": sheet})
    assert opened.status_code == 200, opened.text
    batch = opened.json()
    assert batch["status"] == "needs_consent"
    for _ in range(2):
        service = SearchPreparationService(workspace)
        service.recover()
        assert service.get(project_id, batch["id"]).jobs == []
        assert service.store.children(batch["id"]) == []
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )
    deferred = client.post(
        f"{base}/{batch['id']}/decision",
        json={"plan_hash": batch["plan_hash"], "decision": "not_now"},
    )
    assert deferred.status_code == 422
    assert service.store.children(batch["id"]) == []
    accepted = client.post(
        f"{base}/{batch['id']}/decision",
        json={"plan_hash": batch["plan_hash"], "decision": "prepare"},
    )
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()["jobs"]) == 1
    service.recover()
    assert len(service.store.children(batch["id"])) == 1


@pytest.mark.parametrize("origin", ["import", "materialization"])
def test_trusted_automatic_ready_media_still_admits_without_downloads(
    tmp_path, monkeypatch, origin
):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Automatic preparation")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Files")
    column = project.add_column(sheet, "Source", type="image")
    digest = project.add_blob(b"media", mime="image/png")
    rows = project.add_rows(
        sheet, [{"Source": {"blob": digest, "mime": "image/png"}}], {"Source": column}
    )
    service = SearchPreparationService(workspace)
    if origin == "import":
        batch_id = service.create_after_import(
            project_id, receipt_id="receipt:trusted-import", sheet_id=sheet
        )
    else:
        # Bulk import has no enclosure job ID. A transient preview failure
        # must leave an exact scope that a new coordinator can recover.
        original_plan = service._plan

        def transient(*_args, **_kwargs):
            raise preparation_module.RouteError(503, "preview temporarily unavailable")

        monkeypatch.setattr(service, "_plan", transient)
        with pytest.raises(preparation_module.RouteError):
            service.create_after_materialization(
                project_id, sheet_id=sheet, row_ids=rows
            )
        [pending] = service.store.list_active_page(storage_org_id=None)
        assert pending.plan is None
        assert pending.captured_sources is not None
        batch_id = pending.id
        extra = project.add_column(sheet, "Later", type="image")
        project.apply_edits(
            [
                {
                    "row_id": rows[0],
                    "column_id": extra,
                    "value": {"blob": digest, "mime": "image/png"},
                }
            ]
        )
        project.add_rows(
            sheet,
            [{"Source": {"blob": digest, "mime": "image/png"}}],
            {"Source": column},
        )
        monkeypatch.setattr(service, "_plan", original_plan)
    assert batch_id is not None
    SearchPreparationService(workspace).recover()
    assert len(service.get(project_id, batch_id).jobs) == 1
    if origin == "import":
        # The same receipt remains stable after action admission changed the
        # project cursor by creating ordinary output columns.
        assert (
            service.create_after_import(
                project_id, receipt_id="receipt:trusted-import", sheet_id=sheet
            )
            == batch_id
        )
    assert (
        model_pull_store.list_recent(
            workspace.queue.engine, workspace_root=str(workspace.root)
        )
        == []
    )
    planned = service.store.get(
        batch_id, project_id=project_id, storage_org_id=None
    ).plan
    assert {(item["row_id"], item["column_id"]) for item in planned["items"]} == {
        (rows[0], column)
    }


def test_optional_setup_permission_is_consistent_on_every_plan_response(
    tmp_path, monkeypatch
):
    semantic = replace(
        _Requirement(),
        setup_ref="engine-setup:embeddinggemma-2.local@1",
        capability="semantic_search",
    )
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True), semantic),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Optional setup")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Files")
    column = project.add_column(sheet, "Source", type="image")
    digest = project.add_blob(b"media", mime="image/png")
    project.add_rows(
        sheet, [{"Source": {"blob": digest, "mime": "image/png"}}], {"Source": column}
    )
    setup_calls = []
    service = SearchPreparationService(
        workspace, setup_start=lambda ref, actor: setup_calls.append(ref) or 1
    )
    app = FastAPI()
    register_route_error_handler(app)
    permission = [False]
    register_search_preparation_routes(
        app,
        service=service,
        capabilities_for=lambda _request, _pid: SelectorCapabilities(
            manage_model_downloads=permission[0], may_run_actions=permission[0]
        ),
    )
    client = TestClient(app)
    base = f"/api/projects/{project_id}/search-preparation"
    created = client.post(base, json={"sheet_id": sheet})
    assert created.status_code == 200, created.text
    batch = created.json()
    for projected in [
        batch,
        client.get(f"{base}/{batch['id']}").json(),
        client.get(base).json()["batches"][0],
    ]:
        assert not projected["can_stop"]
        assert not projected["can_setup"]
        assert all(not model["can_setup"] for model in projected["models"])
    rejected = client.post(
        f"{base}/{batch['id']}/decision",
        json={
            "plan_hash": batch["plan_hash"],
            "decision": "prepare",
            "selected_setup_refs": [semantic.setup_ref],
        },
    )
    assert rejected.status_code == 403
    assert setup_calls == []
    assert service.store.children(batch["id"]) == []
    no_download = client.post(
        f"{base}/{batch['id']}/decision",
        json={"plan_hash": batch["plan_hash"], "decision": "prepare"},
    )
    assert no_download.status_code == 403
    assert (
        service.store.get(
            batch["id"], project_id=project_id, storage_org_id=None
        ).decision
        is None
    )
    assert service.store.children(batch["id"]) == []
    assert client.post(f"{base}/{batch['id']}/stop").status_code == 403
    permission[0] = True
    allowed = client.get(f"{base}/{batch['id']}").json()
    assert allowed["can_stop"]
    assert allowed["can_setup"]
    assert next(
        model for model in allowed["models"] if model["setup_ref"] == semantic.setup_ref
    )["can_setup"]
    accepted = client.post(
        f"{base}/{batch['id']}/decision",
        json={
            "plan_hash": batch["plan_hash"],
            "decision": "prepare",
            "selected_setup_refs": [semantic.setup_ref],
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert setup_calls == [semantic.setup_ref]


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


@pytest.mark.parametrize("force_ocr", [False, True])
def test_ready_pdf_ocr_waits_for_exact_decision_over_http(
    tmp_path, monkeypatch, force_ocr
):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )

    async def sample(_path, *, page, **_kwargs):
        return PdfTextSample("", page, 24909, False)

    monkeypatch.setattr(planning_module, "sample_pdf_text", sample)
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = workspace.create("PDF review")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Documents")
    document = project.add_column(sheet, "document", type="file")
    digest = project.add_blob(b"pdf", mime="application/pdf")
    rows = project.add_rows(
        sheet,
        [{"document": {"blob": digest, "mime": "application/pdf"}}],
        {"document": document},
    )
    base = f"/api/projects/{project_id}/search-preparation"
    response = client.post(
        base, json={"sheet_id": sheet, "row_ids": rows, "force_ocr": force_ocr}
    )
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["status"] == "needs_consent"
    assert batch["force_ocr"] is force_ocr
    assert batch["jobs"] == []
    service = SearchPreparationService(workspace)
    service.recover()
    assert client.get(f"{base}/{batch['id']}").json()["status"] == "needs_consent"
    assert service.store.children(batch["id"]) == []
    stale = client.post(
        f"{base}/{batch['id']}/decision",
        json={"plan_hash": "wrong", "decision": "prepare", "selected_setup_refs": []},
    )
    assert stale.status_code == 409
    assert service.store.children(batch["id"]) == []
    decision = {
        "plan_hash": batch["plan_hash"],
        "decision": "prepare",
        "selected_setup_refs": [],
    }
    started = client.post(f"{base}/{batch['id']}/decision", json=decision)
    assert started.status_code == 200, started.text
    [job] = started.json()["jobs"]
    assert job["action_kind"] == "ocr"
    replay = client.post(f"{base}/{batch['id']}/decision", json=decision)
    assert replay.status_code == 200
    assert [job["job_id"] for job in replay.json()["jobs"]] == [job["job_id"]]
    assert len(service.store.children(batch["id"])) == 1


def test_legacy_pdf_action_is_adopted_before_the_review_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )

    async def sample(_path, *, page, **_kwargs):
        return PdfTextSample("", page, 24909, False)

    monkeypatch.setattr(planning_module, "sample_pdf_text", sample)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Prior PDF action")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Documents")
    document = project.add_column(sheet, "document", type="file")
    digest = project.add_blob(b"pdf", mime="application/pdf")
    rows = project.add_rows(
        sheet,
        [{"document": {"blob": digest, "mime": "application/pdf"}}],
        {"document": document},
    )
    service = SearchPreparationService(workspace)
    plan = service._plan(project, sheet_id=sheet, row_ids=rows, force_ocr=False)
    # An upgrade must still adopt an action admitted by the previous process
    # before it could bind the receipt to its reserved child.
    legacy = service.store.create(
        storage_org_id=None,
        project_id=project_id,
        trigger_receipt_id=None,
        sheet_id=sheet,
        row_ids=rows,
        force_ocr=False,
        plan=plan,
    )
    [planned] = preparation_module._planned_children(legacy.plan)
    service.store.reserve_child(
        legacy.id, child_key=planned.child_key, kind=planned.kind
    )
    body = service._action_body(
        project,
        legacy,
        planned.kind,
        planned.column_name,
        planned.items,
        planned.child_key,
    )
    admitted = service.action_runs.run_action(project_id, body)
    assert admitted.payload.get("job_id") is not None, admitted.payload
    service.recover()
    [adopted] = service.store.children(legacy.id)
    assert adopted.receipt_id == admitted.payload["receipt_id"]
    assert adopted.job_id == admitted.payload["job_id"]
    recovered = service.get(project_id, legacy.id)
    assert recovered.status == "preparing"
    assert recovered.jobs[0].job_id == adopted.job_id


def test_pdf_replan_preserves_full_original_cell_scope_and_rejects_late_decisions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )

    async def sample(_path, *, page, **_kwargs):
        return PdfTextSample("embedded text", page, 24909, False)

    monkeypatch.setattr(planning_module, "sample_pdf_text", sample)
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = workspace.create("Mixed review")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Documents")
    document = project.add_column(sheet, "document", type="file")
    image = project.add_column(sheet, "image", type="image")
    digest = project.add_blob(b"pdf", mime="application/pdf")
    image_hash = project.add_blob(b"png", mime="image/png")
    rows = project.add_rows(
        sheet,
        [
            {
                "document": {"blob": digest, "mime": "application/pdf"},
                "image": {"blob": image_hash, "mime": "image/png"},
            }
            for _ in range(10)
        ],
        {"document": document, "image": image},
    )
    base = f"/api/projects/{project_id}/search-preparation"
    service = SearchPreparationService(workspace)
    import_receipt = "receipt:pdf-import"
    original_id = service.create_after_import(
        project_id,
        receipt_id=import_receipt,
        sheet_id=sheet,
        row_ids=rows,
    )
    original = service.get(project_id, original_id).model_dump(
        mode="json", by_alias=True
    )
    assert original["status"] == "needs_consent"
    assert original["jobs"] == []
    # A new source column on the same selected rows must not enter the replacement.
    extra = project.add_column(sheet, "extra", type="file")
    project.apply_edits(
        [
            {
                "row_id": row,
                "column_id": extra,
                "value": {"blob": digest, "mime": "application/pdf"},
            }
            for row in rows
        ]
    )
    replacement_response = client.post(
        f"{base}/{original['id']}/replan",
        json={"plan_hash": original["plan_hash"], "force_ocr": True},
    )
    assert replacement_response.status_code == 200, replacement_response.text
    replacement = replacement_response.json()
    assert replacement["id"] != original["id"]
    assert replacement["force_ocr"] is True
    assert replacement["status"] == "needs_consent"
    assert replacement["jobs"] == []
    assert replacement["groups"] == [
        {"kind": "ocr", "count": 20, "status": "needs_consent"}
    ]
    stored = service.store.get(
        replacement["id"], project_id=project_id, storage_org_id=None
    )
    assert {(item["row_id"], item["column_id"]) for item in stored.plan["items"]} == {
        (row, column) for row in rows for column in (document, image)
    }
    assert client.get(f"{base}/{original['id']}").json()["status"] == "stopped"
    assert service.batch_for_receipt(project_id, import_receipt) == replacement["id"]
    assert (
        client.post(
            f"{base}/{original['id']}/decision",
            json={"plan_hash": original["plan_hash"], "decision": "prepare"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{base}/{replacement['id']}/decision",
            json={"plan_hash": original["plan_hash"], "decision": "prepare"},
        ).status_code
        == 409
    )
    refreshed = service.store.get(
        replacement["id"], project_id=project_id, storage_org_id=None
    )
    assert {
        (item["row_id"], item["column_id"]) for item in refreshed.plan["items"]
    } == {(row, column) for row in rows for column in (document, image)}
    kept_response = client.post(
        f"{base}/{replacement['id']}/replan",
        json={"plan_hash": replacement["plan_hash"], "force_ocr": False},
    )
    assert kept_response.status_code == 200, kept_response.text
    kept = kept_response.json()
    assert kept["force_ocr"] is False
    assert [(group["kind"], group["count"]) for group in kept["groups"]] == [
        ("native_pdf", 10),
        ("ocr", 10),
    ]
    stored_kept = service.store.get(
        kept["id"], project_id=project_id, storage_org_id=None
    )
    child = preparation_module._planned_children(stored_kept.plan)[0]
    service.store.reserve_child(kept["id"], child_key=child.child_key, kind=child.kind)
    assert (
        client.post(
            f"{base}/{kept['id']}/replan",
            json={"plan_hash": kept["plan_hash"], "force_ocr": True},
        ).status_code
        == 409
    )
    assert (
        service.store.get(
            kept["id"], project_id=project_id, storage_org_id=None
        ).stop_requested_at
        is None
    )
    started = client.post(
        f"{base}/{kept['id']}/decision",
        json={
            "plan_hash": kept["plan_hash"],
            "decision": "prepare",
            "selected_setup_refs": [],
        },
    )
    assert started.status_code == 200, started.text
    assert len(started.json()["jobs"]) == 2
    assert service.batch_for_receipt(project_id, import_receipt) == kept["id"]
    assert (
        service.create_after_import(
            project_id, receipt_id=import_receipt, sheet_id=sheet
        )
        == kept["id"]
    )


@pytest.mark.parametrize("repair_skipped_pdf", [False, True])
def test_mixed_pdf_plan_preserves_good_work_and_skipped_source_scope(
    tmp_path, monkeypatch, repair_skipped_pdf
):
    from tests.server.test_server_import_pdf_routes import _pdf_bytes

    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = workspace.create("Mixed PDF preparation")["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/files",
        files=[
            ("files", ("native.pdf", _pdf_bytes("Native beacon"), "application/pdf")),
            ("files", ("scan.pdf", _pdf_bytes(""), "application/pdf")),
            ("files", ("broken.pdf", b"not a PDF", "application/pdf")),
        ],
    )
    assert imported.status_code == 200, imported.text
    sheet = imported.json()["sheet_id"]
    base = f"/api/projects/{project_id}/search-preparation"
    batch = client.get(
        f"{base}/{imported.json()['search_preparation_batch_id']}"
    ).json()
    expected_warning = "Skipped broken.pdf: PDF text preview could not be read."
    assert batch["status"] == "needs_consent"
    assert [(group["kind"], group["count"]) for group in batch["groups"]] == [
        ("native_pdf", 1),
        ("ocr", 1),
    ]
    assert batch["warnings"] == [expected_warning]
    assert batch["jobs"] == []
    service = SearchPreparationService(workspace)
    project = workspace.get(project_id)
    original = service.store.get(
        batch["id"], project_id=project_id, storage_org_id=None
    )
    sources = original.plan["sources"]
    original_cells = {(item["row_id"], item["column_id"]) for item in sources}
    assert len(original_cells) == 3
    broken = next(item for item in sources if item["filename"] == "broken.pdf")
    assert broken["row_id"] not in {item["row_id"] for item in original.plan["items"]}

    # A later source column must not broaden this plan's original cell roster.
    extra = project.add_column(sheet, "Unrelated PDF", type="file")
    project.apply_edits(
        [
            {
                "row_id": broken["row_id"],
                "column_id": extra,
                "value": {"blob": sources[0]["blob_hash"], "mime": "application/pdf"},
            }
        ]
    )
    for force_ocr in (True, False):
        response = client.post(
            f"{base}/{batch['id']}/replan",
            json={"plan_hash": batch["plan_hash"], "force_ocr": force_ocr},
        )
        assert response.status_code == 200, response.text
        batch = response.json()
        assert batch["warnings"] == [expected_warning]
        assert sum(group["count"] for group in batch["groups"]) == 2
        assert batch["status"] == "needs_consent"
        assert batch["jobs"] == []
        stored = service.store.get(
            batch["id"], project_id=project_id, storage_org_id=None
        )
        assert {
            (item["row_id"], item["column_id"]) for item in stored.plan["sources"]
        } == original_cells
        assert set(stored.row_ids) == {row for row, _ in original_cells}

    if repair_skipped_pdf:
        repaired = project.add_blob(
            _pdf_bytes("Repaired beacon"), mime="application/pdf"
        )
        project.apply_edits(
            [
                {
                    "row_id": broken["row_id"],
                    "column_id": broken["column_id"],
                    "value": {
                        "blob": repaired,
                        "filename": "broken.pdf",
                        "mime": "application/pdf",
                    },
                }
            ]
        )
    stale = client.post(
        f"{base}/{batch['id']}/decision",
        json={
            "plan_hash": batch["plan_hash"] if repair_skipped_pdf else "stale",
            "decision": "prepare",
            "selected_setup_refs": [],
        },
    )
    assert stale.status_code == 409, stale.text
    batch = client.get(f"{base}/{batch['id']}").json()
    assert batch["status"] == "needs_consent"
    assert batch["warnings"] == ([] if repair_skipped_pdf else [expected_warning])
    stored = service.store.get(batch["id"], project_id=project_id, storage_org_id=None)
    assert {
        (item["row_id"], item["column_id"]) for item in stored.plan["sources"]
    } == original_cells
    expected_rows = {row for row, _ in original_cells}
    if not repair_skipped_pdf:
        expected_rows.remove(broken["row_id"])
    assert {item["row_id"] for item in stored.plan["items"]} == expected_rows
    assert batch["jobs"] == []
    assert client.get("/api/providers/models/pulls").json()["pulls"] == []
    started = client.post(
        f"{base}/{batch['id']}/decision",
        json={
            "plan_hash": batch["plan_hash"],
            "decision": "prepare",
            "selected_setup_refs": [],
        },
    )
    assert started.status_code == 200, started.text
    assert {job["action_kind"] for job in started.json()["jobs"]} == {
        "native_pdf",
        "ocr",
    }
    assert len(started.json()["jobs"]) == 2
    assert started.json()["warnings"] == batch["warnings"]


def test_pdf_preview_failure_is_readable_http_error(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())

    async def sample(_path, *, page, **_kwargs):
        return PdfTextSample("native", page, 24909, False)

    monkeypatch.setattr(planning_module, "sample_pdf_text", sample)
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = workspace.create("PDF preview")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Documents")
    document = project.add_column(sheet, "document", type="file")
    digest = project.add_blob(b"pdf", mime="application/pdf")
    project.add_rows(
        sheet,
        [{"document": {"blob": digest, "mime": "application/pdf"}}],
        {"document": document},
    )
    base = f"/api/projects/{project_id}/search-preparation"
    created = client.post(base, json={"sheet_id": sheet}).json()
    preview = client.get(
        f"{base}/{created['id']}/text-samples", params={"page_number": 24909}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["samples"][0]["page_count"] == 24909

    async def failed(_path, **_kwargs):
        raise PdfTextError("PDF extraction failed")

    monkeypatch.setattr(planning_module, "sample_pdf_text", failed)
    failed_preview = client.get(f"{base}/{created['id']}/text-samples")
    assert failed_preview.status_code == 422
    assert "preview could not be read" in failed_preview.text
    failed_create = client.post(base, json={"sheet_id": sheet})
    assert failed_create.status_code == 422


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
    # Existing persisted decisions retain replay/recovery semantics; the new
    # HTTP contract no longer admits not_now as a fresh decision.
    first = service.store.decide(
        batch.id,
        project_id=project_id,
        storage_org_id=None,
        plan_hash=batch.plan_hash,
        decision="not_now",
        selected_setup_refs=[],
        decided_by="owner-1",
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


@pytest.mark.parametrize("origin", ["manual", "automatic_replan"])
def test_manual_undecided_batch_does_not_admit_when_engine_becomes_ready(
    tmp_path, monkeypatch, origin
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
    if origin == "manual":
        batch = service.create(
            project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False
        )
    else:
        automatic_id = service.create_after_import(
            project_id, receipt_id="receipt:replan", sheet_id=sheet_id
        )
        automatic = service.get(project_id, automatic_id)
        batch = service.replan(
            project_id, automatic.id, plan_hash=automatic.plan_hash, force_ocr=False
        )
        assert (
            service.store.get(
                batch.id, project_id=project_id, storage_org_id=None
            ).plan["admission_policy"]
            == "manual_review"
        )
    assert service.store.list_active_page(storage_org_id=None)[0].id == batch.id

    requirements[0] = replace(requirements[0], weights_ready=True)
    service.recover()

    assert service.store.children(batch.id) == []
    current = service.get(project_id, batch.id)
    assert current.status == "needs_consent"
    # Readiness changed the displayed facts, so stale approval refreshes the
    # exact plan before it can admit the ordinary action.
    with pytest.raises(preparation_module.RouteError) as stale:
        service.decide(
            project_id,
            batch.id,
            plan_hash=batch.plan_hash,
            decision="prepare",
            selected_setup_refs=[],
            may_setup_models=True,
            initiated_by="owner",
        )
    assert stale.value.status_code == 409
    refreshed = service.get(project_id, batch.id)
    service.decide(
        project_id,
        batch.id,
        plan_hash=refreshed.plan_hash,
        decision="prepare",
        selected_setup_refs=[],
        may_setup_models=True,
        initiated_by="owner",
    )
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


def test_live_action_progress_overrides_a_stale_queued_receipt(
    tmp_path, monkeypatch
) -> None:
    ready = replace(_Requirement(), weights_ready=True)
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (ready,))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Live preparation")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    rows = []
    for number in range(3):
        digest = project.add_blob(
            f"image-{number}".encode(),
            filename=f"scan-{number}.png",
            mime="image/png",
        )
        rows.append(
            {
                "Image": {
                    "blob": digest,
                    "filename": f"scan-{number}.png",
                    "mime": "image/png",
                }
            }
        )
    row_ids = project.add_rows(sheet_id, rows, {"Image": image_column})
    service = SearchPreparationService(workspace)

    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    batch = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[],
        may_setup_models=True,
        initiated_by="owner",
    )
    [child] = service.store.children(batch.id)
    assert child.job_id is not None and child.receipt_id is not None
    stored = ReceiptStore(project).find_by_id(child.receipt_id)
    assert stored is not None and stored.status == "queued"

    claimed = workspace.queue.claim("ocr-worker")
    while claimed is not None and claimed.id != child.job_id:
        assert workspace.queue.complete(
            claimed.id, "ocr-worker", {"skipped_by_test": True}
        )
        claimed = workspace.queue.claim("ocr-worker")
    assert claimed is not None and claimed.id == child.job_id
    run_id = job_run_id(claimed)
    assert run_id is not None
    output_columns = {
        column["name"]: column["id"]
        for column in project.columns(sheet_id, include_hidden=True)
    }
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value_kind,value,error,error_code,outcome,"
        "publication_effect) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            row_ids[0],
            output_columns["Text"],
            None,
            None,
            "OCR found no readable text",
            "empty_output",
            "empty_output",
            "publish_error",
        ),
    )
    project.db.execute(
        "UPDATE runs SET completed_rows=1, failed_rows=1 WHERE id=?", (run_id,)
    )
    project.db.commit()

    active = service.get(project_id, batch.id)

    assert active.groups[0].status == "running"
    assert active.jobs[0].status == "running"
    assert active.jobs[0].progress is not None
    assert active.jobs[0].progress.status == "running"
    assert active.jobs[0].progress.total == 3
    assert active.jobs[0].progress.completed == 1
    assert active.jobs[0].progress.failed == 1
    assert active.jobs[0].progress.row_errors is not None
    [error_group] = active.jobs[0].progress.row_errors.groups
    assert error_group.message == "OCR found no readable text"
    assert error_group.count == 1
    assert error_group.terminal is True
    assert ReceiptStore(project).find_by_id(child.receipt_id).status == "queued"

    project.db.execute(
        "UPDATE runs SET status='completed', completed_rows=3, failed_rows=1, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (run_id,),
    )
    project.db.commit()

    terminal = service.get(project_id, batch.id)

    assert terminal.status == "failed"
    assert terminal.groups[0].status == "failed"
    assert terminal.jobs[0].status == "failed"
    assert terminal.jobs[0].progress is not None
    assert terminal.jobs[0].progress.status == "completed"
    assert terminal.jobs[0].progress.total == 3
    assert terminal.jobs[0].progress.completed == 3
    assert terminal.jobs[0].progress.failed == 1
    assert ReceiptStore(project).find_by_id(child.receipt_id).status == "queued"


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

    class ReservationInProgress(preparation_module.ActionRunService):
        def run_action(self, _project_id, _body, **_kwargs):
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

    service = SearchPreparationService(
        workspace, action_runs=ReservationInProgress(workspace)
    )
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    batch = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[],
        may_setup_models=True,
        initiated_by="owner",
    )

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
            "indexing": {
                "status": "idle",
                "job_id": None,
                "error": None,
                "pdf_files": [],
                "pdf_files_truncated": False,
            },
        }
    assert workspace.queue.counts()["queued"] == 0

    for mode in ("lexical", "semantic", "hybrid"):
        searched = client.post(
            f"/api/projects/{project_id}/search",
            json={
                "scope": {"kind": "project"},
                "query": {"kind": "text", "text": "content", "lexical_syntax": "plain"},
                "mode": mode,
                "group_by": "source",
                "limit": 10,
                "ranking": {"rerank": "off"},
            },
        )
        assert searched.status_code == 200, searched.text
        assert searched.json()["coverage"]["state"] == "complete"
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


def test_automatic_ocr_keeps_local_target_with_cloud_first_account(
    tmp_path, monkeypatch
):
    from frisket.cloud_connection import CloudConnectionStore
    from frisket.engine.store.execution_routes import RouteStore
    from frisket.project_settings import ProjectSettings, write_project_settings
    from tests.server.test_cloud_connection import _session, _capabilities

    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )
    monkeypatch.setattr(
        "frisket.ops.ocr_engines.rapidocr_available", lambda: (True, None)
    )
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection("test-token", _session())

    class Client:
        def capabilities(self, _token, account_id):
            return _capabilities(account_id=account_id)

        def execute(self, *_args, **_kwargs):
            raise AssertionError("automatic local preparation must not use Cloud")

    workspace = Workspace(
        tmp_path / "workspace",
        cloud_connection_store=store,
        cloud_connection_client=Client(),
    )
    project_id = workspace.create("Local automatic OCR")["id"]
    project = workspace.get(project_id)
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    sheet = project.add_sheet("Files")
    column = project.add_column(sheet, "Source", type="image")
    digest = project.add_blob(b"media", mime="image/png")
    project.add_rows(
        sheet, [{"Source": {"blob": digest, "mime": "image/png"}}], {"Source": column}
    )
    service = SearchPreparationService(workspace)
    batch_id = service.create_after_import(
        project_id, receipt_id="receipt:local-only", sheet_id=sheet
    )
    [child] = service.store.children(batch_id)
    stored = ReceiptStore(project).find_by_id(child.receipt_id)
    route, _promises = RouteStore.for_run(project, stored.run_id).head()
    assert route.target_snapshot["target_id"] == "local"
    assert len(service.get(project_id, batch_id).jobs) == 1


def test_bulk_callback_replay_ignores_unrelated_project_cursor_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Bulk callback retry")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Files")
    column = project.add_column(sheet, "Source", type="image")
    digest = project.add_blob(b"media", mime="image/png")
    rows = project.add_rows(
        sheet, [{"Source": {"blob": digest, "mime": "image/png"}}], {"Source": column}
    )
    service = SearchPreparationService(workspace)
    batch_id = service.create_after_materialization(
        project_id, sheet_id=sheet, row_ids=rows
    )
    [original] = service.store.children(batch_id)
    project.add_column(sheet, "Unrelated note", type="text")
    replay = SearchPreparationService(workspace).create_after_materialization(
        project_id, sheet_id=sheet, row_ids=rows
    )
    assert replay == batch_id
    assert service.store.children(batch_id) == [original]
    assert [
        job.id
        for job in workspace.queue.list_project_jobs(
            project_id, kind="project.run", limit=20
        )
    ] == [original.job_id]


def test_automatic_plan_refuses_source_rebound_during_planning(tmp_path, monkeypatch):
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (replace(_Requirement(), weights_ready=True),),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Changed during preview")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Files")
    column = project.add_column(sheet, "Source", type="image")
    digest = project.add_blob(b"original", mime="image/png")
    replacement = project.add_blob(b"replacement", mime="image/png")
    [row] = project.add_rows(
        sheet, [{"Source": {"blob": digest, "mime": "image/png"}}], {"Source": column}
    )
    service = SearchPreparationService(workspace)
    original_plan = service._plan

    def changed_while_previewing(*args, **kwargs):
        # A user edit while an earlier file's preview was running means the
        # normal planner now sees a different binding in the same scoped cell.
        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": column,
                    "value": {"blob": replacement, "mime": "image/png"},
                }
            ]
        )
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(service, "_plan", changed_while_previewing)
    batch_id = service.create_after_import(
        project_id, receipt_id="receipt:changed-mid-plan", sheet_id=sheet
    )
    failed = service.get(project_id, batch_id)
    assert failed.status == "failed"
    assert any("source file changed" in error for error in failed.errors)
    assert failed.jobs == []
    assert service.store.children(batch_id) == []
    SearchPreparationService(workspace).recover()
    assert service.get(project_id, batch_id) == failed
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )


def test_import_handoff_projects_durable_pending_available_and_none(
    tmp_path, monkeypatch
):
    from frisket.project_settings import patch_project_settings

    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Preparation handoff")["id"]
    service = SearchPreparationService(workspace)
    assert service.handoff_for_receipt(project_id, "receipt:absent") == ("none", None)
    pending = service.store.create_pending_import(
        storage_org_id=None,
        project_id=project_id,
        receipt_id="receipt:pending",
        import_ref="session:pending",
    )
    assert service.handoff_for_receipt(project_id, "receipt:pending") == (
        "pending",
        None,
    )
    project = workspace.get(project_id)
    patch_project_settings(project, {"prepare_new_files_automatically": False})
    assert service.handoff_for_receipt(project_id, "receipt:pending") == ("none", None)
    patch_project_settings(project, {"prepare_new_files_automatically": True})
    service.store.mark_settled(pending.id, project_id=project_id, storage_org_id=None)
    assert service.handoff_for_receipt(project_id, "receipt:pending") == ("none", None)
    sheet = project.add_sheet("Files")
    failed = service.store.create(
        storage_org_id=None,
        project_id=project_id,
        trigger_receipt_id="receipt:failed",
        sheet_id=sheet,
        row_ids=[],
        force_ocr=False,
        plan={
            "source_cutoff": project.op_cursor,
            "items": [],
            "models": [],
            "groups": [],
        },
        error_message="Preview could not be read",
    )
    patch_project_settings(project, {"prepare_new_files_automatically": False})
    assert service.handoff_for_receipt(project_id, "receipt:failed") == (
        "available",
        failed.id,
    )
    # The pre-existing helper retains its hydrated-only identity semantics.
    assert service.batch_for_receipt(project_id, "receipt:failed") == failed.id
    assert service.batch_for_receipt(project_id, "receipt:pending") is None


@pytest.mark.parametrize("sibling_page", [None, 1])
def test_pdf_sample_selection_is_bound_to_column_before_sampling(
    tmp_path, monkeypatch, sibling_page
):
    from tests.engine.test_native_pdf_text_action import _pdf_bytes

    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    workspace = client.app.state.workspace
    project_id = workspace.create("Two PDFs in one row")["id"]
    project = workspace.get(project_id)
    sheet = project.add_sheet("Documents")
    long_column = project.add_column(sheet, "Long PDF", type="file")
    short_column = project.add_column(sheet, "Short PDF", type="file")
    foreign_column = project.add_column(sheet, "Notes", type="text")
    long_pdf = project.add_blob(
        _pdf_bytes(*(f"Long document page {n}" for n in range(1, 13))),
        filename="long.pdf",
        mime="application/pdf",
    )
    short_pdf = project.add_blob(
        _pdf_bytes("Short document page 1"),
        filename="short.pdf",
        mime="application/pdf",
    )
    sibling = {"blob": short_pdf, "mime": "application/pdf", "filename": "short.pdf"}
    if sibling_page is not None:
        sibling["page"] = sibling_page
    [row] = project.add_rows(
        sheet,
        [
            {
                "Long PDF": {
                    "blob": long_pdf,
                    "mime": "application/pdf",
                    "filename": "long.pdf",
                },
                "Short PDF": sibling,
            }
        ],
        {"Long PDF": long_column, "Short PDF": short_column},
    )
    base = f"/api/projects/{project_id}/search-preparation"
    created = client.post(base, json={"sheet_id": sheet})
    assert created.status_code == 200, created.text
    samples_url = f"{base}/{created.json()['id']}/text-samples"
    legacy = client.get(samples_url, params={"row_id": row, "page_number": 1})
    assert legacy.status_code == 200, legacy.text
    assert {item["column_id"] for item in legacy.json()["samples"]} == {
        long_column,
        short_column,
    }
    selected = client.get(
        samples_url, params={"row_id": row, "column_id": long_column, "page_number": 10}
    )
    assert selected.status_code == 200, selected.text
    [sample] = selected.json()["samples"]
    assert (
        sample["row_id"],
        sample["column_id"],
        sample["page_number"],
        sample["page_count"],
    ) == (row, long_column, 10, 12)
    assert "Long document page 10" in sample["text"]
    sibling_sample = client.get(
        samples_url, params={"row_id": row, "column_id": short_column, "page_number": 1}
    )
    assert sibling_sample.status_code == 200, sibling_sample.text
    assert [item["column_id"] for item in sibling_sample.json()["samples"]] == [
        short_column
    ]
    outside = client.get(
        samples_url,
        params={"row_id": row, "column_id": foreign_column, "page_number": 1},
    )
    assert outside.status_code == 404
    assert (
        workspace.queue.list_project_jobs(project_id, kind="project.run", limit=20)
        == []
    )

from __future__ import annotations

import hashlib
import io
import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from frisket.server.services.pdf_import_commit import PdfImportCommitService
from frisket.server.services.pdf_imports import PdfImportService
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.import_sessions import ImportSessionService
from frisket.server.services.action_runs import ActionRunResponse
from frisket.server.services.search_preparation import SearchPreparationService
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace
from frisket.engine.jobs.import_files import register_import_files_handler
from frisket.engine.jobs.queue import IMPORT_FILES_PAGE_KIND
from frisket.engine.jobs.queue import PROJECT_RUN_KIND
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store.import_inventory import ImportInventory
from frisket.server.services.pdf_import_preparation import PdfImportPreparationStore


@dataclass(frozen=True)
class _Occurrence:
    ordinal: int
    occurrence_id: str
    logical_path: str
    filename: str
    size: int
    sha256: str | None
    path: Path | None
    page_count: int | None
    category: str
    error: str | None = None
    matched: bool = False
    ocr_selected: bool = False


@dataclass
class _Frozen:
    commit_id: str
    draft_id: str
    project_id: str
    storage_identity: str
    idempotency_key: str
    selected_through: int
    selected_total: int
    staged_through: int
    manifest_hash: str
    destination: dict[str, Any]
    ocr_policy: str
    ocr_engine: str
    ocr_action_params: dict[str, Any]
    ocr_authorization: dict[str, Any] | None
    semantic_search: bool
    semantic_setup_confirmation: str | None
    directory: Path
    commit_inventory_path: Path
    committed_total: int
    available_through: int
    uploads_complete: bool
    sealed: bool
    occurrences: list[_Occurrence]
    commit_result: dict[str, Any] | None = None

    def page_occurrences(self, *, after: int = 0, limit: int = 256):
        return [
            item
            for item in self.occurrences
            if after < item.ordinal <= self.available_through
        ][:limit]


class _Intake:
    def __init__(self, frozen: _Frozen) -> None:
        self.frozen = frozen
        self.results: list[dict[str, Any]] = []

    def freeze_for_commit(self, project_id, draft_id, **_kwargs):
        assert project_id == self.frozen.project_id
        assert draft_id == self.frozen.draft_id
        with ImportInventory(self.frozen.commit_inventory_path) as inventory:
            after = inventory.through
            ready = []
            for item in [
                value for value in self.frozen.occurrences if value.ordinal > after
            ]:
                if item.path is None or item.sha256 is None:
                    break
                ready.append(
                    {
                        "logical_path": item.logical_path,
                        "mime": "application/pdf",
                        "sha256": item.sha256,
                        "size": item.size,
                        "kind": "files",
                    }
                )
            if ready:
                inventory.append(ready)
            complete = inventory.through == self.frozen.committed_total
            if complete and not inventory.sealed:
                inventory.seal()
            self.frozen.available_through = inventory.through
            self.frozen.sealed = inventory.sealed
            self.frozen.uploads_complete = complete
        return self.frozen

    def reconcile_commit(self, _project_id, _draft_id, **kwargs):
        self.results.append(kwargs)

    def record_commit_handoff(self, _project_id, _draft_id, **kwargs):
        value = dict(kwargs["result"])
        if self.frozen.commit_result is not None:
            assert self.frozen.commit_result == value
        self.frozen.commit_result = value

    def promote_commit_inventory(
        self, _project_id, _draft_id, *, idempotency_key, target_path
    ):
        assert idempotency_key == self.frozen.idempotency_key
        target_path.parent.mkdir(parents=True, exist_ok=True)
        (target_path.parent / "staged").mkdir(exist_ok=True)
        with ImportInventory(target_path) as inventory:
            inventory._db.execute(
                "CREATE TABLE IF NOT EXISTS pdf_commit_occurrences ("
                "ordinal INTEGER PRIMARY KEY,occurrence_id TEXT NOT NULL,"
                "selected_ordinal INTEGER NOT NULL,category TEXT NOT NULL,"
                "error TEXT,ocr_selected INTEGER NOT NULL)"
            )
            after = inventory.through
            ready = []
            metadata = []
            for item in self.frozen.occurrences[after:]:
                if item.path is None or item.sha256 is None:
                    break
                shutil.copyfile(item.path, target_path.parent / "staged" / item.sha256)
                ready.append(
                    {
                        "logical_path": item.logical_path,
                        "mime": "application/pdf",
                        "sha256": item.sha256,
                        "size": item.size,
                        "kind": "files",
                    }
                )
                metadata.append(item)
            if ready:
                first, _last = inventory.append(ready)
                inventory._db.executemany(
                    "INSERT OR IGNORE INTO pdf_commit_occurrences "
                    "(ordinal,occurrence_id,selected_ordinal,category,error,ocr_selected) "
                    "VALUES (?,?,?,?,?,?)",
                    [
                        (
                            first + offset,
                            item.occurrence_id,
                            item.ordinal,
                            item.category,
                            item.error,
                            int(item.ocr_selected),
                        )
                        for offset, item in enumerate(metadata)
                    ],
                )
                inventory._db.commit()
            if inventory.through == self.frozen.committed_total:
                inventory.seal()


class _TextPreparation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def progress_pdf_import(self, project_id, import_ref):
        value = {"project_id": project_id, "import_ref": import_ref}
        if value not in self.calls:
            self.calls.append(value)

    def reserve_pdf_import(self, _project_id, **_kwargs):
        return None


def _staged(root: Path, ordinal: int, name: str, *, category: str) -> _Occurrence:
    payload = f"PDF {ordinal}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = root / f"{ordinal}.pdf"
    path.write_bytes(payload)
    return _Occurrence(
        ordinal=ordinal,
        occurrence_id=f"occurrence-{ordinal}",
        logical_path=name,
        filename=name,
        size=len(payload),
        sha256=digest,
        path=path,
        page_count=1,
        category=category,
    )


def _workspace(tmp_path: Path) -> tuple[Workspace, str]:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("PDF execution", project_id="pdf-exec")["id"])
    return workspace, project_id


def test_real_draft_promotes_into_ordinary_import_and_undo(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id = _workspace(tmp_path)
    intake = PdfImportService(workspace)
    monkeypatch.setattr(
        intake,
        "_analyze",
        lambda _path: {
            "analysis_state": "done",
            "page_count": 1,
            "category": "text",
            "sample_pages": [1],
            "error": None,
        },
    )
    payloads = [b"%PDF-real-commit-one", b"%PDF-real-commit-two"]
    created = intake.create(
        project_id,
        expected_files=2,
        expected_bytes=sum(map(len, payloads)),
        source_label="Evidence",
    )
    draft_id = str(created["draft_id"])
    intake.append_manifest(
        project_id,
        draft_id,
        batch_id="manifest",
        files=[
            {
                "client_file_id": "one",
                "logical_path": "one.pdf",
                "filename": "one.pdf",
                "size": len(payloads[0]),
            },
            {
                "client_file_id": "two",
                "logical_path": "two.pdf",
                "filename": "two.pdf",
                "size": len(payloads[1]),
            },
        ],
    )
    intake.seal_manifest(project_id, draft_id)
    uploaded = intake.upload(
        project_id,
        draft_id,
        [
            AdmittedUpload(
                filename="one.pdf",
                mime="application/pdf",
                source=io.BytesIO(payloads[0]),
                sha256=hashlib.sha256(payloads[0]).hexdigest(),
                size=len(payloads[0]),
            ),
            AdmittedUpload(
                filename="two.pdf",
                mime="application/pdf",
                source=io.BytesIO(payloads[1]),
                sha256=hashlib.sha256(payloads[1]).hexdigest(),
                size=len(payloads[1]),
            ),
        ],
        file_ids=["one", "two"],
        logical_paths=["one.pdf", "two.pdf"],
        batch_id="upload",
    )
    reviewed = intake.update_choices(
        project_id,
        draft_id,
        expected_revision=uploaded["revision"],
        ocr_policy="none",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "Evidence PDFs"},
    )

    result = PdfImportCommitService(workspace, intake=intake).commit(
        project_id,
        draft_id,
        idempotency_key="commit-real",
        expected_revision=reviewed["revision"],
    )

    project = workspace.get(project_id)
    assert result.state == "completed"
    assert project.row_count(result.sheet_id) == 2
    directory = project.path / ".imports" / result.import_ref
    assert (directory / "inventory.db").is_file()
    assert not (directory / "staged").exists()
    assert project.undo() is not None
    assert project.sheets() == []


def test_commit_appends_only_unmatched_rows_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Evidence")
    columns = {
        "filename": project.add_column(sheet_id, "filename"),
        "media": project.add_column(sheet_id, "media", type="file"),
        "size": project.add_column(sheet_id, "size", type="integer"),
    }
    project.add_rows(
        sheet_id,
        [{"filename": "same.pdf", "size": 5}],
        columns,
    )
    draft = tmp_path / "draft"
    draft.mkdir()
    added = _staged(draft, 2, "new.pdf", category="missing")
    added = replace(added, ordinal=1, ocr_selected=True)
    frozen = _Frozen(
        commit_id="pdf-commit-one",
        draft_id="draft-one",
        project_id=project_id,
        storage_identity=project.storage_identity,
        idempotency_key="commit-once",
        selected_through=2,
        selected_total=2,
        staged_through=2,
        manifest_hash="sha256:frozen-one",
        destination={"kind": "existing", "sheet_id": sheet_id},
        ocr_policy="missing_or_suspicious",
        ocr_engine="rapidocr",
        ocr_action_params={"engine": "rapidocr"},
        ocr_authorization=None,
        semantic_search=False,
        semantic_setup_confirmation=None,
        directory=draft,
        commit_inventory_path=draft / "commit-inventory.db",
        committed_total=1,
        available_through=0,
        uploads_complete=False,
        sealed=False,
        occurrences=[added],
    )
    intake = _Intake(frozen)
    text = _TextPreparation()
    service = PdfImportCommitService(workspace, intake=intake, text_preparation=text)

    first = service.commit(
        project_id, "draft-one", idempotency_key="commit-once", expected_revision=7
    )
    replay = service.commit(
        project_id, "draft-one", idempotency_key="commit-once", expected_revision=7
    )

    assert first == replay
    assert first.state == "completed"
    assert first.committed_rows == 1
    assert project.row_count(sheet_id) == 2
    assert len(text.calls) == 1
    assert text.calls[0]["import_ref"] == first.import_ref
    assert (
        project.undo()
        == project.db.execute(
            "SELECT op_id FROM import_sessions WHERE id=?", (first.import_ref,)
        ).fetchone()[0]
    )
    assert project.row_count(sheet_id) == 1


def test_local_commit_opens_sheet_then_resumes_pending_upload_without_duplicates(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    project = workspace.get(project_id)
    draft = tmp_path / "draft-pending"
    draft.mkdir()
    first = _staged(draft, 1, "one.pdf", category="text")
    pending = _Occurrence(
        ordinal=2,
        occurrence_id="occurrence-2",
        logical_path="two.pdf",
        filename="two.pdf",
        size=5,
        sha256=None,
        path=None,
        page_count=None,
        category="unchecked",
    )
    frozen = _Frozen(
        commit_id="pdf-commit-pending",
        draft_id="draft-pending",
        project_id=project_id,
        storage_identity=project.storage_identity,
        idempotency_key="commit-pending",
        selected_through=2,
        selected_total=2,
        staged_through=1,
        manifest_hash="sha256:frozen-pending",
        destination={"kind": "new", "sheet_name": "PDFs"},
        ocr_policy="none",
        ocr_engine="rapidocr",
        ocr_action_params={"engine": "rapidocr"},
        ocr_authorization=None,
        semantic_search=True,
        semantic_setup_confirmation="semantic-consent",
        directory=draft,
        commit_inventory_path=draft / "commit-inventory.db",
        committed_total=2,
        available_through=0,
        uploads_complete=False,
        sealed=False,
        occurrences=[first, pending],
    )
    intake = _Intake(frozen)
    service = PdfImportCommitService(
        workspace, intake=intake, text_preparation=_TextPreparation()
    )

    opened = service.commit(
        project_id,
        "draft-pending",
        idempotency_key="commit-pending",
        expected_revision=3,
    )
    assert opened.state == "waiting_for_upload"
    assert opened.committed_rows == 1
    assert project.sheets()[0]["name"] == "PDFs"
    assert frozen.commit_result == {
        "import_ref": opened.import_ref,
        "sheet_id": opened.sheet_id,
        "receipt_id": opened.receipt_id,
    }

    staged_second = _staged(draft, 2, "two.pdf", category="missing")
    frozen.occurrences[1] = staged_second
    frozen.staged_through = 2
    finished = service.commit(
        project_id,
        "draft-pending",
        idempotency_key="commit-pending",
        expected_revision=3,
    )
    assert finished.state == "completed"
    assert finished.committed_rows == 2
    assert project.row_count(finished.sheet_id) == 2
    assert len(intake.results) == 1


class _ActionRuns:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.priorities: list[str | None] = []

    def run_action(
        self,
        _project_id: str,
        body: dict[str, Any],
        *,
        background_priority: str | None = None,
    ):
        self.bodies.append(body)
        self.priorities.append(background_priority)
        return ActionRunResponse(
            status_code=202,
            payload={"status": "queued", "receipt_id": f"text-{len(self.bodies)}"},
        )


class _BusyThenReadyActions(_ActionRuns):
    def __init__(self) -> None:
        super().__init__()
        self.ocr_busy = True

    def run_action(
        self,
        _project_id: str,
        body: dict[str, Any],
        *,
        background_priority: str | None = None,
    ):
        self.bodies.append(body)
        self.priorities.append(background_priority)
        if body["action_id"] == "media.ocr" and self.ocr_busy:
            return ActionRunResponse(
                status_code=409,
                payload={
                    "status": "failed",
                    "errors": [
                        {
                            "code": "output_column_busy",
                            "message": "Text is still owned by native extraction",
                        }
                    ],
                },
            )
        return ActionRunResponse(
            status_code=202,
            payload={
                "status": "queued",
                "receipt_id": f"text-{body['action_id']}",
            },
        )


def test_reviewed_text_handoff_uses_one_designated_column_and_action_authority(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Documents")
    project.add_column(sheet_id, "filename")
    project.add_column(sheet_id, "media", type="file")
    rows = project.add_rows(
        sheet_id,
        [
            {"filename": "native.pdf"},
            {"filename": "scan.pdf"},
            {"filename": "broken.pdf"},
        ],
        {str(c["name"]): int(c["id"]) for c in project.columns(sheet_id)},
    )
    actions = _ActionRuns()
    preparation = SearchPreparationService(workspace, action_runs=actions)

    preparation.create_from_pdf_import(
        project_id,
        import_receipt_id="receipt:pdf-import",
        sheet_id=sheet_id,
        rows=[
            {
                "row_id": rows[0],
                "occurrence_id": "native",
                "category": "text",
                "ocr_selected": False,
            },
            {
                "row_id": rows[1],
                "occurrence_id": "scan",
                "category": "missing",
                "ocr_selected": True,
            },
            {
                "row_id": rows[2],
                "occurrence_id": "broken",
                "category": "unreadable",
                "error": "PDF xref could not be read",
                "ocr_selected": True,
            },
        ],
        ocr_policy="missing_or_suspicious",
        ocr_action_params={"engine": "rapidocr", "model": "document-v2"},
        ocr_authorization={"estimate": {"claims": []}},
        semantic_search=False,
        semantic_setup_confirmation=None,
    )

    assert [body["action_id"] for body in actions.bodies] == [
        "media.read_pdf_text",
        "media.ocr",
    ]
    assert actions.bodies[0]["scope"]["row_ids"] == [rows[0], rows[2]]
    assert actions.bodies[1]["scope"]["row_ids"] == [rows[1]]
    assert actions.bodies[1]["params"]["engine"] == "rapidocr"
    assert actions.bodies[1]["params"]["model"] == "document-v2"
    assert actions.priorities == [None, "pdf_import_ocr"]
    assert {body["output_names"]["text"] for body in actions.bodies} == {"text"}
    assert [str(column["name"]) for column in project.columns(sheet_id)].count(
        "text"
    ) == 1
    text_column = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if str(column["name"]) == "text"
    )
    assert (
        project.db.execute(
            "SELECT 1 FROM cells WHERE row_id=? AND column_id=?",
            (rows[2], text_column),
        ).fetchone()
        is None
    )


def test_reviewed_semantic_choice_starts_existing_setup_with_frozen_consent(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id = _workspace(tmp_path)
    started: list[tuple[str, str | None]] = []
    preparation = SearchPreparationService(
        workspace, setup_start=lambda ref, owner: started.append((ref, owner)) or 1
    )
    requirement = SimpleNamespace(
        setup_ref="engine-setup:embeddinggemma-2.local@1",
        display_name="Search by meaning",
        runtime_ready=False,
        weights_ready=False,
        runtime_installable=True,
        download_bytes_if_missing=1234,
        license="Apache-2.0",
        source_url="https://example.test/model",
    )
    monkeypatch.setattr(
        preparation,
        "_setup_snapshot",
        lambda: SimpleNamespace(
            requirements_by_ref={requirement.setup_ref: requirement}
        ),
    )
    binding = {
        "setup_ref": requirement.setup_ref,
        "display_name": requirement.display_name,
        "runtime_ready": False,
        "download_bytes": 1234,
        "additional_runtime_download": True,
        "license": requirement.license,
        "source_url": requirement.source_url,
        "available": True,
    }
    confirmation = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    try:
        preparation._ensure_pdf_semantic_setup(
            project_id,
            import_receipt_id="receipt:pdf-semantic",
            enabled=True,
            confirmation="stale-review",
        )
    except RouteError as exc:
        assert exc.status_code == 409
    else:  # pragma: no cover - regression assertion
        raise AssertionError("stale setup consent was accepted")

    preparation._ensure_pdf_semantic_setup(
        project_id,
        import_receipt_id="receipt:pdf-semantic",
        enabled=True,
        confirmation=confirmation,
    )

    assert started == [
        (
            "engine-setup:embeddinggemma-2.local@1",
            "pdf-import:receipt:pdf-semantic",
        )
    ]


def test_mixed_native_and_ocr_page_serializes_on_shared_text_claim(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    draft = tmp_path / "draft-mixed"
    draft.mkdir()
    native = _staged(draft, 1, "native.pdf", category="text")
    scan = replace(_staged(draft, 2, "scan.pdf", category="missing"), ocr_selected=True)
    frozen = _Frozen(
        commit_id="pdf-commit-mixed",
        draft_id="draft-mixed",
        project_id=project_id,
        storage_identity=workspace.get(project_id).storage_identity,
        idempotency_key="commit-mixed",
        selected_through=2,
        selected_total=2,
        staged_through=2,
        manifest_hash="sha256:frozen-mixed",
        destination={"kind": "new", "sheet_name": "Mixed PDFs"},
        ocr_policy="missing_or_suspicious",
        ocr_engine="rapidocr",
        ocr_action_params={"engine": "rapidocr"},
        ocr_authorization=None,
        semantic_search=False,
        semantic_setup_confirmation=None,
        directory=draft,
        commit_inventory_path=draft / "commit-inventory.db",
        committed_total=2,
        available_through=0,
        uploads_complete=False,
        sealed=False,
        occurrences=[native, scan],
    )
    actions = _BusyThenReadyActions()
    preparation = SearchPreparationService(workspace, action_runs=actions)
    result = PdfImportCommitService(
        workspace, intake=_Intake(frozen), text_preparation=preparation
    ).commit(
        project_id,
        frozen.draft_id,
        idempotency_key=frozen.idempotency_key,
        expected_revision=1,
    )

    assert [body["action_id"] for body in actions.bodies] == [
        "media.read_pdf_text",
        "media.ocr",
    ]
    first = preparation.pdf_import_summary(project_id, result.import_ref)
    assert first.scheduled_through == 0
    assert [child["action_id"] for child in first.children] == ["media.read_pdf_text"]
    ImportSessionService(workspace, search_preparation=preparation).status(
        project_id, result.import_ref
    )
    session = (
        workspace.get(project_id)
        .db.execute(
            "SELECT receipt_id FROM import_sessions WHERE id=?", (result.import_ref,)
        )
        .fetchone()
    )
    preparation.create_after_import(
        project_id,
        receipt_id=str(session["receipt_id"]),
        sheet_id=result.sheet_id,
    )

    actions.ocr_busy = False
    preparation.recover()
    final = preparation.pdf_import_summary(project_id, result.import_ref)
    assert final.scheduled_through == 2
    assert [child["action_id"] for child in final.children] == [
        "media.read_pdf_text",
        "media.ocr",
    ]


def test_stop_completed_import_keeps_rows_and_terminates_pending_preparation(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    draft = tmp_path / "draft-stop"
    draft.mkdir()
    scan = replace(_staged(draft, 1, "scan.pdf", category="missing"), ocr_selected=True)
    frozen = _Frozen(
        commit_id="pdf-commit-stop",
        draft_id="draft-stop",
        project_id=project_id,
        storage_identity=workspace.get(project_id).storage_identity,
        idempotency_key="commit-stop",
        selected_through=1,
        selected_total=1,
        staged_through=1,
        manifest_hash="sha256:frozen-stop",
        destination={"kind": "new", "sheet_name": "Stopped PDFs"},
        ocr_policy="missing_or_suspicious",
        ocr_engine="rapidocr",
        ocr_action_params={"engine": "rapidocr"},
        ocr_authorization=None,
        semantic_search=False,
        semantic_setup_confirmation=None,
        directory=draft,
        commit_inventory_path=draft / "commit-inventory.db",
        committed_total=1,
        available_through=0,
        uploads_complete=False,
        sealed=False,
        occurrences=[scan],
    )
    actions = _BusyThenReadyActions()
    preparation = SearchPreparationService(workspace, action_runs=actions)
    result = PdfImportCommitService(
        workspace, intake=_Intake(frozen), text_preparation=preparation
    ).commit(
        project_id,
        frozen.draft_id,
        idempotency_key=frozen.idempotency_key,
        expected_revision=1,
    )
    queued_jobs = [
        workspace.queue.enqueue(
            PROJECT_RUN_KIND,
            {
                "project_id": project_id,
                "run_id": 999 + offset,
                "action_kind": "media.ocr",
            },
        )
        for offset in range(105)
    ]
    inventory_path = (
        workspace.get(project_id).path / ".imports" / result.import_ref / "inventory.db"
    )
    with PdfImportPreparationStore(inventory_path) as store:
        for offset, job_id in enumerate(queued_jobs):
            store.record_child(
                child_key=f"pending-ocr-{offset}",
                action_id="media.ocr",
                row_count=1,
                response={"status": "queued", "job_id": job_id},
            )

    assert (
        len(preparation.pdf_import_summary(project_id, result.import_ref).children)
        == 100
    )

    stopped = ImportSessionService(workspace, search_preparation=preparation).cancel(
        project_id, result.import_ref
    )
    assert stopped.state == "kept"
    assert stopped.preparation_stopped
    assert workspace.get(project_id).row_count(result.sheet_id) == 1
    assert all(
        workspace.queue.get(job_id).status == "cancelled" for job_id in queued_jobs
    )
    before = len(actions.bodies)
    actions.ocr_busy = False
    preparation.recover()
    assert len(actions.bodies) == before
    pending = preparation.store.for_receipt(
        storage_org_id=workspace.queue_storage_org_id,
        project_id=project_id,
        receipt_id=result.receipt_id,
        include_pending=True,
    )
    assert pending is not None
    assert pending.settled_at is not None


def test_reviewed_text_handoff_refuses_to_reinterpret_paid_draft_consent(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Documents")
    project.add_column(sheet_id, "media", type="file")
    preparation = SearchPreparationService(workspace, action_runs=_ActionRuns())

    try:
        preparation.create_from_pdf_import(
            project_id,
            import_receipt_id="receipt:pdf-import",
            sheet_id=sheet_id,
            rows=[],
            ocr_policy="all",
            ocr_authorization={
                "estimate": {"claims": [{"field": "cost", "display": "$1"}]},
                "confirmation": "draft-bound-token",
                "admission": {"attempt_id": "draft-attempt"},
            },
            semantic_search=False,
            semantic_setup_confirmation=None,
            ocr_action_params={"engine": "remote-ocr"},
        )
    except RouteError as exc:
        assert exc.status_code == 503
    else:  # pragma: no cover - the safety boundary must fail closed
        raise AssertionError("paid draft consent was treated as row-action consent")


def test_reviewed_text_recovery_continues_after_server_restart(tmp_path: Path) -> None:
    workspace, project_id = _workspace(tmp_path)
    project = workspace.get(project_id)
    draft = tmp_path / "draft-recovery"
    draft.mkdir()
    first = replace(
        _staged(draft, 1, "unreadable.pdf", category="unreadable"),
        error="PDF xref could not be read",
        ocr_selected=True,
    )
    pending = _Occurrence(
        ordinal=2,
        occurrence_id="occurrence-2",
        logical_path="scan.pdf",
        filename="scan.pdf",
        size=5,
        sha256=None,
        path=None,
        page_count=None,
        category="unchecked",
    )
    frozen = _Frozen(
        commit_id="pdf-commit-recovery",
        draft_id="draft-recovery",
        project_id=project_id,
        storage_identity=project.storage_identity,
        idempotency_key="commit-recovery",
        selected_through=2,
        selected_total=2,
        staged_through=1,
        manifest_hash="sha256:frozen-recovery",
        destination={"kind": "new", "sheet_name": "Recovered PDFs"},
        ocr_policy="missing_or_suspicious",
        ocr_engine="rapidocr",
        ocr_action_params={"engine": "rapidocr"},
        ocr_authorization=None,
        semantic_search=False,
        semantic_setup_confirmation=None,
        directory=draft,
        commit_inventory_path=draft / "commit-inventory.db",
        committed_total=2,
        available_through=0,
        uploads_complete=False,
        sealed=False,
        occurrences=[first, pending],
    )
    intake = _Intake(frozen)
    actions = _ActionRuns()
    preparation = SearchPreparationService(workspace, action_runs=actions)
    PdfImportCommitService(
        workspace, intake=intake, text_preparation=preparation
    ).commit(
        project_id,
        "draft-recovery",
        idempotency_key="commit-recovery",
        expected_revision=1,
    )
    assert [body["action_id"] for body in actions.bodies] == ["media.read_pdf_text"]
    opened_ref = PdfImportCommitService._ref(frozen.commit_id)
    status = ImportSessionService(workspace, search_preparation=preparation).status(
        project_id, opened_ref
    )
    assert status.selected_total == 2
    assert status.preparation_total == 1
    assert status.unreadable_total == 1
    assert status.row_error_summary is not None
    assert status.row_error_summary.model_dump(mode="json") == {
        "total_failed_rows": 1,
        "groups": [
            {
                "message": "PDF xref could not be read",
                "count": 1,
                "code": "pdf_unreadable",
                "outcome": "row_error",
                "terminal": False,
                "row_ids": [actions.bodies[0]["scope"]["row_ids"][0]],
                "details": None,
            }
        ],
    }
    assert [child.action_id for child in status.preparation_children] == [
        "media.read_pdf_text"
    ]

    frozen.occurrences[1] = replace(
        _staged(draft, 2, "scan.pdf", category="missing"), ocr_selected=True
    )
    frozen.staged_through = 2
    PdfImportCommitService(workspace, intake=intake).commit(
        project_id,
        "draft-recovery",
        idempotency_key="commit-recovery",
        expected_revision=1,
    )

    SearchPreparationService(workspace, action_runs=actions).recover()
    assert [body["action_id"] for body in actions.bodies] == [
        "media.read_pdf_text",
        "media.ocr",
    ]
    status = ImportSessionService(
        workspace,
        search_preparation=SearchPreparationService(workspace, action_runs=actions),
    ).status(project_id, opened_ref)
    assert status.preparation_total == 2
    assert status.ocr_total == 1
    assert {child.action_id for child in status.preparation_children} == {
        "media.read_pdf_text",
        "media.ocr",
    }


def test_commit_request_publishes_one_bounded_page_then_enqueues_continuation(
    tmp_path: Path,
) -> None:
    workspace, project_id = _workspace(tmp_path)
    project = workspace.get(project_id)
    draft = tmp_path / "draft-large"
    draft.mkdir()
    occurrences = [
        _staged(draft, ordinal, f"{ordinal}.pdf", category="text")
        for ordinal in range(1, 601)
    ]
    frozen = _Frozen(
        commit_id="pdf-commit-large",
        draft_id="draft-large",
        project_id=project_id,
        storage_identity=project.storage_identity,
        idempotency_key="commit-large",
        selected_through=600,
        selected_total=600,
        staged_through=600,
        manifest_hash="sha256:frozen-large",
        destination={"kind": "new", "sheet_name": "Large PDFs"},
        ocr_policy="none",
        ocr_engine="rapidocr",
        ocr_action_params={"engine": "rapidocr"},
        ocr_authorization=None,
        semantic_search=False,
        semantic_setup_confirmation=None,
        directory=draft,
        commit_inventory_path=draft / "commit-inventory.db",
        committed_total=600,
        available_through=0,
        uploads_complete=False,
        sealed=False,
        occurrences=occurrences,
    )
    queued: list[tuple[Any, ...]] = []
    actions = _ActionRuns()
    preparation = SearchPreparationService(workspace, action_runs=actions)
    service = PdfImportCommitService(
        workspace,
        intake=_Intake(frozen),
        text_preparation=preparation,
        enqueue=lambda *args: queued.append(args),
    )

    opened = service.commit(
        project_id,
        "draft-large",
        idempotency_key="commit-large",
        expected_revision=1,
    )
    assert opened.state == "running"
    assert opened.committed_rows == 256
    assert len(queued) == 1
    registry = HandlerRegistry()
    register_import_files_handler(
        registry,
        workspace_root=workspace.root,
        queue=workspace.queue,
    )
    workspace.queue.enqueue(
        IMPORT_FILES_PAGE_KIND,
        {
            "project_id": project_id,
            "workspace_root": str(workspace.root),
            "import_ref": opened.import_ref,
            "through": queued[0][2],
            "sealed": queued[0][3],
            "dedupe_key": f"test:{opened.import_ref}",
        },
    )
    assert Worker(workspace.queue, registry, worker_id="import-worker").run_once()
    assert project.row_count(opened.sheet_id) == 600
    assert not (project.path / ".imports" / opened.import_ref / "staged").exists()
    preparation.recover()
    assert [len(body["scope"]["row_ids"]) for body in actions.bodies] == [256, 256]
    preparation.recover()
    assert [len(body["scope"]["row_ids"]) for body in actions.bodies] == [
        256,
        256,
        88,
    ]


def test_bootstrap_blob_promotion_uses_the_ordinary_64mib_page_budget(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, int]] = []

    class _Inventory:
        def page(self, **kwargs):
            calls.append(kwargs)
            return []

    project = SimpleNamespace(blob_store=SimpleNamespace(put_path=None))
    PdfImportCommitService._promote_page_blobs(
        project, tmp_path, _Inventory(), after=17
    )

    assert calls == [{"after": 17, "limit": 256, "byte_budget": 64 * 1024 * 1024}]

from __future__ import annotations

import hashlib
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.pdf_imports import PdfImportDraftError, PdfImportService
from frisket.server.workspace import Workspace


PDF = b"%PDF-test-pdf-intake"


def _upload(name: str = "one.pdf", payload: bytes = PDF) -> AdmittedUpload:
    return AdmittedUpload(
        filename=name,
        mime="application/pdf",
        source=io.BytesIO(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def _service(tmp_path: Path, monkeypatch, **kwargs):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("PDF intake", project_id="pdf-intake")["id"])
    service = PdfImportService(workspace, **kwargs)
    monkeypatch.setattr(
        service,
        "_analyze",
        lambda _path: {
            "analysis_state": "done",
            "page_count": 4,
            "category": "missing",
            "sample_pages": [1, 2, 3, 4],
            "error": None,
        },
    )
    return workspace, project_id, service


def _declared(service, project_id):
    created = service.create(
        project_id,
        expected_files=2,
        expected_bytes=2 * len(PDF),
        source_label="Evidence",
    )
    draft_id = created["draft_id"]
    status = service.append_manifest(
        project_id,
        draft_id,
        batch_id="manifest-1",
        files=[
            {
                "client_file_id": "one",
                "logical_path": "north/one.pdf",
                "filename": "one.pdf",
                "size": len(PDF),
            },
            {
                "client_file_id": "two",
                "logical_path": "south/two.pdf",
                "filename": "two.pdf",
                "size": len(PDF),
            },
        ],
    )
    assert status["manifest"]["files_done"] == 2
    return draft_id, service.seal_manifest(project_id, draft_id)


def test_pdf_draft_stages_outside_project_and_recovers_bounded_status(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    project = workspace.get(project_id)
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload("one.pdf"), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="upload-1",
    )
    assert status["upload"]["files_done"] == 2
    assert status["category_counts"]["missing"] == 2
    assert status["can_review_text"] is True
    assert "files" not in status
    assert project.sheets() == []
    assert project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
    directory = workspace.root / ".pdf_import_drafts" / draft_id
    assert directory.is_dir() and project.path not in directory.parents
    recovered = PdfImportService(workspace).status(project_id, draft_id)
    assert recovered["revision"] == status["revision"]
    samples = service.samples(project_id, draft_id, category="missing", limit=1)
    assert len(samples["items"]) == 1
    assert samples["next_cursor"] is not None


def test_upload_replay_discard_and_late_upload_fence(
    tmp_path: Path, monkeypatch
) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    first = service.upload(
        project_id,
        draft_id,
        [_upload()],
        file_ids=["one"],
        logical_paths=["north/one.pdf"],
        batch_id="upload-1",
    )
    replay = service.upload(
        project_id,
        draft_id,
        [_upload()],
        file_ids=["one"],
        logical_paths=["north/one.pdf"],
        batch_id="upload-1",
    )
    assert replay["revision"] == first["revision"]
    assert service.discard(project_id, draft_id)["state"] == "cancelled"
    with pytest.raises(PdfImportDraftError, match="cancelled"):
        service.upload(
            project_id,
            draft_id,
            [_upload("two.pdf")],
            file_ids=["two"],
            logical_paths=["south/two.pdf"],
            batch_id="late",
        )


def test_slow_analysis_does_not_block_status_or_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    started = threading.Event()
    release = threading.Event()

    def analyze(_path):
        started.set()
        assert release.wait(5)
        return {
            "analysis_state": "done",
            "page_count": 1,
            "category": "text",
            "sample_pages": [1],
            "error": None,
        }

    monkeypatch.setattr(service, "_analyze", analyze)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.upload,
            project_id,
            draft_id,
            [_upload()],
            file_ids=["one"],
            logical_paths=["north/one.pdf"],
            batch_id="slow-analysis",
        )
        assert started.wait(5)
        pending = service.status(project_id, draft_id)
        assert pending["upload"]["files_done"] == 1
        assert pending["analysis"]["files_done"] == 0
        assert service.discard(project_id, draft_id)["state"] == "cancelled"
        release.set()
        assert future.result(timeout=5)["state"] == "cancelled"


def test_freeze_projects_unmatched_inventory_and_late_bytes_advance_it(
    tmp_path: Path, monkeypatch
) -> None:
    matched_calls = []
    ready_calls = []

    def matcher(_project, sheet_id, candidates):
        matched_calls.append((sheet_id, candidates))
        return {("one.pdf", len(PDF))}

    def notify_ready(*args):
        ready_calls.append(args)
        if len(ready_calls) == 1:
            raise RuntimeError("queue temporarily unavailable")

    _workspace, project_id, service = _service(
        tmp_path,
        monkeypatch,
        destination_matcher=matcher,
        on_commit_ready=notify_ready,
    )
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload()],
        file_ids=["one"],
        logical_paths=["north/one.pdf"],
        batch_id="upload-1",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="none",
        ocr_engine="rapidocr",
        ocr_confirmation=None,
        semantic_search=True,
        semantic_setup_confirmation=status["semantic_setup"]["confirmation_hash"],
        destination={"kind": "existing", "sheet_id": 7},
    )
    compared = service.match_destination(project_id, draft_id)
    assert compared["match"]["matched_count"] == 1
    assert compared["match"]["new_count"] == 1
    assert matched_calls == [(7, [("one.pdf", len(PDF)), ("two.pdf", len(PDF))])]
    frozen = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        expected_revision=compared["revision"],
    )
    assert frozen.committed_total == 1
    assert frozen.available_through == 0
    assert frozen.uploads_complete is False
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        service.upload(
            project_id,
            draft_id,
            [_upload("two.pdf")],
            file_ids=["two"],
            logical_paths=["south/two.pdf"],
            batch_id="upload-2",
        )
    late = service.upload(
        project_id,
        draft_id,
        [_upload("two.pdf")],
        file_ids=["two"],
        logical_paths=["south/two.pdf"],
        batch_id="upload-2",
    )
    expected_ready = (project_id, draft_id, "commit-1", late["revision"] - 1)
    assert ready_calls == [expected_ready, expected_ready]
    replay = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        expected_revision=0,
    )
    assert replay.available_through == 1
    assert replay.sealed is True
    (occurrence,) = replay.page_occurrences()
    assert occurrence.occurrence_id == "two"
    assert occurrence.inventory_ordinal == 2
    handoff = service.record_commit_handoff(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        result={"sheet_id": 11, "session_ref": "import-1"},
    )
    assert handoff["state"] == "committing"
    service.reconcile_commit(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        committed=True,
        result={"sheet_id": 11, "session_ref": "import-1"},
    )
    terminal_replay = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        expected_revision=0,
    )
    assert terminal_replay.commit_result == {
        "sheet_id": 11,
        "session_ref": "import-1",
    }


def test_manifest_scope_and_preflight_are_enforced(tmp_path: Path, monkeypatch) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    with pytest.raises(PdfImportDraftError, match="at least two"):
        service.create(
            project_id, expected_files=1, expected_bytes=1, source_label=None
        )
    created = service.create(
        project_id, expected_files=2, expected_bytes=2, source_label=None
    )
    with pytest.raises(PdfImportDraftError, match="PDF paths"):
        service.append_manifest(
            project_id,
            created["draft_id"],
            batch_id="bad",
            files=[
                {
                    "client_file_id": "bad",
                    "logical_path": "bad.txt",
                    "filename": "bad.txt",
                    "size": 2,
                }
            ],
        )
    with pytest.raises(PdfImportDraftError, match="incomplete"):
        service.seal_manifest(project_id, created["draft_id"])


def test_reselection_preserves_duplicate_occurrences(
    tmp_path: Path, monkeypatch
) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    created = service.create(
        project_id,
        expected_files=2,
        expected_bytes=2 * len(PDF),
        source_label=None,
    )
    draft_id = created["draft_id"]
    service.append_manifest(
        project_id,
        draft_id,
        batch_id="duplicates",
        files=[
            {
                "client_file_id": occurrence_id,
                "logical_path": "same.pdf",
                "filename": "same.pdf",
                "size": len(PDF),
            }
            for occurrence_id in ("one", "two")
        ],
    )
    service.seal_manifest(project_id, draft_id)
    reconciled = service.reconcile_reselection(
        project_id,
        draft_id,
        files=[
            {
                "logical_path": "same.pdf",
                "filename": "same.pdf",
                "size": len(PDF),
            },
            {
                "logical_path": "same.pdf",
                "filename": "same.pdf",
                "size": len(PDF),
            },
        ],
    )
    assert [item["occurrence_id"] for item in reconciled["items"]] == ["one", "two"]

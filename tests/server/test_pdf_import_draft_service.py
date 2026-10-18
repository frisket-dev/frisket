from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from frisket.actions.document_extraction_types import (
    Box,
    PositionedPage,
    PositionedToken,
)
from frisket.engine.pdf_text import PdfTextBatch, PdfTextError, PdfTextSample
from frisket.server.services.pdf_import_commit import PdfImportCommitService
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.pdf_imports import PdfImportDraftError, PdfImportService
from frisket.server.services.pdf_imports.analysis import analyze_pdf
from frisket.server.services.pdf_imports.inventory import PdfDraftInventory
from frisket.server.services.pdf_imports.previews import (
    preview_settings,
    read_native_page,
    thumbnail,
)
from frisket.server.workspace import Workspace


PDF = b"%PDF-test-pdf-intake"


def test_concurrent_thumbnails_use_isolated_render_scratch(
    tmp_path: Path, monkeypatch
) -> None:
    barrier = threading.Barrier(2)
    scratches = []

    async def render(source, scratch, **_kwargs):
        scratches.append(scratch)
        barrier.wait(timeout=5)
        rendered = scratch / "page-1.png"
        rendered.write_bytes(source.read_bytes())
        barrier.wait(timeout=5)
        return SimpleNamespace(pages=((1, rendered),))

    monkeypatch.setattr(
        "frisket.server.services.pdf_imports.previews.render_pdf_pages", render
    )
    sources = []
    for name, payload in (("one.pdf", b"first"), ("two.pdf", b"second")):
        source = tmp_path / name
        source.write_bytes(payload)
        sources.append(source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(
            pool.map(
                lambda item: thumbnail(
                    tmp_path,
                    source=item[1],
                    occurrence_id=item[0],
                    page=1,
                ),
                (("one", sources[0]), ("two", sources[1])),
            )
        )
    try:
        assert len(set(scratches)) == 2
        assert [lease.path.read_bytes() for lease in leases] == [b"first", b"second"]
    finally:
        for lease in leases:
            lease.close()


def test_dense_first_page_keeps_representative_navigation_without_false_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []

    async def extract(_path, *, start_page, max_pages, max_chars, timeout_seconds):
        calls.append((start_page, max_pages, max_chars, timeout_seconds))
        return PdfTextBatch(
            pages=[
                PositionedPage(
                    page=start_page,
                    width=612,
                    height=792,
                    tokens=[
                        PositionedToken(
                            text="x" * max_chars,
                            box=Box(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                        )
                    ],
                )
            ],
            page_count=8,
            next_page=2,
        )

    monkeypatch.setattr(
        "frisket.server.services.pdf_imports.analysis.extract_pdf_text_batch", extract
    )
    result = analyze_pdf(
        tmp_path / "dense.pdf", quality_assessor=lambda *_args, **_kwargs: ()
    )
    assert result["analysis_state"] == "done"
    assert result["category"] == "text"
    assert result["page_count"] == 8
    assert len(result["sample_pages"]) == 6
    assert calls == [(1, 1, 12_000, 30)]


def test_dense_positioned_page_falls_back_to_bounded_text_sample(
    tmp_path: Path, monkeypatch
) -> None:
    async def positioned(*_args, **_kwargs):
        raise PdfTextError("PDF page exceeds the positioned-text character limit")

    sample_calls = []

    async def sample(_path, *, page, max_chars, timeout_seconds):
        sample_calls.append((page, max_chars, timeout_seconds))
        return PdfTextSample("readable " * 1_500, page, 9, True)

    monkeypatch.setattr(
        "frisket.server.services.pdf_imports.analysis.extract_pdf_text_batch",
        positioned,
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_imports.analysis.sample_pdf_text", sample
    )
    result = analyze_pdf(
        tmp_path / "dense.pdf", quality_assessor=lambda *_args, **_kwargs: ()
    )
    assert result["analysis_state"] == "done"
    assert result["category"] == "text"
    assert result["page_count"] == 9
    assert len(result["sample_pages"]) == 6
    assert sample_calls == [(1, 12_000, 30)]


def test_actual_dense_pdf_is_readable_with_bounded_fallback(tmp_path: Path) -> None:
    source = tmp_path / "dense.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 8 Tf 20 700 Td ({' '.join(['DENSE'] * 3_000)}) Tj ET".encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    with source.open("wb") as handle:
        writer.write(handle)
    result = analyze_pdf(source, quality_assessor=lambda *_args, **_kwargs: ())
    assert result == {
        "analysis_state": "done",
        "page_count": 1,
        "category": "text",
        "sample_pages": [1],
        "error": None,
    }
    text, truncated = read_native_page(source, page=1)
    assert len(text) == 12_000
    assert truncated is True


def _upload(
    name: str = "one.pdf",
    payload: bytes = PDF,
    mime: str = "application/pdf",
) -> AdmittedUpload:
    return AdmittedUpload(
        filename=name,
        mime=mime,
        source=io.BytesIO(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def _png_payload() -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (12, 8), (24, 80, 160)).save(output, format="PNG")
    return output.getvalue()


def _staged_selection(
    root: Path, items: list[tuple[str, bytes, str]]
) -> tuple[Path, list[dict]]:
    staging = root / "private-staging"
    files = staging / "files"
    files.mkdir(parents=True)
    entries = []
    for logical_path, payload, mime in items:
        digest = hashlib.sha256(payload).hexdigest()
        target = files / digest
        if not target.exists():
            target.write_bytes(payload)
        entries.append(
            {
                "logical_path": logical_path,
                "filename": Path(logical_path).name,
                "mime": mime,
                "kind": "files",
                "path": f"files/{digest}",
                "sha256": digest,
                "size": len(payload),
            }
        )
    return staging, entries


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


def test_adopted_server_staging_publishes_all_bytes_before_bounded_analysis(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    note = b"supporting attachment"
    staging, entries = _staged_selection(
        tmp_path,
        [
            ("one.pdf", PDF, "application/pdf"),
            ("two.pdf", PDF + b"-two", "application/pdf"),
            ("notes/readme.txt", note, "text/plain"),
        ],
    )

    adopted = service.adopt_staged(
        project_id, staging, entries, source_label="Archive evidence"
    )

    assert not staging.exists()
    assert adopted["manifest"]["files_done"] == 3
    assert adopted["manifest"]["sealed"] is True
    assert adopted["upload"]["files_done"] == 3
    assert adopted["upload"]["sealed"] is True
    assert adopted["analysis"] == {"files_done": 0, "files_total": 3}
    assert adopted["category_counts"]["unchecked"] == 3
    directory = workspace.root / ".pdf_import_drafts" / adopted["draft_id"]
    assert {path.name for path in (directory / "files").iterdir()} == {
        str(entry["sha256"]) for entry in entries
    }

    first = service.analyze_staged(project_id, adopted["draft_id"], limit=2)
    assert first["analysis"]["files_done"] == 2
    second = service.analyze_staged(project_id, adopted["draft_id"], limit=2)
    assert second["analysis"]["files_done"] == 3
    replay = service.analyze_staged(project_id, adopted["draft_id"], limit=2)
    assert replay["revision"] == second["revision"]
    assert replay["category_counts"] == {
        "text": 0,
        "suspicious": 0,
        "missing": 2,
        "image": 0,
        "other": 1,
        "unreadable": 0,
        "unchecked": 0,
    }


def test_adopted_server_staging_failure_has_one_cleanup_owner(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    staging, entries = _staged_selection(
        tmp_path,
        [
            ("one.pdf", PDF, "application/pdf"),
            ("two.pdf", PDF + b"-two", "application/pdf"),
        ],
    )
    entries[1]["size"] += 1

    with pytest.raises(PdfImportDraftError, match="staged source"):
        service.adopt_staged(project_id, staging, entries)

    assert not staging.exists()
    assert list((workspace.root / ".pdf_import_drafts").iterdir()) == []


def test_stale_unpublished_adoption_is_reclaimed(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    hidden = workspace.root / ".pdf_import_drafts" / ".adopt-pdf-interrupted"
    hidden.mkdir(parents=True)
    (hidden / "files").mkdir()
    os.utime(hidden, (0, 0))

    PdfImportService(workspace)

    assert not hidden.exists()


def test_mixed_files_keep_mime_preview_and_missing_ocr_scope(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    png = _png_payload()
    note = b"supporting attachment"
    broken = b"not a real png"
    fake_pdf = b"this is not a PDF"
    eps = (
        b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
        b"newpath 0 0 moveto 10 10 lineto stroke\nshowpage\n"
    )
    monkeypatch.setattr(
        service,
        "_analyze",
        lambda path: (
            {
                "analysis_state": "done",
                "page_count": 4,
                "category": "missing",
                "sample_pages": [1, 2, 3, 4],
                "error": None,
            }
            if path.read_bytes() == PDF
            else analyze_pdf(path, quality_assessor=lambda *_args, **_kwargs: ())
        ),
    )
    declared = [
        ("pdf", "evidence/report.pdf", PDF),
        ("fake-pdf", "evidence/not-really.pdf", fake_pdf),
        ("image", "evidence/photo.pdf", png),
        ("other", "evidence/notes.txt", note),
        ("unsupported-image", "evidence/vector.eps", eps),
        ("broken", "evidence/broken.png", broken),
    ]
    created = service.create(
        project_id,
        expected_files=len(declared),
        expected_bytes=sum(len(payload) for _, _, payload in declared),
        source_label="Mixed evidence",
    )
    draft_id = created["draft_id"]
    service.append_manifest(
        project_id,
        draft_id,
        batch_id="mixed-manifest",
        files=[
            {
                "client_file_id": occurrence_id,
                "logical_path": path,
                "filename": Path(path).name,
                "size": len(payload),
            }
            for occurrence_id, path, payload in declared
        ],
    )
    service.seal_manifest(project_id, draft_id)
    uploads = [
        _upload("report.pdf", PDF, "application/octet-stream"),
        _upload("not-really.pdf", fake_pdf, "text/plain"),
        _upload("photo.pdf", png, "application/pdf"),
        _upload("notes.txt", note, "text/plain"),
        _upload("vector.eps", eps, "application/postscript"),
        _upload("broken.png", broken, "image/png"),
    ]
    status = service.upload(
        project_id,
        draft_id,
        uploads,
        file_ids=[item[0] for item in declared],
        logical_paths=[item[1] for item in declared],
        batch_id="mixed-upload",
    )
    assert status["category_counts"] == {
        "text": 0,
        "suspicious": 0,
        "missing": 1,
        "image": 1,
        "other": 2,
        "unreadable": 2,
        "unchecked": 0,
    }

    replay = service.upload(
        project_id,
        draft_id,
        [
            _upload("report.pdf", PDF, "application/octet-stream"),
            _upload("not-really.pdf", fake_pdf, "text/plain"),
            _upload("photo.pdf", png, "application/pdf"),
            _upload("notes.txt", note, "text/plain"),
            _upload("vector.eps", eps, "application/postscript"),
            _upload("broken.png", broken, "image/png"),
        ],
        file_ids=[item[0] for item in declared],
        logical_paths=[item[1] for item in declared],
        batch_id="mixed-upload",
    )
    assert replay["revision"] == status["revision"]

    image_page = service.page(project_id, draft_id, "image", 1)
    assert image_page["native_text"] == ""
    assert image_page["native_text_truncated"] is False
    lease = service.thumbnail(project_id, draft_id, "image", 1)
    try:
        assert lease.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        lease.close()
    with pytest.raises(PdfImportDraftError, match="no reviewable page"):
        service.page(project_id, draft_id, "other", 1)

    other = service.samples(project_id, draft_id, category="other")
    assert other["extension_counts"] == [
        {"extension": ".eps", "count": 1},
        {"extension": ".txt", "count": 1},
    ]
    assert other["unlisted_extension_count"] == 0

    choices = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="missing",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "Mixed evidence"},
    )
    frozen = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="mixed-commit",
        expected_revision=choices["revision"],
    )
    occurrences = frozen.page_occurrences()
    assert [(row.mime, row.category, row.ocr_selected) for row in occurrences] == [
        ("application/pdf", "missing", True),
        ("application/pdf", "unreadable", False),
        ("image/png", "image", True),
        ("text/plain", "other", False),
        ("application/postscript", "other", False),
        ("image/png", "unreadable", False),
    ]
    with sqlite3.connect(frozen.commit_inventory_path) as db:
        assert db.execute(
            "SELECT mime,ocr_selected FROM pdf_commit_occurrences ORDER BY ordinal"
        ).fetchall() == [
            ("application/pdf", 1),
            ("application/pdf", 0),
            ("image/png", 1),
            ("text/plain", 0),
            ("application/postscript", 0),
            ("image/png", 0),
        ]
        assert db.execute(
            "SELECT mime FROM inventory_items ORDER BY ordinal"
        ).fetchall() == [
            ("application/pdf",),
            ("application/pdf",),
            ("image/png",),
            ("text/plain",),
            ("application/postscript",),
            ("image/png",),
        ]
    assert workspace.get(project_id).sheets() == []


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


def test_status_reconciles_preview_registry_terminal_state(
    tmp_path: Path, monkeypatch
) -> None:
    preview_runs = SimpleNamespace(
        get_preview=lambda _project_id, _job_id: SimpleNamespace(
            status_code=200,
            payload={
                "status": "done",
                "accounting": {"receipt_id": "receipt-1"},
            },
        )
    )
    workspace, project_id, service = _service(
        tmp_path, monkeypatch, preview_runs=preview_runs
    )
    draft_id, _ = _declared(service, project_id)
    manifest_path = workspace.root / ".pdf_import_drafts" / draft_id / "manifest.json"
    header = json.loads(manifest_path.read_text("utf-8"))
    header["preview_jobs"] = [
        {
            "job_id": "preview-1",
            "occurrence_id": "one",
            "page": 1,
            "ocr_action_params": {"engine": "rapidocr"},
            "status": "running",
            "receipt_id": None,
            "error": None,
        }
    ]
    service._store.write_header(manifest_path.parent, header)
    status = service.status(project_id, draft_id)
    assert status["preview_jobs"] == [
        {
            **header["preview_jobs"][0],
            "status": "done",
            "receipt_id": "receipt-1",
        }
    ]


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
        replay = service.freeze_for_commit(
            project_id,
            draft_id,
            idempotency_key="commit-1",
            expected_revision=0,
        )
        assert replay.commit_id == frozen.commit_id
        if len(ready_calls) == 1:
            raise RuntimeError("queue temporarily unavailable")

    workspace, project_id, service = _service(
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
        ocr_selection={
            "kind": "engine_model",
            "engine": "llm",
            "model": "openai/gpt-4.1-mini",
        },
        semantic_search=True,
        semantic_setup_confirmation=status["semantic_setup"]["confirmation_hash"],
        destination={"kind": "existing", "sheet_id": 7},
    )
    compared = service.match_destination(project_id, draft_id)
    assert compared["match"]["matched_count"] == 1
    assert compared["match"]["new_count"] == 1
    assert compared["match"]["new_category_counts"]["unchecked"] == 1
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
    assert frozen.ocr_action_params == {
        "engine": "llm",
        "model": "openai/gpt-4.1-mini",
    }
    intake_root = workspace.get(project_id).path / ".imports" / "pdf-import-1"
    intake_root.mkdir(parents=True)
    promoted = service.promote_commit_inventory(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        target_path=intake_root / "inventory.db",
    )
    assert promoted.commit_inventory_path == intake_root / "inventory.db"
    assert promoted.source_files_root == intake_root / "staged"
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
    assert late["match"]["state"] == "ready"
    assert late["match"]["matched_count"] == 1
    assert late["match"]["new_category_counts"]["unchecked"] == 0
    assert late["match"]["new_category_counts"]["missing"] == 1
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
    assert occurrence.path is not None
    assert occurrence.path.parent == intake_root / "staged"
    with replay.commit_inventory_path.open("rb"):
        pass
    with sqlite3.connect(replay.commit_inventory_path) as db:
        assert db.execute(
            "SELECT occurrence_id,category,error,sha256,page_count,ocr_selected "
            "FROM pdf_commit_occurrences WHERE ordinal=1"
        ).fetchone() == (
            "two",
            "missing",
            None,
            hashlib.sha256(PDF).hexdigest(),
            4,
            0,
        )
    shutil.rmtree(replay.source_files_root)
    promoted_replay = service.promote_commit_inventory(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        target_path=intake_root / "inventory.db",
    )
    assert promoted_replay.commit_id == replay.commit_id
    handoff = service.record_commit_handoff(
        project_id,
        draft_id,
        idempotency_key="commit-1",
        result={"sheet_id": 11, "session_ref": "import-1"},
    )
    assert handoff["state"] == "committing"
    assert "commit_inventory_path" not in handoff["commit"]
    assert "source_files_root" not in handoff["commit"]
    assert "promotion_target" not in handoff["commit"]
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
    with pytest.raises(PdfImportDraftError, match="result changed"):
        service.reconcile_commit(
            project_id,
            draft_id,
            idempotency_key="commit-1",
            committed=True,
            result={"sheet_id": 12, "session_ref": "import-2"},
        )


def test_paid_ocr_commit_requires_current_import_scope_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    quoted = []

    def quote(_project_id, binding, _request_context):
        quoted.append(dict(binding))
        promise_hash = "a" * 64
        return {
            "estimate": {
                "rows": 2,
                "cost": 0.16,
                "cost_source": "provider_quote",
                "billed_cost": 160_000,
                "policy_id": "identity.v1",
                "pricing_key": "paid-ocr-page",
                "engine": "remote-ocr",
                "claims": [{"field": "cost", "display": "$0.16"}],
                "promise_set_hash": promise_hash,
                "requires_confirmation": True,
            },
            "authorization": {
                "schema_version": "frisket.pdf_import_ocr_approval.v1",
                "actor": "user:test",
                "promise_set_hash": promise_hash,
                "scope_hash": binding["manifest_hash"],
                "scope": {
                    "storage_identity": binding["storage_identity"],
                    "draft_id": binding["draft_id"],
                    "roster_digest": binding["work_scope_hash"],
                    "source_count": binding["selected_count"],
                    "page_count": binding["known_pages"],
                },
                "ocr_action_params": binding["ocr_action_params"],
                "target": {"target_id": "paid"},
                "route": {"engine": "remote-ocr"},
                "promises": [{"field": "cost", "op": "le", "value": "0.16"}],
                "quote": {"billed_cost": 160_000, "policy_id": "identity.v1"},
                "allocation": {
                    "schema_version": "frisket.pdf_import_ocr_allocation.v1",
                    "roster": "effective_unmatched_ocr_selected_commit_order",
                    "max_commit_occurrences_per_child": 256,
                    "source_count": 2,
                    "page_count": 8,
                    "approved_billed_cost": 160_000,
                    "currency": "USD",
                    "policy_id": "identity.v1",
                    "row_quote_authority": "canonical_commit_occurrences",
                    "children": [
                        {
                            "child_key": "commit-ordinals:1-2",
                            "commit_ordinal_start": 1,
                            "commit_ordinal_end": 2,
                            "source_count": 2,
                            "page_count": 8,
                            "quote": {
                                "billed_cost": 160_000,
                                "policy_id": "identity.v1",
                            },
                        }
                    ],
                },
            },
        }

    _workspace, project_id, service = _service(
        tmp_path, monkeypatch, batch_ocr_quoter=quote
    )
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="upload-paid",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="all",
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    estimate = service.estimate_batch_ocr(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        ocr_policy="all",
    )
    assert estimate["advisory"] is False
    assert estimate["revision"] == service.status(project_id, draft_id)["revision"]
    assert estimate["estimate"]["billed_cost"] == 160_000
    assert quoted[0]["selected_count"] == 2
    assert quoted[0]["known_pages"] == 8

    for confirmation in (None, "b" * 64):
        with pytest.raises(PdfImportDraftError, match="OCR approval"):
            service.freeze_for_commit(
                project_id,
                draft_id,
                idempotency_key="paid-commit",
                expected_revision=estimate["revision"],
                ocr_confirmation=confirmation,
            )
    frozen = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="paid-commit",
        expected_revision=estimate["revision"],
        ocr_confirmation="a" * 64,
    )
    assert frozen.ocr_authorization is not None
    assert frozen.ocr_authorization.actor == "user:test"
    assert frozen.ocr_authorization.scope["page_count"] == 8
    assert len(quoted) == 4


def test_paid_ocr_freeze_rejects_route_or_price_change(
    tmp_path: Path, monkeypatch
) -> None:
    calls = 0

    def quote(_project_id, binding, _request_context):
        nonlocal calls
        calls += 1
        promise_hash = ("a" if calls == 1 else "b") * 64
        return {
            "estimate": {
                "rows": 2,
                "cost": 0.16,
                "cost_source": "provider_quote",
                "billed_cost": 160_000,
                "policy_id": "identity.v1",
                "pricing_key": "paid-ocr-page",
                "engine": "remote-ocr",
                "claims": [{"field": "cost", "display": "$0.16"}],
                "promise_set_hash": promise_hash,
                "requires_confirmation": True,
            },
            "authorization": {
                "schema_version": "frisket.pdf_import_ocr_approval.v1",
                "actor": "user:test",
                "promise_set_hash": promise_hash,
                "scope_hash": binding["manifest_hash"],
                "scope": {
                    "storage_identity": binding["storage_identity"],
                    "draft_id": binding["draft_id"],
                    "roster_digest": binding["work_scope_hash"],
                    "source_count": binding["selected_count"],
                    "page_count": binding["known_pages"],
                },
                "ocr_action_params": binding["ocr_action_params"],
                "target": {"target_id": "paid"},
                "route": {"engine": "remote-ocr"},
                "promises": [{"field": "cost", "op": "le", "value": "0.16"}],
                "quote": {"billed_cost": 160_000, "policy_id": "identity.v1"},
                "allocation": {
                    "schema_version": "frisket.pdf_import_ocr_allocation.v1",
                    "roster": "effective_unmatched_ocr_selected_commit_order",
                    "max_commit_occurrences_per_child": 256,
                    "source_count": 2,
                    "page_count": 8,
                    "approved_billed_cost": 160_000,
                    "currency": "USD",
                    "policy_id": "identity.v1",
                    "row_quote_authority": "canonical_commit_occurrences",
                    "children": [
                        {
                            "child_key": "commit-ordinals:1-2",
                            "commit_ordinal_start": 1,
                            "commit_ordinal_end": 2,
                            "source_count": 2,
                            "page_count": 8,
                            "quote": {
                                "billed_cost": 160_000,
                                "policy_id": "identity.v1",
                            },
                        }
                    ],
                },
            },
        }

    _workspace, project_id, service = _service(
        tmp_path, monkeypatch, batch_ocr_quoter=quote
    )
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="upload-paid",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="all",
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    estimate = service.estimate_batch_ocr(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        ocr_policy="all",
    )
    with pytest.raises(PdfImportDraftError, match="OCR quote changed"):
        service.freeze_for_commit(
            project_id,
            draft_id,
            idempotency_key="paid-stale",
            expected_revision=estimate["revision"],
            ocr_confirmation="a" * 64,
        )


def test_remote_ocr_with_no_effective_work_needs_no_quote(
    tmp_path: Path, monkeypatch
) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service,
        "_analyze",
        lambda _path: {
            "analysis_state": "done",
            "page_count": 4,
            "category": "text",
            "sample_pages": [1, 2, 3, 4],
            "error": None,
        },
    )
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="upload-clean",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="missing_or_suspicious",
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )

    frozen = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="no-ocr-work",
        expected_revision=status["revision"],
    )

    assert frozen.ocr_authorization is None
    assert frozen.ocr_policy == "missing_or_suspicious"


def test_paid_ocr_all_excludes_unreadable_pdf_but_keeps_import_row(
    tmp_path: Path, monkeypatch
) -> None:
    corrupt_pdf = b"%PDF-corrupt".ljust(len(PDF), b"x")
    quoted = []

    def quote(_project_id, binding, _request_context):
        quoted.append(dict(binding))
        promise_hash = "a" * 64
        child = binding["child_partitions"][0]
        return {
            "estimate": {
                "rows": 1,
                "cost": 0.08,
                "cost_source": "provider_quote",
                "billed_cost": 80_000,
                "policy_id": "identity.v1",
                "engine": "remote-ocr",
                "claims": [{"field": "cost", "display": "$0.08"}],
                "promise_set_hash": promise_hash,
                "requires_confirmation": True,
            },
            "authorization": {
                "schema_version": "frisket.pdf_import_ocr_approval.v1",
                "actor": "user:test",
                "promise_set_hash": promise_hash,
                "scope_hash": binding["manifest_hash"],
                "scope": {
                    "storage_identity": binding["storage_identity"],
                    "draft_id": binding["draft_id"],
                    "roster_digest": binding["work_scope_hash"],
                    "source_count": 1,
                    "page_count": 4,
                },
                "ocr_action_params": binding["ocr_action_params"],
                "target": {"target_id": "paid"},
                "route": {"engine": "remote-ocr"},
                "promises": [],
                "quote": {"billed_cost": 80_000, "policy_id": "identity.v1"},
                "allocation": {
                    "schema_version": "frisket.pdf_import_ocr_allocation.v1",
                    "roster": "effective_unmatched_ocr_selected_commit_order",
                    "max_commit_occurrences_per_child": 256,
                    "source_count": 1,
                    "page_count": 4,
                    "approved_billed_cost": 80_000,
                    "currency": "USD",
                    "policy_id": "identity.v1",
                    "row_quote_authority": "canonical_commit_occurrences",
                    "children": [
                        {
                            **{
                                key: value
                                for key, value in child.items()
                                if key != "row_pages"
                            },
                            "quote": {
                                "billed_cost": 80_000,
                                "policy_id": "identity.v1",
                            },
                        }
                    ],
                },
            },
        }

    _workspace, project_id, service = _service(
        tmp_path, monkeypatch, batch_ocr_quoter=quote
    )

    def analyze(path):
        if path.read_bytes() == corrupt_pdf:
            return {
                "analysis_state": "error",
                "page_count": None,
                "category": "unreadable",
                "sample_pages": [],
                "error": "Could not read PDF",
            }
        return {
            "analysis_state": "done",
            "page_count": 4,
            "category": "missing",
            "sample_pages": [1, 2, 3, 4],
            "error": None,
        }

    monkeypatch.setattr(service, "_analyze", analyze)
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf", corrupt_pdf)],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="mixed-upload",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="all",
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    estimate = service.estimate_batch_ocr(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_selection={"kind": "engine", "engine": "remote-ocr"},
        ocr_policy="all",
    )
    frozen = service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="mixed-paid",
        expected_revision=estimate["revision"],
        ocr_confirmation="a" * 64,
    )

    assert quoted[0]["selected_count"] == 1
    assert quoted[0]["known_pages"] == 4
    assert quoted[0]["child_partitions"][0]["row_pages"] == {1: 4}
    occurrences = frozen.page_occurrences()
    assert [(row.category, row.ocr_selected) for row in occurrences] == [
        ("missing", True),
        ("unreadable", False),
    ]
    assert occurrences[1].error == "Could not read PDF"


def test_destination_match_uses_bounded_current_value_projection(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Existing PDFs")
    columns = {
        "filename": project.add_column(sheet_id, "Filename"),
        "size": project.add_column(sheet_id, "Size", type="integer"),
    }
    project.add_rows(
        sheet_id,
        [
            {
                "filename": "one.pdf" if index == 1024 else f"existing-{index}.pdf",
                "size": len(PDF),
            }
            for index in range(2048)
        ],
        columns,
    )
    monkeypatch.setattr(
        project,
        "get_values",
        lambda *_args, **_kwargs: pytest.fail("destination match loaded a full column"),
    )
    draft_id, status = _declared(service, project_id)
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="none",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=True,
        semantic_setup_confirmation=status["semantic_setup"]["confirmation_hash"],
        destination={"kind": "existing", "sheet_id": sheet_id},
    )
    matched = service.match_destination(project_id, draft_id)
    assert matched["match"]["checked"] == 2
    assert matched["match"]["matched_count"] == 1
    assert matched["match"]["new_count"] == 1


def test_upload_preserves_full_roster_destination_match(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(
        tmp_path,
        monkeypatch,
        destination_matcher=lambda _project, _sheet, candidates: {candidates[0]},
    )
    draft_id, status = _declared(service, project_id)
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="none",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=True,
        semantic_setup_confirmation=status["semantic_setup"]["confirmation_hash"],
        destination={"kind": "existing", "sheet_id": 7},
    )
    matched = service.match_destination(project_id, draft_id)
    assert matched["match"]["new_category_counts"]["unchecked"] == 1
    uploaded = service.upload(
        project_id,
        draft_id,
        [_upload("two.pdf")],
        file_ids=["two"],
        logical_paths=["south/two.pdf"],
        batch_id="upload-after-match",
    )
    assert uploaded["match"]["state"] == "ready"
    assert uploaded["match"]["matched_count"] == 1
    assert uploaded["match"]["new_category_counts"]["unchecked"] == 0
    assert uploaded["match"]["new_category_counts"]["missing"] == 1
    assert workspace.get(project_id).sheets() == []


def test_manifest_scope_and_preflight_are_enforced(tmp_path: Path, monkeypatch) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    with pytest.raises(PdfImportDraftError, match="at least two"):
        service.create(
            project_id, expected_files=1, expected_bytes=1, source_label=None
        )
    created = service.create(
        project_id, expected_files=2, expected_bytes=2, source_label=None
    )
    with pytest.raises(PdfImportDraftError, match="safe relative paths"):
        service.append_manifest(
            project_id,
            created["draft_id"],
            batch_id="bad",
            files=[
                {
                    "client_file_id": "bad",
                    "logical_path": "../bad.txt",
                    "filename": "bad.txt",
                    "size": 2,
                }
            ],
        )
    with pytest.raises(PdfImportDraftError, match="incomplete"):
        service.seal_manifest(project_id, created["draft_id"])


def test_junk_manifest_is_rejected_atomically_without_consuming_ordinals(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    created = service.create(
        project_id,
        expected_files=2,
        expected_bytes=2 * len(PDF),
        source_label="Folder",
    )
    draft_id = created["draft_id"]
    with pytest.raises(PdfImportDraftError, match="ignored system file"):
        service.append_manifest(
            project_id,
            draft_id,
            batch_id="manifest-folder",
            files=[
                {
                    "client_file_id": "junk",
                    "logical_path": "folder/._one.pdf",
                    "filename": "._one.pdf",
                    "size": len(PDF),
                },
                {
                    "client_file_id": "two",
                    "logical_path": "folder/two.pdf",
                    "filename": "two.pdf",
                    "size": len(PDF),
                },
            ],
        )
    assert service.status(project_id, draft_id)["manifest"]["files_done"] == 0

    admitted = service.append_manifest(
        project_id,
        draft_id,
        batch_id="manifest-folder",
        files=[
            {
                "client_file_id": occurrence_id,
                "logical_path": f"folder/{occurrence_id}.pdf",
                "filename": f"{occurrence_id}.pdf",
                "size": len(PDF),
            }
            for occurrence_id in ("one", "two")
        ],
    )
    assert admitted["manifest"]["files_done"] == 2
    directory = workspace.root / ".pdf_import_drafts" / draft_id
    with PdfDraftInventory(directory / "inventory.db") as inventory:
        assert [
            (row["ordinal"], row["occurrence_id"])
            for row in inventory.staged_page(after=0, limit=10)
        ] == [(1, "one"), (2, "two")]


def test_upload_rejects_non_route_safe_occurrence_id(
    tmp_path: Path, monkeypatch
) -> None:
    _workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    with pytest.raises(PdfImportDraftError, match="URL-safe"):
        service.upload(
            project_id,
            draft_id,
            [_upload()],
            file_ids=["folder/one"],
            logical_paths=["north/one.pdf"],
            batch_id="unsafe-id",
        )


def test_service_startup_purges_expired_terminal_draft(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    service.discard(project_id, draft_id)
    directory = workspace.root / ".pdf_import_drafts" / draft_id
    manifest = directory / "manifest.json"
    header = json.loads(manifest.read_text("utf-8"))
    header["expires_at"] = "2000-01-01T00:00:00Z"
    service._store.write_header(directory, header)
    PdfImportService(workspace)
    assert not directory.exists()


def test_service_startup_purges_expired_interrupted_cancellation(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    directory = workspace.root / ".pdf_import_drafts" / draft_id
    manifest = directory / "manifest.json"
    header = json.loads(manifest.read_text("utf-8"))
    header["state"] = "cancelling"
    header["expires_at"] = "2000-01-01T00:00:00Z"
    service._store.write_header(directory, header)

    PdfImportService(workspace)

    assert not directory.exists()


def test_transient_promotion_failure_preserves_commit_for_retry(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="transient-promotion-upload",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="none",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    commit = PdfImportCommitService(workspace, intake=service)
    promote = service.promote_commit_inventory

    def analysis_running(*_args, **_kwargs):
        raise PdfImportDraftError(409, "PDF text analysis is still running")

    monkeypatch.setattr(service, "promote_commit_inventory", analysis_running)
    with pytest.raises(PdfImportDraftError, match="analysis is still running"):
        commit.commit(
            project_id,
            draft_id,
            idempotency_key="transient-promotion",
            expected_revision=status["revision"],
        )

    retry_status = service.status(project_id, draft_id)
    assert retry_status["state"] == "committing"
    assert retry_status["commit"] is not None

    monkeypatch.setattr(service, "promote_commit_inventory", promote)
    result = commit.commit(
        project_id,
        draft_id,
        idempotency_key="transient-promotion",
        expected_revision=retry_status["revision"],
    )
    assert result.committed_rows == 2
    assert service.status(project_id, draft_id)["state"] == "committed"


def test_failed_promotion_intent_rolls_back_before_reopening(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, status = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="promote-upload",
    )
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="none",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    service.freeze_for_commit(
        project_id,
        draft_id,
        idempotency_key="promotion-failure",
        expected_revision=status["revision"],
    )
    intake_root = workspace.get(project_id).path / ".imports" / "failed-promotion"
    intake_root.mkdir(parents=True)
    original_promote = service._promote_path
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("staged move failed")
        return original_promote(source, target)

    monkeypatch.setattr(service, "_promote_path", fail_second)
    with pytest.raises(RuntimeError, match="staged move failed"):
        service.promote_commit_inventory(
            project_id,
            draft_id,
            idempotency_key="promotion-failure",
            target_path=intake_root / "inventory.db",
        )

    reopened = service.reconcile_commit(
        project_id,
        draft_id,
        idempotency_key="promotion-failure",
        committed=False,
    )

    assert reopened["state"] == "open"
    assert not (intake_root / "inventory.db").exists()
    assert (workspace.root / ".pdf_import_drafts" / draft_id / "files").is_dir()


def test_page_uses_current_ocr_engine_and_settings_cache(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, _ = _declared(service, project_id)
    status = service.upload(
        project_id,
        draft_id,
        [_upload(), _upload("two.pdf")],
        file_ids=["one", "two"],
        logical_paths=["north/one.pdf", "south/two.pdf"],
        batch_id="cache-upload",
    )
    directory = workspace.root / ".pdf_import_drafts" / draft_id
    digest = hashlib.sha256(PDF).hexdigest()
    rapid_params = {"engine": "rapidocr"}
    llm_params = {"engine": "llm", "model": "openai/gpt-4.1-mini"}
    with PdfDraftInventory(directory / "inventory.db") as inventory:
        for params, text in ((rapid_params, "rapid text"), (llm_params, "llm text")):
            _settings, settings_hash = preview_settings(params)
            inventory.cache_ocr(
                digest,
                1,
                params["engine"],
                settings_hash,
                {"text": text, "blocks": [], "ocr_action_params": params},
            )

    assert service.page(project_id, draft_id, "one", 1)["ocr_text"] == "rapid text"
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="all",
        ocr_selection={
            "kind": "engine_model",
            "engine": "llm",
            "model": "openai/gpt-4.1-mini",
        },
        semantic_search=False,
        semantic_setup_confirmation=None,
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    assert status["choices"]["ocr_selection"]["engine"] == "llm"
    assert service.page(project_id, draft_id, "one", 1)["ocr_text"] == "llm text"


def test_cross_filesystem_commit_promotion_is_explicitly_refused(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "staged").write_bytes(b"staged")

    def cross_device(_source, _target):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(
        "frisket.server.services.pdf_imports.service.os.replace", cross_device
    )
    with pytest.raises(PdfImportDraftError, match="same filesystem"):
        PdfImportService._promote_path(source, target)
    assert (source / "staged").read_bytes() == b"staged"
    assert not target.exists()


def test_failed_first_freeze_removes_unpublished_projection(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, project_id, service = _service(tmp_path, monkeypatch)
    draft_id, status = _declared(service, project_id)
    status = service.update_choices(
        project_id,
        draft_id,
        expected_revision=status["revision"],
        ocr_policy="none",
        ocr_selection={"kind": "engine", "engine": "rapidocr"},
        semantic_search=True,
        semantic_setup_confirmation=status["semantic_setup"]["confirmation_hash"],
        destination={"kind": "new", "sheet_name": "PDFs"},
    )
    original_advance = service._advance_commit_inventory

    def fail_after_artifact(*args, **kwargs):
        original_advance(*args, **kwargs)
        raise RuntimeError("freeze interrupted")

    monkeypatch.setattr(service, "_advance_commit_inventory", fail_after_artifact)
    with pytest.raises(RuntimeError, match="freeze interrupted"):
        service.freeze_for_commit(
            project_id,
            draft_id,
            idempotency_key="freeze-failure",
            expected_revision=status["revision"],
        )
    directory = workspace.root / ".pdf_import_drafts" / draft_id
    assert service.status(project_id, draft_id)["state"] == "open"
    assert not (directory / "commit-inventory.db").exists()
    with sqlite3.connect(directory / "inventory.db") as db:
        assert db.execute("SELECT COUNT(*) FROM pdf_commit_projection").fetchone() == (
            0,
        )


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

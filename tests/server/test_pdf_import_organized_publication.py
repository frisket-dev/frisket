from __future__ import annotations

import hashlib
import io
import shutil

import pytest
from pypdf import PdfReader, PdfWriter

from frisket.engine.jobs import HandlerRegistry, Worker
from frisket.engine.jobs.pdf_split import register_pdf_split_handler
from frisket.engine.store.pdf_split_intake import pdf_split_status, read_pdf_split_plan
from frisket.server.services.pdf_import_commit import PdfImportCommitService
from frisket.server.services.pdf_imports.service import PdfImportService
from frisket.server.workspace import Workspace


def _pdf(pages):
    writer = PdfWriter()
    for page in range(pages):
        writer.add_blank_page(width=100 + page, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _stage(root, files):
    staging = root / "staging"
    (staging / "files").mkdir(parents=True)
    entries = []
    for index, (name, data, mime, extra) in enumerate(files):
        item = {
            "logical_path": f"{index}/{name}",
            "filename": name,
            "mime": mime,
            "size": 0 if data is None else len(data),
            **extra,
        }
        if data is not None:
            digest = hashlib.sha256(data).hexdigest()
            (staging / "files" / digest).write_bytes(data)
            item.update(sha256=digest, path=f"files/{digest}")
        entries.append(item)
    return staging, entries


def _admit(
    workspace,
    pid,
    root,
    files,
    *,
    organization="pages",
    destination=None,
    starts=None,
    ocr_policy="none",
    selection=None,
    preview=None,
    intake=None,
):
    service = intake or PdfImportService(workspace, preview_runs=preview)
    staging, entries = _stage(root, files)
    status = service.adopt_staged(pid, staging, entries)
    while status["analysis"]["files_done"] < status["analysis"]["files_total"]:
        status = service.analyze_staged(pid, status["draft_id"])
    choices = {
        **status["choices"],
        "organization": organization,
        "split_starts": starts or {},
        "ocr_policy": ocr_policy,
        "semantic_search": False,
        "semantic_setup_confirmation": None,
        "destination": destination or {"kind": "new", "sheet_name": "Organized"},
    }
    if selection is not None:
        choices["ocr_selection"] = selection
    status = service.update_choices(
        pid, status["draft_id"], expected_revision=status["revision"], **choices
    )
    if choices["destination"]["kind"] == "existing":
        status = service.match_destination(pid, status["draft_id"])
    return service, status


def _run(workspace, result):
    registry = HandlerRegistry()
    register_pdf_split_handler(
        registry, workspace_root=workspace.root, queue=workspace.queue
    )
    worker = Worker(workspace.queue, registry)
    for _ in range(10):
        job = workspace.queue.get(result.job_id)
        if job.status not in {"queued", "running"}:
            break
        assert worker.run_once()
    assert job.status == "done", job
    return pdf_split_status(workspace.get("uploads"), result.import_ref)


def test_final_upload_pages_materialize_eight_pdfs_after_draft_cleanup_and_replay(
    tmp_path,
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    service, status = _admit(
        workspace,
        pid,
        tmp_path,
        [
            ("first.pdf", _pdf(3), "application/pdf", {}),
            ("second.pdf", _pdf(5), "application/pdf", {}),
        ],
    )
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="final-search",
        expected_revision=status["revision"],
    )
    assert frozen.organization == "pages" and frozen.packet_publication
    committer = PdfImportCommitService(workspace, intake=service)
    result = committer.commit_frozen(pid, frozen)
    project = workspace.get(pid)
    assert project.visible_row_ids(result.sheet_id) == []
    assert service.status(pid, status["draft_id"])["state"] == "committed"
    shutil.rmtree(frozen.directory)
    terminal = _run(workspace, result)
    assert terminal["status"] == "completed"
    rows = project.visible_row_ids(result.sheet_id)
    assert len(rows) == 8
    columns = {
        column["name"]: column["id"] for column in project.columns(result.sheet_id)
    }
    names = project.get_values(result.sheet_id, columns["filename"], rows)
    assert list(names.values()) == ["first.pdf"] * 3 + ["second.pdf"] * 5
    media = project.get_values(result.sheet_id, columns["media"], rows)
    for index, row in enumerate(rows):
        with project.materialize_blob(media[row]["blob"]) as path:
            child = PdfReader(path)
            assert len(child.pages) == 1
            assert int(child.pages[0].mediabox.width) == 100 + (
                index if index < 3 else index - 3
            )
    replay = committer.commit_frozen(pid, frozen)
    assert (
        replay.import_ref == result.import_ref
        and replay.receipt_id == result.receipt_id
    )
    assert len(project.visible_row_ids(result.sheet_id)) == 8
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
        ).fetchone()[0]
        == 1
    )
    project.undo()
    assert project.visible_row_ids(result.sheet_id) == []
    project.redo()
    assert project.visible_row_ids(result.sheet_id) == rows


def test_final_upload_publishes_all_257_occurrences_across_inventory_pages(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    # Identical bytes are intentionally deduplicated in storage, while every
    # admitted filename remains a distinct source occurrence in manifest order.
    data = _pdf(1)
    filenames = [f"source-{index:03}.pdf" for index in range(257)]
    service, status = _admit(
        workspace,
        pid,
        tmp_path,
        [(name, data, "application/pdf", {}) for name in filenames],
    )
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="all-inventory-pages",
        expected_revision=status["revision"],
    )
    assert frozen.selected_total == frozen.committed_total == 257
    result = PdfImportCommitService(workspace, intake=service).commit_frozen(
        pid, frozen
    )
    terminal = _run(workspace, result)
    assert terminal["status"] == "completed"
    for key in (
        "selected_total",
        "source_count",
        "source_attempted",
        "source_succeeded",
        "expected_rows",
        "document_count",
    ):
        assert terminal[key] == 257, key
    assert terminal["source_failed"] == terminal["source_skipped"] == 0
    project = workspace.get(pid)
    rows = project.visible_row_ids(result.sheet_id)
    assert len(rows) == 257
    columns = {
        column["name"]: column["id"] for column in project.columns(result.sheet_id)
    }
    names = project.get_values(result.sheet_id, columns["filename"], rows)
    assert list(names.values()) == filenames
    final_media = project.get_values(result.sheet_id, columns["media"], [rows[-1]])
    with project.materialize_blob(final_media[rows[-1]]["blob"]) as path:
        assert len(PdfReader(path).pages) == 1
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
        ).fetchone()[0]
        == 1
    )
    project.undo()
    assert project.visible_row_ids(result.sheet_id) == []
    project.redo()
    assert project.visible_row_ids(result.sheet_id) == rows


def test_upload_split_requires_reviewed_starts_and_uses_local_ranges(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    service, status = _admit(
        workspace,
        pid,
        tmp_path,
        [("first.pdf", _pdf(3), "application/pdf", {})],
        organization="split_documents",
    )
    with pytest.raises(ValueError, match="every PDF"):
        service.freeze_for_commit(
            pid,
            status["draft_id"],
            idempotency_key="split",
            expected_revision=status["revision"],
        )
    from frisket.server.services.pdf_imports.inventory import PdfDraftInventory

    with PdfDraftInventory(
        service._root / status["draft_id"] / "inventory.db"
    ) as inventory:
        key = inventory.staged_page(after=0, limit=1)[0]["occurrence_id"]
    status = service.update_choices(
        pid,
        status["draft_id"],
        expected_revision=status["revision"],
        **{**status["choices"], "split_starts": {key: [1, 3]}},
    )
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="split",
        expected_revision=status["revision"],
    )
    result = PdfImportCommitService(workspace, intake=service).commit_frozen(
        pid, frozen
    )
    _run(workspace, result)
    project = workspace.get(pid)
    rows = project.visible_row_ids(result.sheet_id)
    media_column = next(
        column["id"]
        for column in project.columns(result.sheet_id)
        if column["name"] == "media"
    )
    lengths = []
    for value in project.get_values(result.sheet_id, media_column, rows).values():
        with project.materialize_blob(value["blob"]) as path:
            lengths.append(len(PdfReader(path).pages))
    assert lengths == [2, 1]


@pytest.mark.parametrize("organization", ["pages", "keep_documents"])
def test_matched_and_mixed_url_sources_keep_order_errors_and_original_identity(
    tmp_path, organization
):
    import json

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    project = workspace.get(pid)
    first, second = _pdf(3), _pdf(5)
    sheet = project.add_sheet("Archive")
    filename = project.add_column(sheet, "filename", type="text")
    size = project.add_column(sheet, "size", type="integer")
    originals = project.add_rows(
        sheet,
        [{"filename": "matched.pdf", "size": len(first)}],
        {"filename": filename, "size": size},
    )
    service, status = _admit(
        workspace,
        pid,
        tmp_path,
        [
            ("matched.pdf", first, "application/pdf", {}),
            (
                "notes.txt",
                b"notes",
                "text/plain",
                {
                    "source_url": "https://example.test/notes.txt",
                    "provider": "direct",
                    "metadata": {},
                },
            ),
            (
                "kept.pdf",
                second,
                "application/pdf",
                {
                    "source_url": "https://example.test/kept.pdf",
                    "provider": "direct",
                    "metadata": {"_media_acquisition_v1": {"title": "Original title"}},
                },
            ),
            (
                "failed.pdf",
                None,
                "application/octet-stream",
                {
                    "source_url": "https://example.test/failed.pdf",
                    "provider": "direct",
                    "acquisition_error": "Download failed",
                },
            ),
        ],
        organization=organization,
        destination={"kind": "existing", "sheet_id": sheet},
    )
    assert status["match"]["matched_count"] == 1
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="mixed",
        expected_revision=status["revision"],
    )
    result = PdfImportCommitService(workspace, intake=service).commit_frozen(
        pid, frozen
    )
    plan = read_pdf_split_plan(project.path / ".imports" / result.import_ref)
    assert [source["filename"] for source in plan["sources"]] == [
        "notes.txt",
        "kept.pdf",
        "failed.pdf",
    ]
    assert plan["sources"][-1]["sha256"] is None
    shutil.rmtree(frozen.directory)
    terminal = _run(workspace, result)
    assert terminal["source_failed"] == 1
    assert terminal["skipped_duplicates"] == 1
    rows = project.visible_row_ids(sheet)
    expected_pdf_rows = 5 if organization == "pages" else 1
    assert len(rows) == len(originals) + expected_pdf_rows + 2
    assert project.get_values(sheet, filename, originals)[originals[0]] == "matched.pdf"
    columns = {column["name"]: column["id"] for column in project.columns(sheet)}
    names = project.get_values(sheet, columns[plan["roles"]["filename"]], rows[1:])
    assert [names[row] for row in rows[1:]] == ["notes.txt"] + [
        "kept.pdf"
    ] * expected_pdf_rows + ["failed.pdf"]
    columns = {column["name"]: column["id"] for column in project.columns(sheet)}
    media = project.get_values(sheet, columns["media"], rows)
    assert not media.get(rows[-1])
    assert (
        project.get_values(sheet, columns["error"], [rows[-1]])[rows[-1]]
        == "Download failed"
    )
    assert (
        project.get_values(sheet, columns["source"], [rows[-1]])[rows[-1]]
        == "https://example.test/failed.pdf"
    )
    if organization == "keep_documents":
        assert media[rows[2]]["blob"] == hashlib.sha256(second).hexdigest()
    retained = project.db.execute(
        "SELECT source_url,metadata FROM blobs WHERE hash=?",
        (hashlib.sha256(second).hexdigest(),),
    ).fetchone()
    assert retained["source_url"] == "https://example.test/kept.pdf"
    assert (
        json.loads(retained["metadata"])["_media_acquisition_v1"]["title"]
        == "Original title"
    )
    project.undo()
    assert project.visible_row_ids(sheet) == originals


@pytest.mark.parametrize("cache_first_page", [False, True])
def test_organized_paid_ocr_persists_exact_page_claims_across_choice_revision(
    tmp_path, monkeypatch, cache_first_page
):
    from tests.server.test_pdf_split_preparation import _runtime
    from frisket.server.services.pdf_imports.storage import PdfImportDraftError

    adapter, preview, project, provider, _read, _op, _source = _runtime(
        tmp_path / "paid", monkeypatch
    )
    workspace = adapter.workspace
    try:
        selection = {"kind": "engine", "engine": "anthropic/claude-haiku-4-5"}
        service, status = _admit(
            workspace,
            "pages",
            tmp_path,
            [("paid.pdf", _pdf(3), "application/pdf", {})],
            ocr_policy="all",
            selection=selection,
            preview=preview,
        )
        estimate = service.estimate_batch_ocr(
            "pages",
            status["draft_id"],
            expected_revision=status["revision"],
            ocr_policy="all",
            ocr_selection=selection,
        )
        assert estimate["known_pages"] == 3 and estimate["selected_count"] == 1
        assert estimate["estimate"]["requires_confirmation"]
        if cache_first_page:
            from frisket.server.services.pdf_imports.inventory import PdfDraftInventory
            from frisket.server.services.pdf_imports.previews import preview_settings

            _, settings_hash = preview_settings({"engine": selection["engine"]})
            with PdfDraftInventory(
                service._root / status["draft_id"] / "inventory.db"
            ) as inventory:
                row = inventory.staged_page(after=0, limit=1)[0]
                inventory.cache_ocr(
                    row["sha256"],
                    1,
                    selection["engine"],
                    settings_hash,
                    {
                        "text": "Accepted sample",
                        "engine": selection["engine"],
                        "ocr_action_params": {"engine": selection["engine"]},
                        "blocks": [],
                    },
                )
        status = service.status("pages", status["draft_id"])
        with pytest.raises(PdfImportDraftError, match="exact claims"):
            service.freeze_for_commit(
                "pages",
                status["draft_id"],
                idempotency_key="paid",
                expected_revision=status["revision"],
                ocr_confirmation="unrelated",
            )
        # Search consent updates the draft revision, without changing the paid
        # original-page work set or forcing another OCR quote.
        status = service.update_choices(
            "pages",
            status["draft_id"],
            expected_revision=status["revision"],
            **status["choices"],
        )
        restarted = PdfImportService(workspace, preview_runs=preview)
        frozen = restarted.freeze_for_commit(
            "pages",
            status["draft_id"],
            idempotency_key="paid",
            expected_revision=status["revision"],
            ocr_confirmation=estimate["estimate"]["promise_set_hash"],
        )
        assert frozen.ocr_authorization is None
        facts = frozen.pdf_preparation
        assert facts["kind"] == "pdf_split_source_pages"
        grant = facts["ocr_authorization"]
        assert (
            grant["scope"]["schema_version"] == "frisket.pdf_source_page_ocr_scope.v1"
        )
        assert [target["page_number"] for target in grant["scope"]["targets"]] == (
            [2, 3] if cache_first_page else [1, 2, 3]
        )
        assert (
            grant["allocation"]["approval_parent_hash"]
            == estimate["estimate"]["promise_set_hash"]
        )
        assert provider.calls == 0
        from frisket.engine.store.import_intake import read_import_header

        result = PdfImportCommitService(workspace, intake=restarted).commit_frozen(
            "pages", frozen
        )
        durable = read_import_header(project.path / ".imports" / result.import_ref)
        assert durable.pdf_preparation["ocr_authorization"] == grant
        assert (
            durable.pdf_preparation["ocr_action_params"] == facts["ocr_action_params"]
        )
        assert durable.pdf_preparation["kind"] == "pdf_split_source_pages"
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


@pytest.mark.parametrize("text_source", ["native", "sample_ocr"])
def test_organized_publication_stages_known_page_text_before_draft_cleanup(
    tmp_path, monkeypatch, text_source
):
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    from frisket.engine.jobs import pdf_split as split_jobs
    from frisket.server.services.pdf_imports.inventory import PdfDraftInventory
    from frisket.server.services.pdf_imports.previews import preview_settings

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for index in range(3):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(
            f"BT /F1 12 Tf 72 720 Td (Known original page {index + 1}) Tj ET".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    payload = io.BytesIO()
    writer.write(payload)
    data = payload.getvalue()
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    engine = "anthropic/claude-haiku-4-5" if text_source == "sample_ocr" else "rapidocr"
    service, status = _admit(
        workspace,
        pid,
        tmp_path,
        [("known.pdf", data, "application/pdf", {})],
        ocr_policy="all" if text_source == "sample_ocr" else "missing",
        selection={"kind": "engine", "engine": engine},
    )
    if text_source == "sample_ocr":
        # This is the exact settings form written by the shipped upload sample
        # reader, before the page coordinator normalizes its OcrParams defaults.
        _, settings_hash = preview_settings({"engine": engine})
        with PdfDraftInventory(
            service._root / status["draft_id"] / "inventory.db"
        ) as inventory:
            for page in range(1, 4):
                inventory.cache_ocr(
                    hashlib.sha256(data).hexdigest(),
                    page,
                    engine,
                    settings_hash,
                    {
                        "page": page,
                        "text": f"Accepted OCR page {page}",
                        "engine": engine,
                        "ocr_action_params": {"engine": engine},
                        "blocks": [],
                    },
                )
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="known",
        expected_revision=status["revision"],
    )
    assert frozen.pdf_preparation is None
    assert bool(frozen.native_pages) is (text_source == "native")
    result = PdfImportCommitService(workspace, intake=service).commit_frozen(
        pid, frozen
    )
    shutil.rmtree(frozen.directory)

    def forbid_read(*_args, **_kwargs):
        raise AssertionError("publication reread known original pages")

    monkeypatch.setattr(split_jobs, "extract_pdf_text_batch", forbid_read)
    terminal = _run(workspace, result)
    assert terminal["source_succeeded"] == 1
    project = workspace.get(pid)
    rows = project.visible_row_ids(result.sheet_id)
    text_column = next(
        column["id"]
        for column in project.columns(result.sheet_id)
        if column["name"] == "text"
    )
    values = project.get_values(result.sheet_id, text_column, rows)
    expected = (
        "Accepted OCR page" if text_source == "sample_ocr" else "Known original page"
    )
    assert [values[row].strip() for row in rows] == [
        f"{expected} {page}" for page in range(1, 4)
    ]


def test_final_http_commit_publishes_reviewed_pages_on_the_ordinary_import_status(
    tmp_path,
):
    from fastapi.testclient import TestClient
    from frisket.server.app import create_app
    from frisket.server.services.pdf_import_commit import PdfImportCommitResult

    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Uploads"}).json()["id"]
    workspace = client.app.state.workspace
    service, status = _admit(
        workspace,
        pid,
        tmp_path,
        [
            ("first.pdf", _pdf(3), "application/pdf", {}),
            ("second.pdf", _pdf(5), "application/pdf", {}),
        ],
        intake=client.app.state.pdf_import_service,
    )
    response = client.post(
        f"/api/projects/{pid}/import/pdf-imports/{status['draft_id']}/commit",
        json={
            "idempotency_key": "final-http-search",
            "expected_revision": status["revision"],
        },
    )
    assert response.status_code == 202, response.text
    result = PdfImportCommitResult(**response.json()["job"])
    assert result.job_id and result.import_ref
    # The public final action replays its durable admission without publishing
    # another import session or adding another history operation.
    replay = client.post(
        f"/api/projects/{pid}/import/pdf-imports/{status['draft_id']}/commit",
        json={
            "idempotency_key": "final-http-search",
            "expected_revision": status["revision"],
        },
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["job"]["import_ref"] == result.import_ref
    registry = HandlerRegistry()
    register_pdf_split_handler(
        registry, workspace_root=workspace.root, queue=workspace.queue
    )
    worker = Worker(workspace.queue, registry)
    for _ in range(10):
        if workspace.queue.get(result.job_id).status == "done":
            break
        assert worker.run_once()
    progress = client.get(
        f"/api/projects/{pid}/import/files/sessions/{result.import_ref}"
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["committed_rows"] == 8
    project = workspace.get(pid)
    assert len(project.visible_row_ids(result.sheet_id)) == 8

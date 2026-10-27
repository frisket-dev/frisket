from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pypdf import PdfReader, PdfWriter

from frisket.contracts.http.pdf_packet_splits import (
    PdfPacketCommitRequest,
    PdfPacketDestination,
    PdfPacketSheetCreateRequest,
)
from frisket.engine.jobs import HandlerRegistry, Worker
from frisket.engine.store.cell_writes import BaseCellWrite, replace_base_cells
from frisket.engine.store.import_sessions import ImportInProgress
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.ocr_word_stream import resolve_ocr_word_stream
from frisket.engine.store.pdf_split_intake import pdf_split_status
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.import_intake import request_import_cancel, import_intake_dir
import pytest
from frisket.pdf_packets.source_map import SourceMap, SourceOccurrence
from frisket.server.services.pdf_packet_commit import PdfPacketCommitter
from frisket.server.services.pdf_packet_commit import PdfSplitPassthrough
from frisket.server.services.pdf_packet_scope import PdfPacketScopeService
from frisket.server.workspace import Workspace
from frisket.server.services.import_sessions import ImportSessionService
from frisket.server.routes.import_sessions import register_import_session_routes
from frisket.server.route_errors import register_route_error_handler


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for page in range(pages):
        writer.add_blank_page(width=100 + page, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _scope(workspace, project_id):
    project = workspace.get(project_id)
    sheet = project.add_sheet("Originals")
    file_column = project.add_column(sheet, "File", type="file")
    case_column = project.add_column(sheet, "case", type="text")
    private_column = project.add_column(sheet, "private", type="text", hidden=True)
    records = []
    for index, count in enumerate((3, 5)):
        digest = project.add_blob(
            _pdf(count), filename=f"source-{index}.pdf", mime="application/pdf"
        )
        records.append(
            {
                "File": {"blob": digest},
                "case": f"case-{index}",
                "private": "not selected",
            }
        )
    rows = project.add_rows(
        sheet,
        records,
        {"File": file_column, "case": case_column, "private": private_column},
    )
    admitted = PdfPacketScopeService(workspace).admit(
        project_id,
        PdfPacketSheetCreateRequest(
            sheet_id=sheet,
            file_column_id=file_column,
            organization="pages",
            scope="all",
        ),
    )
    source_map = SourceMap(
        tuple(
            SourceOccurrence(
                source.key, source.sha256, source.page_start, source.page_end
            )
            for source in admitted.sources
        )
    )
    return project, sheet, rows, case_column, admitted.sources, source_map


def test_existing_sheet_destination_contract_defaults_legacy_controls():
    request = PdfPacketCommitRequest(
        idempotency_key="replace",
        confirmed_starts=[1],
        destination={"kind": "replace", "sheet_id": 12},
        carried_column_ids=[23],
    )
    assert request.destination.sheet_id == 12
    assert request.carried_column_ids == [23]
    assert request.name_pattern
    assert request.keep_ocr_text is False


def test_durable_apply_materializes_three_plus_five_pages_and_replaces_once(
    tmp_path: Path,
):
    from frisket.engine.jobs.pdf_split import register_pdf_split_handler

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, originals, case_column, sources, source_map = _scope(
        workspace, project_id
    )
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=source_map.page_ranges(),
        destination=PdfPacketDestination(kind="replace", sheet_id=sheet),
        carried_column_ids=[case_column],
        idempotency_key="replace-pages",
        request_context=None,
    )
    assert started["import_ref"] and started["receipt_id"]
    assert isinstance(started["job_id"], int)
    assert project.visible_row_ids(sheet) == originals
    original_file_column = int(project.columns(sheet)[0]["id"])
    original_producer = int(
        project.db.execute(
            "SELECT producer_id FROM cells WHERE row_id=? AND column_id=?",
            (originals[0], original_file_column),
        ).fetchone()[0]
    )
    project.db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ImportInProgress):
            replace_base_cells(
                project.db,
                producer_id=original_producer,
                column_ids=[original_file_column],
                row_ids=[originals[0]],
                cells=[
                    BaseCellWrite(
                        originals[0], original_file_column, {"blob": "f" * 64}
                    )
                ],
            )
    finally:
        project.db.rollback()
    # A fresh handler reads only the frozen durable plan; it does not borrow
    # a live packet preview session or its in-memory source openers.
    registry = HandlerRegistry()
    register_pdf_split_handler(
        registry, workspace_root=workspace.root, queue=workspace.queue
    )
    worker = Worker(workspace.queue, registry)
    for _ in range(10):
        job = workspace.queue.get(started["job_id"])
        if job.status not in {"queued", "running"}:
            break
        assert worker.run_once()
    assert job.status == "done", job
    children = project.visible_row_ids(sheet)
    assert len(children) == 8, [
        tuple(row)
        for row in project.db.execute(
            "SELECT state,error FROM import_session_pdf_sources"
        )
    ]
    assert all(row not in children for row in originals)
    filename_column = next(
        column["id"]
        for column in project.columns(sheet)
        if column["name"] == "filename"
    )
    filenames = project.get_values(sheet, filename_column, children)
    assert [filenames[row] for row in children] == ["source-0.pdf"] * 3 + [
        "source-1.pdf"
    ] * 5
    case_values = project.get_values(sheet, case_column, children)
    assert [case_values[row] for row in children] == ["case-0"] * 3 + ["case-1"] * 5
    source_cards = project.db.execute(
        "SELECT external_ref_json FROM source_artifacts WHERE json_extract(external_ref_json,'$.kind')='pdf_packet_split_source' ORDER BY id"
    ).fetchall()
    assert [json.loads(row[0])["sibling_count"] for row in source_cards] == [3, 5]
    assert all(
        project.db.execute(
            "SELECT parent_row_id FROM rows WHERE id=?", (row,)
        ).fetchone()[0]
        is None
        for row in children
    )
    file_column = next(
        column["id"] for column in project.columns(sheet) if column["name"] == "File"
    )
    values = project.get_values(sheet, file_column, children)
    for index, row in enumerate(children):
        with project.materialize_blob(values[row]["blob"]) as path:
            child = PdfReader(path)
            assert len(child.pages) == 1
            assert int(child.pages[0].mediabox.width) == 100 + (
                index if index < 3 else index - 3
            )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
        ).fetchone()[0]
        == 1
    )
    project.undo()
    assert project.visible_row_ids(sheet) == originals
    project.redo()
    assert project.visible_row_ids(sheet) == children


def _run(workspace, result, **kwargs):
    from frisket.engine.jobs.pdf_split import register_pdf_split_handler

    registry = HandlerRegistry()
    register_pdf_split_handler(
        registry, workspace_root=workspace.root, queue=workspace.queue, **kwargs
    )
    worker = Worker(workspace.queue, registry)
    for _ in range(10):
        job = workspace.queue.get(result["job_id"])
        if job.status not in {"queued", "running"}:
            break
        assert worker.run_once()
    assert job.status == "done", job
    return pdf_split_status(workspace.get("apply"), result["import_ref"])


def test_new_sheet_uses_admission_context_snapshot_and_replays_without_reading(
    tmp_path,
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, rows, case_column, sources, source_map = _scope(
        workspace, project_id
    )
    committer = PdfPacketCommitter(workspace)
    kwargs = dict(
        sources=sources,
        source_map=source_map,
        ranges=source_map.page_ranges(),
        destination=PdfPacketDestination(kind="new_sheet", name="Children"),
        carried_column_ids=[case_column],
        idempotency_key="new",
    )
    started = committer.start(project_id, **kwargs)
    project.apply_edits(
        [{"row_id": rows[0], "column_id": case_column, "value": "changed later"}]
    )
    cursor = project.op_cursor
    result = _run(workspace, started)
    assert result["status"] == "completed"
    assert project.op_cursor == cursor
    assert project.visible_row_ids(sheet) == rows
    children = project.visible_row_ids(started["sheet_id"])
    copied = next(
        column["id"]
        for column in project.columns(started["sheet_id"])
        if column["name"] == "case"
    )
    assert (
        list(project.get_values(started["sheet_id"], copied, children).values())
        == ["case-0"] * 3 + ["case-1"] * 5
    )
    assert not any(
        column["name"] == "private" for column in project.columns(started["sheet_id"])
    )

    def forbidden():
        raise AssertionError("idempotent replay reread input")

    replay = committer.start(
        project_id,
        **{
            **kwargs,
            "sources": tuple(
                replace(source, open_source=forbidden) for source in sources
            ),
        },
    )
    assert replay["import_ref"] == started["import_ref"]
    assert replay["receipt_id"] == started["receipt_id"]
    assert replay["job_id"] == started["job_id"]


def test_stop_between_sources_keeps_one_atomic_group_and_one_undo(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, rows, _, sources, source_map = _scope(workspace, project_id)
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=source_map.page_ranges(),
        destination=PdfPacketDestination(kind="replace", sheet_id=sheet),
        idempotency_key="stopped",
    )
    from frisket.engine.jobs import pdf_split as split_jobs

    original = split_jobs._source_plan

    def stop_second(
        project, project_id, directory, ordinal, source, roles, stopped, progress
    ):
        if ordinal == 1:
            request_import_cancel(directory)
        yield from original(
            project, project_id, directory, ordinal, source, roles, stopped, progress
        )

    # Control a real source boundary; publication, staging and cancellation
    # remain the actual store/queue behavior.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(split_jobs, "_source_plan", stop_second)
        result = _run(workspace, started)
    assert result["status"] == "partial"
    assert result["source_succeeded"] == 1
    assert result["source_skipped"] == 1
    visible = project.visible_row_ids(sheet)
    assert len(visible) == 4 and rows[1] == visible[-1]
    project.undo()
    assert project.visible_row_ids(sheet) == rows
    project.redo()
    assert project.visible_row_ids(sheet) == visible


def test_prepared_pages_rebase_without_native_reread_and_keep_original_facts(
    tmp_path, monkeypatch
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, rows, _, sources, _ = _scope(workspace, project_id)
    source = sources[0]
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=source.sha256,
        filename=source.filename,
        page_count=3,
        source_sheet_id=sheet,
        source_row_id=rows[0],
        source_column_id=source.source_column_id,
        metadata={
            "engine": "test-ocr",
            "ocr_token_granularity": "block",
            "page_images": {},
        },
    )
    operation = project.db.execute(
        "SELECT op_id FROM base_cell_producers WHERE id=(SELECT producer_id FROM cells WHERE row_id=? LIMIT 1)",
        (rows[0],),
    ).fetchone()[0]
    project.db.execute("BEGIN IMMEDIATE")
    saved = PreparedContentStore(project).stage_reference(
        source_artifact_id=artifact["id"],
        producing_op_id=operation,
        pages=[
            PreparedPageDraft(
                page,
                f"page {page}",
                {
                    "engine": "test-ocr",
                    "blocks": [
                        {
                            "text": f"page {page}",
                            "bbox": {
                                "space": "page_normalized",
                                "x0": 0.1,
                                "y0": 0.1,
                                "x1": 0.8,
                                "y1": 0.2,
                            },
                        }
                    ],
                },
            )
            for page in (1, 2, 3)
        ],
    )
    project.db.commit()
    source = replace(source, prepared_ref_id=saved.ref_id, prepared_pages=(1, 2, 3))
    source_map = SourceMap([SourceOccurrence(source.key, source.sha256, 1, 3)])

    async def no_reread(*args, **kwargs):
        raise AssertionError("prepared source pages were reread")

    monkeypatch.setattr(
        "frisket.engine.jobs.pdf_split.extract_pdf_text_batch", no_reread
    )
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=[source],
        source_map=source_map,
        ranges=source_map.ranges([1, 3]),
        destination=PdfPacketDestination(kind="new_sheet", name="Prepared children"),
        organization="split_documents",
        idempotency_key="prepared",
    )
    assert _run(workspace, started)["status"] == "completed"
    children = project.visible_row_ids(started["sheet_id"])
    text_column = next(
        column["id"]
        for column in project.columns(started["sheet_id"])
        if column["name"] == "text"
    )
    assert list(
        project.get_values(started["sheet_id"], text_column, children).values()
    ) == ["page 1\n\npage 2", "page 3"]
    refs = list(
        project.db.execute(
            "SELECT value FROM cells WHERE column_id=? AND value_kind='prepared_content_ref' ORDER BY row_id",
            (text_column,),
        )
    )
    assert [
        tuple(
            pin.page_number
            for pin in PreparedContentStore(project).resolve(row[0]).pins
        )
        for row in refs
    ] == [(1, 2), (1,)]
    originals = project.db.execute(
        "SELECT source_sheet_id,source_row_id,source_column_id FROM source_artifacts WHERE blob_hash=?",
        (source.sha256,),
    ).fetchall()
    assert all(
        tuple(item) == (sheet, rows[0], source.source_column_id) for item in originals
    )
    file_column = next(
        column["id"]
        for column in project.columns(started["sheet_id"])
        if column["name"] == "media"
    )
    file_values = project.get_values(started["sheet_id"], file_column, children)
    for child, pages in zip(children, [(1, 2), (1,)], strict=True):
        stream = resolve_ocr_word_stream(project, file_values[child]["blob"])
        assert stream is not None and stream.token_granularity == "block"
        assert tuple(token.page for token in stream.stream.tokens) == pages


def test_failed_url_is_error_row_without_fake_blob_and_reports_partial(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    error = PdfSplitPassthrough(
        key="url:one",
        sha256=None,
        filename="unavailable.pdf",
        size=0,
        mime="application/pdf",
        open_source=None,
        source_url="https://example.invalid/file.pdf",
        error="The URL could not be downloaded.",
    )
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=(),
        source_map=None,
        ranges=(),
        passthrough=[error],
        destination=PdfPacketDestination(kind="new_sheet", name="Errors"),
        idempotency_key="failed-url",
    )
    result = _run(workspace, started)
    assert result["status"] == "partial"
    assert result["source_failed"] == 1 and result["source_succeeded"] == 0
    project = workspace.get(project_id)
    assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    columns = {
        column["name"]: column["id"] for column in project.columns(started["sheet_id"])
    }
    row = project.visible_row_ids(started["sheet_id"])[0]
    assert (
        project.get_values(started["sheet_id"], columns["media"], [row]).get(row)
        is None
    )

    assert (
        project.get_values(started["sheet_id"], columns["source"], [row])[row]
        == error.source_url
    )


def _progress_client(workspace):
    app = FastAPI()
    register_route_error_handler(app)
    register_import_session_routes(
        app,
        service=ImportSessionService(workspace),
        max_rows_for_request=lambda pid, request: None,
    )
    return TestClient(app)


def test_standard_import_status_lists_split_and_cancels_queued_writer(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, rows, _, sources, source_map = _scope(workspace, project_id)
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=source_map.page_ranges(),
        destination=PdfPacketDestination(kind="replace", sheet_id=sheet),
        idempotency_key="status",
    )
    client = _progress_client(workspace)
    base = f"/api/projects/{project_id}/import/files/sessions"
    status = client.get(f"{base}/{started['import_ref']}")
    assert status.status_code == 200, status.text
    assert status.json()["admitted_files"] == 2
    assert status.json()["estimated_rows"] == 8
    assert status.json()["sheet_id"] == sheet
    assert started["import_ref"] in [
        item["import_ref"] for item in client.get(base).json()["sessions"]
    ]
    stopped = client.post(f"{base}/{started['import_ref']}/cancel")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "cancelled"
    assert stopped.json()["committed_rows"] == 0
    assert project.visible_row_ids(sheet) == rows
    removed = client.post(
        f"{base}/{started['import_ref']}/resolve", json={"decision": "remove"}
    )
    assert removed.status_code == 409 and "Undo" in removed.text
    kept = client.post(
        f"{base}/{started['import_ref']}/resolve", json={"decision": "keep"}
    )
    assert kept.status_code == 200 and kept.json()["state"] == "kept"
    assert not (
        import_intake_dir(project.path, started["import_ref"]) / "inventory.db"
    ).exists()


def test_standard_stop_during_real_split_worker_retains_completed_source(
    tmp_path, monkeypatch
):
    from frisket.engine.jobs.pdf_split import register_pdf_split_handler
    from frisket.engine.jobs import pdf_split as split_jobs

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, rows, _, sources, source_map = _scope(workspace, project_id)
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=source_map.page_ranges(),
        destination=PdfPacketDestination(kind="replace", sheet_id=sheet),
        idempotency_key="active-stop",
    )
    client = _progress_client(workspace)
    base = f"/api/projects/{project_id}/import/files/sessions"
    original = split_jobs._source_plan

    def stop_second(
        project, project_id, directory, ordinal, source, roles, stopped, progress
    ):
        if ordinal == 1:
            response = client.post(f"{base}/{started['import_ref']}/cancel")
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "cancelling"
        yield from original(
            project, project_id, directory, ordinal, source, roles, stopped, progress
        )

    monkeypatch.setattr(split_jobs, "_source_plan", stop_second)
    registry = HandlerRegistry()
    register_pdf_split_handler(
        registry, workspace_root=workspace.root, queue=workspace.queue
    )
    assert Worker(workspace.queue, registry).run_once()
    response = client.get(f"{base}/{started['import_ref']}")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "kept"
    assert response.json()["committed_rows"] == 3
    assert response.json()["through"] == 1
    visible = project.visible_row_ids(sheet)
    assert len(visible) == 4 and rows[1] == visible[-1]
    assert started["import_ref"] in [
        item["import_ref"] for item in client.get(base).json()["sessions"]
    ]
    assert (
        client.post(
            f"{base}/{started['import_ref']}/resolve", json={"decision": "remove"}
        ).status_code
        == 409
    )
    project.undo()
    assert project.visible_row_ids(sheet) == rows
    project.redo()
    assert project.visible_row_ids(sheet) == visible


def test_keep_preserves_source_order_original_bytes_caches_and_matching_counts(
    tmp_path, monkeypatch
):
    from PIL import Image

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, _, _, _, sources, source_map = _scope(workspace, project_id)
    sources = (
        replace(
            sources[0],
            source_url="https://example.test/original.pdf",
            provider="url",
            metadata={"acquisition": {"kind": "url"}},
        ),
        sources[1],
    )
    pixels = io.BytesIO()
    Image.new("RGB", (4, 4)).save(pixels, format="PNG")
    image_bytes = pixels.getvalue()
    image_hash = project.add_blob(image_bytes, filename="photo.png", mime="image/png")
    image = PdfSplitPassthrough(
        "image:one",
        image_hash,
        "photo.png",
        len(image_bytes),
        "image/png",
        None,
        page_count=1,
    )
    error = PdfSplitPassthrough(
        "url:error",
        None,
        "missing.pdf",
        0,
        "application/pdf",
        None,
        source_url="https://example.test/missing.pdf",
        error="The URL could not be downloaded.",
    )
    cache_root = tmp_path / "review-native"
    cache_root.mkdir()
    native = {}
    for ordinal, source in enumerate(sources):
        native[source.key] = {}
        for page in range(1, source.page_count + 1):
            path = cache_root / f"{ordinal}-{page}.json"
            path.write_text(
                json.dumps(
                    {
                        "text": f"PDF {ordinal} page {page}",
                        "engine": "native",
                        "positions": None,
                    }
                )
            )
            native[source.key][page] = path

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "keeping cached originals must not rewrite or reread pages"
        )

    monkeypatch.setattr("frisket.engine.jobs.pdf_split.write_pdf_range", forbidden)
    monkeypatch.setattr(
        "frisket.engine.jobs.pdf_split.extract_pdf_text_batch", forbidden
    )
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=source_map.ranges(source_map.fixed_starts),
        destination=PdfPacketDestination(kind="new_sheet", name="Mixed keep"),
        idempotency_key="mixed-keep",
        preserve_originals=True,
        organization="split_documents",
        passthrough=[image, error],
        source_order=[sources[0].key, error.key, image.key, sources[1].key],
        skipped_duplicates=2,
        native_pages=native,
        cached_pages={
            image.key: {
                1: {
                    "text": "image text",
                    "engine": "test-ocr",
                    "positions": {"engine": "test-ocr", "blocks": []},
                }
            }
        },
    )
    for path in cache_root.iterdir():
        path.unlink()
    result = _run(workspace, started)
    assert result["status"] == "partial" and result["source_failed"] == 1
    assert result["skipped_duplicates"] == 2 and result["selected_total"] == 6
    columns = {
        column["name"]: column["id"] for column in project.columns(started["sheet_id"])
    }
    rows = project.visible_row_ids(started["sheet_id"])
    assert list(
        project.get_values(started["sheet_id"], columns["filename"], rows).values()
    ) == [sources[0].filename, error.filename, image.filename, sources[1].filename]
    media = project.get_values(started["sheet_id"], columns["media"], rows)
    assert [(media.get(row) or {}).get("blob") for row in rows] == [
        sources[0].sha256,
        None,
        image_hash,
        sources[1].sha256,
    ]
    assert (
        project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0] == 0
    )
    texts = project.get_values(started["sheet_id"], columns["text"], rows)
    assert texts[rows[0]] == "PDF 0 page 1\n\nPDF 0 page 2\n\nPDF 0 page 3"
    assert texts[rows[2]] == "image text"
    status = ImportSessionService(workspace).status(project_id, started["import_ref"])
    assert status.state == "kept" and status.problem_count == 1
    assert (
        status.admitted_files == 6
        and status.through == 6
        and status.skipped_duplicates == 2
    )


def test_resume_recovers_admission_before_queue_handoff_without_draft_input(
    tmp_path, monkeypatch
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, _, _, _, sources, source_map = _scope(workspace, project_id)
    committer = PdfPacketCommitter(workspace)
    enqueue = workspace.queue.enqueue

    def unavailable(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(workspace.queue, "enqueue", unavailable)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        committer.start(
            project_id,
            sources=sources,
            source_map=source_map,
            ranges=source_map.page_ranges(),
            destination=PdfPacketDestination(kind="new_sheet", name="Recover"),
            idempotency_key="resume",
        )
    monkeypatch.setattr(workspace.queue, "enqueue", enqueue)
    result = committer.resume(project_id, idempotency_key="resume")
    assert result is not None and isinstance(result["job_id"], int)
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
        ).fetchone()[0]
        == 1
    )
    assert _run(workspace, result)["document_count"] == 8
    assert committer.resume(project_id, idempotency_key="not-admitted") is None


def test_durable_source_publication_rolls_back_rows_and_lineage_on_write_failure(
    tmp_path, monkeypatch
):
    from frisket.engine.store import pdf_split

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, original_sheet, originals, _, sources, source_map = _scope(
        workspace, project_id
    )
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=source_map.page_ranges(),
        destination=PdfPacketDestination(kind="new_sheet", name="Failed output"),
        idempotency_key="write-failure",
    )
    publish = pdf_split.publish_import_blobs

    def fail_after_real_publication(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("forced publication failure")

    monkeypatch.setattr(pdf_split, "publish_import_blobs", fail_after_real_publication)
    result = _run(workspace, started)
    assert result["status"] == "failed"
    assert result["document_count"] == 0 and result["source_failed"] == 2
    assert project.visible_row_ids(original_sheet) == originals
    assert project.visible_row_ids(started["sheet_id"]) == []
    for table in (
        "source_artifacts",
        "evidence_links",
        "blob_derivations",
        "import_session_rows",
    ):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (started["receipt_id"],)
    ).fetchone()
    assert receipt[0] == "failed"


@pytest.mark.parametrize(
    "pattern", ["{packet.__class__}", "{index:100000000}", "{packet"]
)
def test_shared_committer_refuses_unsafe_filename_placeholders(tmp_path, pattern):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Apply", project_id="apply")["id"])
    project, sheet, _, _, sources, source_map = _scope(workspace, project_id)
    with pytest.raises(ValueError, match="name pattern"):
        PdfPacketCommitter(workspace).start(
            project_id,
            sources=sources,
            source_map=source_map,
            ranges=source_map.page_ranges(),
            destination=PdfPacketDestination(kind="new_sheet", name="Unsafe"),
            idempotency_key="pattern",
            name_pattern=pattern,
        )
    assert [row["id"] for row in project.sheets()] == [sheet]
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

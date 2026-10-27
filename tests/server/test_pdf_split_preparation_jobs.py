from __future__ import annotations

from dataclasses import replace

import threading

import pytest

from frisket.contracts.http.pdf_packet_splits import PdfPacketDestination
from frisket.engine.jobs import HandlerRegistry, Worker
from frisket.engine.jobs.pdf_split import register_pdf_split_handler
from frisket.engine.store.document_text import resolve_document_text_selection
from frisket.engine.store.import_intake import import_intake_dir
from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.pdf_packets.source_map import SourceMap, SourceOccurrence
from frisket.server.services.pdf_packet_commit import PdfPacketCommitter
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.services.pdf_split_preparation import PdfSplitPreparationStore
from frisket.server.services.search_preparation import SearchPreparationService
from tests.deterministic_time import controlled_time
from tests.server.test_pdf_split_preparation import _runtime


@pytest.mark.parametrize("outcome", ["success", "existing", "provider_failure", "stop"])
def test_actual_queue_worker_prepares_original_page_and_publishes_child_under_split(
    tmp_path, monkeypatch, outcome
):
    adapter, preview, project, provider, read, _op, original = _runtime(
        tmp_path, monkeypatch
    )
    workspace = adapter.workspace
    with original.open_source() as source_path:
        source_size = source_path.stat().st_size
    source = AdmittedPacketSource(
        "first",
        read.blob_hash.removeprefix("sha256:"),
        "original.pdf",
        source_size,
        3,
        2,
        2,
        original.open_source,
    )
    if outcome == "existing":
        with original.open_source() as path:
            digest = project.add_blob(
                path.read_bytes(), filename="original.pdf", mime="application/pdf"
            )
        original_sheet = project.add_sheet("Originals")
        original_column = project.add_column(original_sheet, "File", type="file")
        value = {"blob": digest, "page": 2}
        original_row = project.add_rows(
            original_sheet, [{"File": value}], {"File": original_column}
        )[0]
        source = replace(
            source,
            sha256=digest,
            source_sheet_id=original_sheet,
            source_row_id=original_row,
            source_column_id=original_column,
            frozen_value=value,
        )
        read = replace(read, source_artifact_id=None, source_column_id=original_column)
        adapter.admitted_sources[source.key] = source
    mapping = SourceMap((SourceOccurrence("first", source.sha256, 2, 2),))

    class Destination(PdfPacketDestination):
        sheet_id: int | None = None

    quote = adapter.quote(targets=[read], plan_token="reviewed-plan")
    adapter.approve(quote, confirmation=quote["estimate"]["promise_set_hash"])
    facts = {
        "kind": "pdf_split_source_pages",
        "plan_token": "reviewed-plan",
        "ocr_action_params": dict(read.ocr_action_params),
        "ocr_authorization": quote["authorization"],
        "semantic_search": True,
        "semantic_setup_confirmation": "reviewed-semantic-download",
    }
    preparation = SearchPreparationService(workspace, preview_runs=preview)
    setup_calls = []

    def semantic_setup(project_id, **facts):
        setup_calls.append((project_id, facts))

    monkeypatch.setattr(preparation, "_ensure_pdf_semantic_setup", semantic_setup)
    entered, release = threading.Event(), threading.Event()
    complete = provider.complete
    if outcome == "provider_failure":

        async def failed(request, client):
            provider.calls += 1
            raise RuntimeError("OCR provider is unavailable")

        provider.complete = failed
    elif outcome == "stop":

        async def blocked(request, client):
            entered.set()
            assert release.wait(10)
            return await complete(request, client)

        provider.complete = blocked
    try:
        started = PdfPacketCommitter(workspace).start(
            "pages",
            sources=[source],
            source_map=mapping,
            ranges=mapping.page_ranges(),
            destination=Destination(kind="new_sheet", name="Pages"),
            idempotency_key="prepared-split",
            pdf_preparation=facts,
        )
        registry = HandlerRegistry()
        register_pdf_split_handler(
            registry,
            workspace_root=workspace.root,
            queue=workspace.queue,
            on_terminal_import=lambda project_id, receipt_id, sheet_id: (
                preparation.create_after_import(
                    project_id, receipt_id=receipt_id, sheet_id=sheet_id
                )
            ),
        )
        worker = Worker(workspace.queue, registry)
        assert worker.run_once()
        job = workspace.queue.get(started["job_id"])
        assert job.status == "done", job
        import_ref = started["import_ref"]
        if outcome not in {"success", "existing"}:
            if outcome == "stop":
                assert entered.wait(5)
                stopped = preparation.stop_pdf_import("pages", import_ref)
                assert stopped.stopped is True
                assert stopped.scheduled_through == 0
                assert stopped.children[0]["status"] == "stopping"
                release.set()
            with controlled_time(timeout=15) as clock:
                clock.wait_until(
                    lambda: all(
                        child["status"] in {"failed", "cancelled"}
                        for child in preparation.pdf_import_summary(
                            "pages", import_ref
                        ).children
                    ),
                    message="source-page preparation did not become terminal",
                )
            summary = preparation.pdf_import_summary("pages", import_ref)
            assert summary.ocr_total == 1
            assert summary.ocr_completed == 0
            assert summary.stopped is (outcome == "stop")
            assert bool(summary.error) is (outcome == "provider_failure")
            directory = import_intake_dir(project.path, import_ref)
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                source_work = store.db.execute(
                    "SELECT status,prepared_ref_id FROM pdf_split_page_work"
                ).fetchone()
                if outcome == "stop":
                    assert source_work["status"] == "cancelled"
                    cached = PreparedContentStore(project).resolve(
                        source_work["prepared_ref_id"]
                    )
                    assert cached.text == "Read page"
                    assert cached.covered_pages == (2,)
                else:
                    assert source_work["prepared_ref_id"] is None
                assert (
                    store.db.execute(
                        "SELECT COUNT(*) FROM pdf_split_page_bindings WHERE published=0"
                    ).fetchone()[0]
                    == 0
                )
            cursor = project.db.execute(
                "SELECT cursor FROM import_sessions WHERE id=?", (import_ref,)
            ).fetchone()[0]
            assert preparation.progress_pdf_import("pages", import_ref) == cursor
            return
        with controlled_time(timeout=15) as clock:
            clock.wait_until(
                lambda: (
                    preparation.pdf_import_summary("pages", import_ref).ocr_completed
                    == 1
                ),
                message="durable split OCR did not reach child Text",
            )
        directory = import_intake_dir(project.path, import_ref)
        with PdfSplitPreparationStore(directory / "inventory.db") as store:
            source_work = store.db.execute(
                "SELECT * FROM pdf_split_page_work"
            ).fetchone()
            assert source_work["status"] == "completed"
            assert source_work["receipt_id"]
            bindings = store.db.execute(
                "SELECT published FROM pdf_split_page_bindings"
            ).fetchall()
            assert [row[0] for row in bindings] == [1]
        session = project.db.execute(
            "SELECT * FROM import_sessions WHERE id=?", (import_ref,)
        ).fetchone()
        row = project.db.execute(
            "SELECT row_id FROM import_session_rows WHERE session_id=?", (import_ref,)
        ).fetchone()[0]
        role = project.db.execute(
            "SELECT role.source_column_id FROM document_text_column_roles role JOIN columns column ON column.id=role.source_column_id WHERE column.sheet_id=?",
            (session["sheet_id"],),
        ).fetchone()[0]
        selected = resolve_document_text_selection(
            project, (session["sheet_id"], row, role)
        )
        content = PreparedContentStore(project).resolve(selected.prepared_ref_id)
        assert content.text == "Read page"
        assert content.covered_pages == (1,)
        assert content.complete is True
        assert content.pins[0].positions["source_page_number"] == 2
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM ops WHERE kind='media.ocr'"
            ).fetchone()[0]
            == 1
        )  # earlier explicit preview op only
        assert provider.calls == 1
        assert setup_calls[0][0] == "pages"
        assert setup_calls[0][1]["enabled"] is True
        assert setup_calls[0][1]["confirmation"] == "reviewed-semantic-download"
        assert setup_calls[0][1]["import_receipt_id"] == started["receipt_id"]
        project.undo()
        assert project.visible_row_ids(session["sheet_id"]) == []
        project.redo()
        assert (
            PreparedContentStore(project)
            .resolve(
                resolve_document_text_selection(
                    project, (session["sheet_id"], row, role)
                ).prepared_ref_id
            )
            .text
            == "Read page"
        )
    finally:
        release.set()
        preview._registry.shutdown()
        project.close()

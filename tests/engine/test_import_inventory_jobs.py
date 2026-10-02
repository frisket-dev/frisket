from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    reserve_typed_action_job,
)
from frisket.engine.jobs import (
    HandlerRegistry,
    JobHandlerContext,
    SqliteJobQueue,
    Worker,
)
from frisket.engine.jobs.import_files import register_import_files_handler
from frisket.engine.jobs.search_index import register_search_index_handler
from frisket.engine.jobs.queue import BLOB_METADATA_KIND, IMPORT_FILES_PAGE_KIND
from frisket.engine.store import Project
from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    import_intake_dir,
    read_import_header,
    write_import_header,
)
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.services.import_sessions import ImportSessionService
from frisket.server.services.sheet_grid import SheetGridService

REF = "import-" + "a" * 32


def setup_import(root, *, count=5, sealed=False):
    with closing(Project.create(root / "files.frisket")) as project:
        bound = typed_action_for_request(
            {
                "action_id": "import.files",
                "scope": {"kind": "project"},
                "sheet_name": "Documents",
                "params": {"inventory_ref": REF},
                "idempotency_key": REF,
            }
        )
        envelope = reserve_typed_action_job(project, "files", bound)
        assert isinstance(envelope, ActionJobEnvelope)
        directory = import_intake_dir(project.path, REF)
        directory.mkdir(parents=True)
        write_import_header(
            directory,
            ImportIntakeHeader("files", project.storage_identity, envelope.to_json()),
        )
        digest = project.blob_store.put(b"original")
        with ImportInventory(directory / "inventory.db") as inventory:
            inventory.append(
                {
                    "logical_path": f"{i}.txt",
                    "mime": "text/plain",
                    "sha256": digest,
                    "size": 8,
                    "kind": "files",
                }
                for i in range(count)
            )
            if sealed:
                inventory.seal()
        return envelope, directory


def registry_for(root, queue):
    registry = HandlerRegistry()
    register_import_files_handler(registry, workspace_root=root, queue=queue)
    register_search_index_handler(registry, workspace_root=root, queue=queue)
    return registry


def run_until_settled(worker, queue, job_id):
    for _ in range(20):
        if queue.get(job_id).status not in {"queued", "running"}:
            return
        assert worker.run_once()
    pytest.fail(f"job {job_id} did not settle")


def run_until_attempted(worker, queue, job_id):
    for _ in range(20):
        if queue.get(job_id).attempts:
            return
        worker.run_once()
    pytest.fail(f"job {job_id} was not attempted")


def enqueue(queue, through):
    return queue.enqueue(
        IMPORT_FILES_PAGE_KIND,
        {"project_id": "files", "import_ref": REF, "through": through},
    )


def test_worker_consumes_uploaded_cursor_then_seal_finalizes_same_receipt(tmp_path):
    envelope, directory = setup_import(tmp_path)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 2)
        run_until_settled(worker, queue, first)
        assert queue.get(first).status == "done", queue.get(first).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.cursor == session.committed_rows == 2
            assert (
                ReceiptStore(project).find_by_id(envelope.receipt_id).status
                == "running"
            )
        second = enqueue(queue, 5)
        run_until_settled(worker, queue, second)
        assert queue.get(second).status == "done", queue.get(second).error
        with ImportInventory(directory / "inventory.db") as inventory:
            inventory.seal()
        final = enqueue(queue, 5)
        run_until_settled(worker, queue, final)
        assert queue.get(final).status == "done", queue.get(final).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "completed"
            assert session.committed_rows == project.row_count(session.sheet_id) == 5
            assert (
                ReceiptStore(project).find_by_id(envelope.receipt_id).status
                == "completed"
            )
            assert (
                project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
            )


def test_cancel_intent_stops_next_job_without_deleting_committed_rows(tmp_path):
    _envelope, directory = setup_import(tmp_path)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 2)
        run_until_settled(worker, queue, first)
        write_import_header(
            directory, replace(read_import_header(directory), cancel_requested=True)
        )
        cancelled = enqueue(queue, 5)
        run_until_settled(worker, queue, cancelled)
        assert queue.get(cancelled).status == "done", queue.get(cancelled).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "cancelled"
            assert project.row_count(session.sheet_id) == 2


def test_completed_retry_schedules_metadata_without_reimporting(tmp_path, monkeypatch):
    _envelope, directory = setup_import(tmp_path, sealed=True)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        original_enqueue = queue.enqueue

        def fail_metadata(kind, *args, **kwargs):
            if kind == BLOB_METADATA_KIND:
                raise OSError("temporary queue failure")
            return original_enqueue(kind, *args, **kwargs)

        monkeypatch.setattr(queue, "enqueue", fail_metadata)
        worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 5)
        run_until_attempted(worker, queue, first)
        assert queue.get(first).status != "done"
        monkeypatch.setattr(queue, "enqueue", original_enqueue)
        retry = enqueue(queue, 5)
        run_until_settled(worker, queue, retry)
        assert queue.get(retry).status == "done", queue.get(retry).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "completed"
            assert project.row_count(session.sheet_id) == 5
        with ImportInventory(directory / "inventory.db") as inventory:
            assert inventory.totals() == {"count": 5, "bytes": 40}
            assert inventory.through == 5
            assert inventory.page(limit=1) == []
        stale = enqueue(queue, 5)
        while queue.get(stale).status == "queued":
            assert worker.run_once()
        assert queue.get(stale).status == "done", queue.get(stale).error
        assert any(job.kind == BLOB_METADATA_KIND for job in queue.list_jobs())


def test_large_resume_yields_to_another_queue_claim(tmp_path):
    setup_import(tmp_path, count=1030, sealed=True)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 1030)
        run_until_settled(worker, queue, first)
        assert queue.get(first).status == "done", queue.get(first).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "active"
            assert session.committed_rows == 1024
        assert worker.run_once()
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "completed"
            assert session.committed_rows == project.row_count(session.sheet_id) == 1030


@pytest.mark.parametrize("decision", ["keep", "remove"])
def test_multi_page_cancel_retry_and_resolution_preserve_exact_partial_state(
    tmp_path, decision
):
    envelope, _directory = setup_import(tmp_path, count=600, sealed=True)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        first_worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 512)
        run_until_settled(first_worker, queue, first)
        assert queue.get(first).status == "done", queue.get(first).error

        with closing(Project(tmp_path / "files.frisket")) as project:
            workspace = SimpleNamespace(get=lambda _project_id: project)
            service = ImportSessionService(workspace)
            browser = SheetGridService(workspace)
            session = ImportSessionStore(project).get(REF)
            assert session.cursor == session.committed_rows == 512
            assert project.row_count(session.sheet_id) == 512
            source_column = next(
                int(column["id"])
                for column in project.columns(session.sheet_id)
                if column["type"] == "file"
            )
            page = browser.document_page(
                "files", session.sheet_id, source_column_id=source_column, limit=100
            )
            assert len(page["items"]) == 100
            assert [item["ordinal"] for item in page["items"]] == list(range(1, 101))

            before_retry = {
                "rows": project.row_count(session.sheet_id),
                "cells": project.db.execute("SELECT count(*) FROM cells").fetchone()[0],
                "ops": project.db.execute("SELECT count(*) FROM ops").fetchone()[0],
                "runs": project.db.execute("SELECT count(*) FROM runs").fetchone()[0],
            }
            cancelled = service.cancel("files", REF)
            assert cancelled.state == "cancelled" and cancelled.committed_rows == 512

        # A new worker represents process restart. A stale/retried wakeup must
        # observe the terminal session without publishing the remaining page.
        retry_worker = Worker(queue, registry_for(tmp_path, queue))
        retry = enqueue(queue, 600)
        run_until_settled(retry_worker, queue, retry)
        assert queue.get(retry).status == "done", queue.get(retry).error

        with closing(Project(tmp_path / "files.frisket")) as project:
            workspace = SimpleNamespace(get=lambda _project_id: project)
            service = ImportSessionService(workspace)
            browser = SheetGridService(workspace)
            session = ImportSessionStore(project).get(REF)
            assert session.state == "cancelled" and session.committed_rows == 512
            assert {
                "rows": project.row_count(session.sheet_id),
                "cells": project.db.execute("SELECT count(*) FROM cells").fetchone()[0],
                "ops": project.db.execute("SELECT count(*) FROM ops").fetchone()[0],
                "runs": project.db.execute("SELECT count(*) FROM runs").fetchone()[0],
            } == before_retry
            receipt = ReceiptStore(project).find_by_id(envelope.receipt_id)
            assert receipt is not None and len(receipt.parsed().op_ids) == 1

            resolved = service.resolve("files", REF, decision=decision)
            assert resolved.state == ("kept" if decision == "keep" else "removed")
            if decision == "keep":
                assert project.row_count(session.sheet_id) == 512
                kept = browser.document_page(
                    "files",
                    session.sheet_id,
                    source_column_id=source_column,
                    limit=100,
                )
                assert len(kept["items"]) == 100
                assert (
                    ReceiptStore(project).find_by_id(envelope.receipt_id).status
                    == "partial"
                )
            else:
                assert project.row_count(session.sheet_id) == 0
                removed = browser.document_page(
                    "files",
                    session.sheet_id,
                    source_column_id=source_column,
                    limit=100,
                )
                assert removed["items"] == []
                assert (
                    ReceiptStore(project).find_by_id(envelope.receipt_id).status
                    == "cancelled"
                )


def test_job_refuses_payload_authority_and_another_project_inventory(tmp_path):
    _envelope, directory = setup_import(tmp_path)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = registry_for(tmp_path, queue)
        with pytest.raises(ValueError, match="row-backed"):
            registry.get(IMPORT_FILES_PAGE_KIND)(
                {
                    "project_id": "files",
                    "import_ref": REF,
                    "through": 5,
                    "handler_authority_id": "forged",
                },
                JobHandlerContext.without_job_row(),
            )
        write_import_header(
            directory, replace(read_import_header(directory), storage_identity="other")
        )
        job = queue.get(enqueue(queue, 5))
        run_until_attempted(Worker(queue, registry), queue, job.id)
        assert queue.get(job.id).status != "done"
        with closing(Project(tmp_path / "files.frisket")) as project:
            assert project.sheets() == []

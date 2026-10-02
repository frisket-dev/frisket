from contextlib import closing
from dataclasses import replace

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
    return registry


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
        assert worker.run_once()
        assert queue.get(first).status == "done", queue.get(first).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.cursor == session.committed_rows == 2
            assert (
                ReceiptStore(project).find_by_id(envelope.receipt_id).status
                == "running"
            )
        second = enqueue(queue, 5)
        assert worker.run_once()
        assert queue.get(second).status == "done", queue.get(second).error
        with ImportInventory(directory / "inventory.db") as inventory:
            inventory.seal()
        final = enqueue(queue, 5)
        assert worker.run_once()
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
        enqueue(queue, 2)
        worker.run_once()
        write_import_header(
            directory, replace(read_import_header(directory), cancel_requested=True)
        )
        cancelled = enqueue(queue, 5)
        worker.run_once()
        assert queue.get(cancelled).status == "done", queue.get(cancelled).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "cancelled"
            assert project.row_count(session.sheet_id) == 2


def test_completed_retry_schedules_metadata_without_reimporting(tmp_path, monkeypatch):
    setup_import(tmp_path, sealed=True)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        original_enqueue = queue.enqueue

        def fail_metadata(kind, *args, **kwargs):
            if kind == BLOB_METADATA_KIND:
                raise OSError("temporary queue failure")
            return original_enqueue(kind, *args, **kwargs)

        monkeypatch.setattr(queue, "enqueue", fail_metadata)
        worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 5)
        worker.run_once()
        assert queue.get(first).status != "done"
        monkeypatch.setattr(queue, "enqueue", original_enqueue)
        retry = enqueue(queue, 5)
        worker.run_once()
        assert queue.get(retry).status == "done", queue.get(retry).error
        with closing(Project(tmp_path / "files.frisket")) as project:
            session = ImportSessionStore(project).get(REF)
            assert session.state == "completed"
            assert project.row_count(session.sheet_id) == 5
        assert any(job.kind == BLOB_METADATA_KIND for job in queue.list_jobs())


def test_large_resume_yields_to_another_queue_claim(tmp_path):
    setup_import(tmp_path, count=1030, sealed=True)
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        worker = Worker(queue, registry_for(tmp_path, queue))
        first = enqueue(queue, 1030)
        worker.run_once()
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
        Worker(queue, registry).run_once()
        assert queue.get(job.id).status != "done"
        with closing(Project(tmp_path / "files.frisket")) as project:
            assert project.sheets() == []

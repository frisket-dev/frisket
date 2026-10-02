"""Consume admitted file inventories on the existing project job queue."""

from __future__ import annotations

import logging
from pathlib import Path

from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_jobs import ActionJobEnvelope, bind_typed_action_job
from frisket.engine.executor.file_inventory_read import FileInventoryAdmission
from frisket.engine.executor.table_action import run_inventory_table_page
from frisket.engine.store import Project
from frisket.engine.store.import_intake import (
    import_intake_dir,
    import_worker_lock,
    read_import_header,
)
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.project_identity import ProjectStorageKey

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import (
    BLOB_METADATA_KIND,
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    IMPORT_FILES_PAGE_KIND,
    JobQueue,
    claimed_project_location,
)
from .worker import HandlerRegistration, HandlerRegistry

_PAGES_PER_CLAIM = 4


def register_import_files_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, context: JobHandlerContext) -> dict:
        if (
            context.job_id is None
            or context.job_attempt is None
            or not context.handler_authority_id
        ):
            raise ValueError("file import requires a row-backed queue claim")

        def live() -> bool:
            current = queue.get(context.job_id)
            return (
                current is not None
                and current.status == "running"
                and current.attempts == context.job_attempt
            )

        project_id, project_root, path = claimed_project_location(
            payload,
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            require_storage_identity=require_storage_identity
            or project_opener is not None,
        )
        ref = payload["import_ref"]
        through = payload["through"]
        if type(through) is not int or through < 0:
            raise ValueError("import job requires a nonnegative admitted cursor")
        project = (
            Project(path)
            if project_opener is None
            else open_claimed_project(payload, path, project_opener)
        )
        sessions = ImportSessionStore(project)

        def project_payload() -> dict:
            continuation = {
                "project_id": project_id,
                "workspace_root": str(project_root),
            }
            claimed = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
            if isinstance(claimed, ProjectStorageKey):
                continuation["storage_org_id"] = claimed.storage_org_id
            if isinstance(context.trusted_job_org_id, int):
                continuation["org_id"] = context.trusted_job_org_id
            return continuation

        def schedule_metadata(receipt_id: str) -> None:
            continuation = project_payload()
            continuation["dedupe_key"] = f"blob-metadata:{receipt_id}"
            queue.enqueue(BLOB_METADATA_KIND, continuation)

        try:
            directory = import_intake_dir(project.path, ref)
            # Concurrent upload batches may enqueue this session more than once.
            # Serialize consumption, not uploads or project browsing. A timeout
            # uses ordinary queue retry; never drop a wakeup because a job is busy.
            with import_worker_lock(directory, timeout=5):
                if not live():
                    return {"status": "superseded"}
                header = read_import_header(directory)
                if (
                    header.project_id != project_id
                    or header.storage_identity != project.storage_identity
                ):
                    raise ValueError("import inventory belongs to another project")
                envelope = ActionJobEnvelope.from_json(header.envelope)
                bound = bind_typed_action_job(envelope, project=project)
                if isinstance(bound, ActionResult):
                    raise ValueError("import inventory request no longer validates")
                if getattr(bound.params, "inventory_ref", None) != ref:
                    raise ValueError("import request names a different inventory")
                stored = ReceiptStore(project).find_by_id(envelope.receipt_id)
                if stored is None:
                    raise ValueError("import reservation is missing")
                session = sessions.get(ref)
                if session is not None and session.state in {
                    "cancelled",
                    "kept",
                    "removed",
                    "completed",
                }:
                    if session.state in {"completed", "kept"}:
                        schedule_metadata(envelope.receipt_id)
                    return {
                        "status": session.state,
                        "completed_rows": session.committed_rows,
                    }
                if stored.status == "queued":
                    ReceiptStore(project).update_body_status(
                        stored.parsed().model_copy(update={"status": "running"}),
                        require_status="queued",
                    )
                elif stored.status != "running":
                    return {"status": stored.status}
                if session is not None:
                    StreamingSheetWriter.resume_session(
                        project,
                        session_id=ref,
                        writer_authority=context.handler_authority_id,
                        expected_cursor=session.cursor,
                    )
                with ImportInventory(directory / "inventory.db") as inventory:
                    totals = inventory.totals()
                    if (
                        through > totals["count"]
                        or (
                            header.max_rows is not None
                            and totals["count"] > header.max_rows
                        )
                        or (
                            header.max_bytes is not None
                            and totals["bytes"] > header.max_bytes
                        )
                    ):
                        raise ValueError(
                            "import job exceeds its admitted inventory or row limit"
                        )
                    pages = 0
                    while True:
                        session = sessions.get(ref)
                        cursor = session.cursor if session else 0
                        header = read_import_header(directory)
                        if header.cancel_requested or not live():
                            if session is not None:
                                writer = StreamingSheetWriter._from_session(
                                    project, session
                                )
                                if header.cancel_requested:
                                    writer.cancel(expected_cursor=cursor)
                                else:
                                    writer.pause(expected_cursor=cursor)
                            elif header.cancel_requested:
                                stored = ReceiptStore(project).find_by_id(
                                    envelope.receipt_id
                                )
                                ReceiptStore(project).update_body_status(
                                    stored.parsed().model_copy(
                                        update={"status": "cancelled"}
                                    ),
                                    require_status="running",
                                )
                            return {
                                "status": "cancelled"
                                if header.cancel_requested
                                else "paused",
                                "completed_rows": session.committed_rows
                                if session
                                else 0,
                            }
                        # An empty final page completes an import sealed after
                        # the last upload job already published its rows.
                        final_empty = (
                            inventory.sealed and cursor == inventory.totals()["count"]
                        )
                        if cursor >= through and not final_empty:
                            return {
                                "status": "admitting",
                                "completed_rows": session.committed_rows
                                if session
                                else 0,
                            }
                        if pages >= _PAGES_PER_CLAIM:
                            continuation = project_payload()
                            continuation.update(
                                import_ref=ref,
                                through=through,
                                dedupe_key=f"import:{ref}:{cursor}:{through}:{inventory.sealed}",
                            )
                            queue.enqueue(IMPORT_FILES_PAGE_KIND, continuation)
                            return {
                                "status": "running",
                                "completed_rows": session.committed_rows,
                            }
                        session = run_inventory_table_page(
                            project,
                            project_id,
                            bound,
                            admission=FileInventoryAdmission(
                                ref,
                                inventory,
                                project.blob_store,
                                cursor,
                                limit=max(1, min(256, through - cursor)),
                            ),
                            session_id=ref,
                            writer_authority=context.handler_authority_id,
                            reserved_action_id=envelope.action_id,
                            reserved_receipt_id=envelope.receipt_id,
                        )
                        pages += 1
                        if session.state == "completed":
                            schedule_metadata(envelope.receipt_id)
                            return {
                                "status": "completed",
                                "completed_rows": session.committed_rows,
                                "sheet_id": session.sheet_id,
                            }
        except Exception:
            session = sessions.get(ref)
            if (
                session is not None
                and session.state == "active"
                and session.writer_authority == context.handler_authority_id
            ):
                StreamingSheetWriter._from_session(project, session).pause(
                    expected_cursor=session.cursor
                )
            raise
        finally:
            if live():
                from .search_index import enqueue_search_index

                try:
                    enqueue_search_index(project, queue, project_payload())
                except Exception:
                    logging.getLogger(__name__).warning(
                        "search_index_enqueue_failed", exc_info=True
                    )
            project.close()

    return registry.add(
        IMPORT_FILES_PAGE_KIND, handle, origin="frisket.production.import_files"
    )

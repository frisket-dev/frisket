"""Bounded multipart intake for resumable ``import.files`` sessions."""

from __future__ import annotations

import inspect
import logging
import secrets
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionResult
from frisket.contracts.http.import_sessions import (
    ImportSessionList,
    ImportSessionStatus,
)
from frisket.engine.executor.action_jobs import (
    action_job_cancelled_result,
    reserve_typed_action_job,
)
from frisket.engine.jobs.queue import IMPORT_FILES_PAGE_KIND
from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    append_inventory_batch,
    compact_terminal_inventory,
    import_admit_lock,
    import_intake_dir,
    import_worker_lock,
    inventory_batch_through,
    inventory_item_from_staged,
    read_import_header,
    request_import_cancel,
    set_import_resolution,
    write_import_header,
)
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk_types import (
    BulkImportLimits,
    BulkUpload,
    ImportBulkRouteError,
)
from frisket.server.services.import_uploads import upload_sheet_name


_MAX_CHUNK_FILES = 256
_MULTI_FILE_BATCH_BYTES = 64 * 1024 * 1024
logger = logging.getLogger(__name__)
EnqueueImport = Callable[[str, str, int, bool], Awaitable[None] | None]


def import_page_dedupe_key(
    project_id: str, ref: str, through: int, sealed: bool
) -> str:
    return f"import-files-page:{project_id}:{ref}:{through}:{int(sealed)}"


class ImportSessionService:
    def __init__(
        self,
        workspace: Any,
        *,
        limits: BulkImportLimits | None = None,
        enqueue: EnqueueImport | None = None,
    ) -> None:
        self._workspace = workspace
        self._limits = limits or BulkImportLimits()
        self._enqueue = enqueue

    def create(
        self, project_id: str, sheet_name: str, *, max_rows: int | None = None
    ) -> ImportSessionStatus:
        project = self._workspace.get(project_id)
        if max_rows is not None and (
            isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")
        configured_files = self._limits.max_upload_files
        if configured_files is not None:
            max_rows = (
                configured_files
                if max_rows is None
                else min(max_rows, configured_files)
            )
        root = Path(project.path) / ".imports"
        root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(root / ".create.lock")):
            while True:
                ref = f"import-{secrets.token_hex(16)}"
                directory = import_intake_dir(project.path, ref)
                try:
                    directory.mkdir()
                    break
                except FileExistsError:
                    continue
            try:
                request_key = f"import-session:{ref}"
                final_name = self._available_sheet_name(
                    project, root, request_key, sheet_name
                )
                action = {
                    "action_id": "import.files",
                    "scope": {"kind": "project"},
                    "sheet_name": final_name,
                    "params": {"inventory_ref": ref},
                    "output_names": {},
                    "idempotency_key": request_key,
                }
                bound = typed_action_for_request(action)
                envelope = reserve_typed_action_job(project, project_id, bound)
                if isinstance(envelope, ActionResult):
                    raise RuntimeError("new import session unexpectedly replayed")
                header = ImportIntakeHeader(
                    project_id=project_id,
                    storage_identity=project.storage_identity,
                    envelope=envelope.to_json(),
                    max_rows=max_rows,
                    max_bytes=self._limits.max_upload_bytes,
                )
                write_import_header(directory, header)
                ImportInventory(directory / "inventory.db").close()
                return self._status(project, ref, directory, header)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    async def upload(
        self,
        project_id: str,
        ref: str,
        uploads: list[BulkUpload],
        *,
        batch_id: str,
    ) -> ImportSessionStatus:
        if not uploads or len(uploads) > _MAX_CHUNK_FILES:
            raise ImportBulkRouteError(413, "upload chunk must contain 1..256 files")
        project, directory, header = self._open(project_id, ref)
        with import_admit_lock(directory):
            header = self._validated_header(project, project_id, directory)
            if header.cancel_requested:
                raise ValueError("cancelled import does not accept more files")
            inventory = ImportInventory(directory / "inventory.db")
            scratch = directory / f".batch-{secrets.token_hex(8)}"
            (scratch / "files").mkdir(parents=True)
            try:
                staged = await import_bulk_sources.stage_uploads(
                    uploads, scratch, False, self._chunk_limits(len(uploads))
                )
                facts = [inventory_item_from_staged(item) for item in staged]
                retry_through = inventory_batch_through(inventory, batch_id, facts)
                if retry_through is not None:
                    through = retry_through
                else:
                    if inventory.sealed:
                        raise ValueError("sealed import does not accept more files")
                    totals = inventory.totals()
                    if (
                        header.max_rows is not None
                        and totals["count"] + len(staged) > header.max_rows
                    ):
                        raise ImportBulkRouteError(
                            413, "upload file count exceeds deployment limit"
                        )
                    batch_bytes = sum(int(item["size"]) for item in staged)
                    if (
                        header.max_bytes is not None
                        and totals["bytes"] + batch_bytes > header.max_bytes
                    ):
                        raise ImportBulkRouteError(
                            413, "upload bytes exceeds deployment limit"
                        )
                    stored: set[str] = set()
                    for item in staged:
                        digest = str(item["sha256"])
                        if digest not in stored:
                            observed = project.blob_store.put_path(
                                scratch / str(item["path"]), expected_digest=digest
                            )
                            if observed != digest:
                                raise ValueError("canonical blob digest changed")
                            stored.add(digest)
                    through, _added = append_inventory_batch(
                        inventory,
                        batch_id,
                        facts,
                    )
                status = self._status(project, ref, directory, header, inventory)
            finally:
                inventory.close()
                shutil.rmtree(scratch, ignore_errors=True)
        await self._enqueue_page(project_id, ref, through, False)
        return status

    async def seal(self, project_id: str, ref: str) -> ImportSessionStatus:
        project, directory, _header = self._open(project_id, ref)
        with import_admit_lock(directory):
            header = self._validated_header(project, project_id, directory)
            with ImportInventory(directory / "inventory.db") as inventory:
                inventory.seal()
                status = self._status(project, ref, directory, header, inventory)
                through = status.through
        await self._enqueue_page(project_id, ref, through, True)
        return status

    def status(self, project_id: str, ref: str) -> ImportSessionStatus:
        project, directory, header = self._open(project_id, ref)
        with ImportInventory(directory / "inventory.db") as inventory:
            return self._status(project, ref, directory, header, inventory)

    def cancel(self, project_id: str, ref: str) -> ImportSessionStatus:
        project, directory, _header = self._open(project_id, ref)
        with import_admit_lock(directory):
            header = self._validated_header(project, project_id, directory)
            header = request_import_cancel(directory)
        try:
            with import_worker_lock(directory, timeout=0):
                session = ImportSessionStore(project).get(ref)
                if session is not None and session.state in ("active", "paused"):
                    StreamingSheetWriter._from_session(project, session).cancel(
                        expected_cursor=session.cursor
                    )
                elif session is None:
                    action_job_cancelled_result(
                        project,
                        project_id=project_id,
                        receipt_id=str(header.envelope["receipt_id"]),
                        action_kind="import.files",
                    )
        except Timeout:
            pass
        with ImportInventory(directory / "inventory.db") as inventory:
            return self._status(project, ref, directory, header, inventory)

    def resolve(
        self, project_id: str, ref: str, *, decision: str
    ) -> ImportSessionStatus:
        if decision not in {"keep", "remove"}:
            raise ValueError("decision must be keep or remove")
        project, directory, header = self._open(project_id, ref)
        with import_worker_lock(directory, timeout=0):
            with import_admit_lock(directory):
                session = ImportSessionStore(project).get(ref)
                if session is None:
                    if not header.cancel_requested:
                        raise ValueError("only a cancelled import can be resolved")
                    header = set_import_resolution(
                        directory, "kept" if decision == "keep" else "removed"
                    )
                elif session.state in ("kept", "removed"):
                    expected = "kept" if decision == "keep" else "removed"
                    if session.state != expected:
                        raise ValueError(
                            "cancelled import was already resolved differently"
                        )
                elif session.state != "cancelled":
                    raise ValueError("only a cancelled import can be resolved")
                else:
                    writer = StreamingSheetWriter._from_session(project, session)
                    if decision == "keep":
                        writer.finalize_session(
                            expected_cursor=session.cursor, keep_cancelled=True
                        )
                    else:
                        writer.remove_session(expected_cursor=session.cursor)
                try:
                    compact_terminal_inventory(directory)
                except Exception:
                    logger.warning(
                        "terminal import inventory compaction failed",
                        exc_info=True,
                    )
        with ImportInventory(directory / "inventory.db") as inventory:
            status = self._status(project, ref, directory, header, inventory)
        if session is not None and decision == "keep":
            # The last page job may already have exited before Cancel/Keep.
            # Reuse the workspace's normal import trigger and enqueue recovery.
            schedule_metadata = getattr(
                project, "_frisket_schedule_blob_metadata", None
            )
            if callable(schedule_metadata):
                schedule_metadata(str(header.envelope["receipt_id"]))
        return status

    async def resume(self, project_id: str, ref: str) -> ImportSessionStatus:
        project, directory, header = self._open(project_id, ref)
        session = ImportSessionStore(project).get(ref)
        if session is not None and session.state not in ("active", "paused"):
            raise ValueError("import session cannot be resumed")
        if header.cancel_requested:
            raise ValueError("cancelled import requires keep or remove")
        with ImportInventory(directory / "inventory.db") as inventory:
            status = self._status(project, ref, directory, header, inventory)
        await self._enqueue_page(project_id, ref, status.through, status.sealed)
        return status

    def list(self, project_id: str) -> ImportSessionList:
        project = self._workspace.get(project_id)
        root = Path(project.path) / ".imports"
        sessions = []
        db_states = {
            str(row["id"]): str(row["state"])
            for row in project.db.execute("SELECT id,state FROM import_sessions")
        }
        recent_completed = {
            str(row["id"])
            for row in project.db.execute(
                "SELECT id FROM import_sessions WHERE state='completed' "
                "ORDER BY updated_at DESC LIMIT 10"
            )
        }
        if root.is_dir():
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                try:
                    ref = directory.name
                    header = self._validated_header(project, project_id, directory)
                    db_state = db_states.get(ref)
                    if db_state in {"kept", "removed"} or (
                        db_state == "completed" and ref not in recent_completed
                    ):
                        continue
                    if header.resolution in {"kept", "removed"}:
                        continue
                    with ImportInventory(directory / "inventory.db") as inventory:
                        sessions.append(
                            self._status(project, ref, directory, header, inventory)
                        )
                except (KeyError, OSError, ValueError):
                    continue
        return ImportSessionList(sessions=sessions)

    def _open(self, project_id: str, ref: str):
        project = self._workspace.get(project_id)
        directory = import_intake_dir(project.path, ref)
        header = self._validated_header(project, project_id, directory)
        return project, directory, header

    @staticmethod
    def _validated_header(project, project_id: str, directory: Path):
        header = read_import_header(directory)
        if (
            header.project_id != project_id
            or header.storage_identity != project.storage_identity
            or header.envelope.get("project_id") != project_id
        ):
            raise ValueError("import session does not belong to this project")
        return header

    def _chunk_limits(self, upload_count: int) -> BulkImportLimits:
        byte_limit = self._limits.max_upload_bytes
        if upload_count > 1:
            byte_limit = (
                _MULTI_FILE_BATCH_BYTES
                if byte_limit is None
                else min(byte_limit, _MULTI_FILE_BATCH_BYTES)
            )
        return BulkImportLimits(
            max_upload_files=min(
                self._limits.max_upload_files or _MAX_CHUNK_FILES, _MAX_CHUNK_FILES
            ),
            max_upload_bytes=byte_limit,
        )

    @staticmethod
    def _available_sheet_name(
        project, root: Path, request_key: str, requested: str
    ) -> str:
        candidate = upload_sheet_name(
            project, "import.files", request_key, requested, allocate=True
        )
        admitted = set()
        states = {
            str(row["id"]): str(row["state"])
            for row in project.db.execute("SELECT id,state FROM import_sessions")
        }
        for directory in root.iterdir():
            if not directory.is_dir() or states.get(directory.name) == "removed":
                continue
            try:
                header = read_import_header(directory)
                if header.resolution is not None:
                    continue
                admitted.add(str(header.envelope["action"]["sheet_name"]))
            except (KeyError, OSError, TypeError, ValueError):
                continue
        if candidate not in admitted:
            return candidate
        names = admitted | {
            str(row["name"]) for row in project.db.execute("SELECT name FROM sheets")
        }
        suffix = 2
        candidate = requested
        while candidate in names:
            candidate = f"{requested}-{suffix}"
            suffix += 1
        return candidate

    async def _enqueue_page(
        self, project_id: str, ref: str, through: int, sealed: bool
    ) -> None:
        if self._enqueue is None:
            return
        result = self._enqueue(project_id, ref, through, sealed)
        if inspect.isawaitable(result):
            await result

    def _status(
        self,
        project,
        ref: str,
        directory: Path,
        header: ImportIntakeHeader,
        inventory: ImportInventory | None = None,
    ) -> ImportSessionStatus:
        owned = inventory is None
        inventory = inventory or ImportInventory(directory / "inventory.db")
        try:
            totals = inventory.totals()
            through = inventory.through
            sealed = inventory.sealed
        finally:
            if owned:
                inventory.close()
        session = ImportSessionStore(project).get(ref)
        if session is not None:
            state = {
                "active": "running",
                "paused": "paused",
                "cancelled": "cancelled",
                "completed": "completed",
                "kept": "kept",
                "removed": "removed",
            }[session.state]
            if header.cancel_requested and session.state in {"active", "paused"}:
                state = "cancelling"
        elif header.resolution is not None:
            state = header.resolution
        elif header.cancel_requested:
            try:
                with import_worker_lock(directory, timeout=0):
                    state = "cancelled"
            except Timeout:
                state = "cancelling"
        else:
            job = self._latest_page_job(header.project_id, ref, through, sealed)
            state = (
                "paused" if job is not None and job.status == "failed" else "admitting"
            )
        return ImportSessionStatus(
            import_ref=ref,
            state=state,
            admitted_files=totals["count"],
            admitted_bytes=totals["bytes"],
            through=through,
            committed_rows=0 if session is None else session.committed_rows,
            committed_bytes=0 if session is None else session.committed_bytes,
            sheet_id=None if session is None else session.sheet_id,
            sheet_name=str(header.envelope["action"].get("sheet_name") or "files"),
            cancel_requested=header.cancel_requested,
            sealed=sealed,
            error=(
                "Import processing failed. Retry the import to continue."
                if session is None and state == "paused"
                else None
            ),
        )

    def _latest_page_job(self, project_id: str, ref: str, through: int, sealed: bool):
        queue = getattr(self._workspace, "queue", None)
        if queue is None:
            return None
        return queue.find_job_by_refs(
            IMPORT_FILES_PAGE_KIND,
            statuses=("queued", "running", "failed", "done", "cancelled"),
            project_id=project_id,
            storage_org_id=getattr(self._workspace, "queue_storage_org_id", None),
            dedupe_key=import_page_dedupe_key(project_id, ref, through, sealed),
        )


__all__ = ["ImportSessionService", "import_page_dedupe_key"]

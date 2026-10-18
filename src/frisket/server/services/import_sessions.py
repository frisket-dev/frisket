"""Bounded multipart intake for resumable ``import.files`` sessions."""

from __future__ import annotations

import inspect
import logging
import secrets
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict
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
    ActionJobEnvelope,
    action_job_cancelled_result,
    reserve_typed_action_job,
)
from frisket.engine.jobs.queue import IMPORT_FILES_PAGE_KIND
from frisket.engine.jobs.import_table import table_import_dedupe_key
from frisket.engine.jobs.queue import IMPORT_TABLE_STREAM_KIND
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
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.engine.store.table_import_intake import (
    FrozenTableImport,
    advance_table_progress,
    compact_table_intake,
    finish_table_intake,
    frozen_table_import,
    mark_table_committed,
    table_import_status,
    table_issue_groups,
)
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
EnqueueTableImport = Callable[[str, str, str], Awaitable[None] | None]


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
        enqueue_table: EnqueueTableImport | None = None,
        search_preparation: Any = None,
    ) -> None:
        self._workspace = workspace
        self._limits = limits or BulkImportLimits()
        self._enqueue = enqueue
        self._enqueue_table_callback = enqueue_table
        self._search_preparation = search_preparation

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
                if self._search_preparation is not None:
                    self._search_preparation.reserve_import(
                        project_id,
                        receipt_id=envelope.receipt_id,
                        import_ref=ref,
                    )
                return self._status(project, ref, directory, header)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    async def commit_table(
        self,
        project_id: str,
        frozen: FrozenTableImport,
        *,
        max_rows: int | None = None,
    ) -> ImportSessionStatus:
        """Bind, bootstrap, and queue one frozen table intake.

        The reviewed table service owns the plan.  This shared import service
        owns the ordinary action receipt and streaming-session publication so
        app routes do not have to reproduce the crash-recovery sequence.
        """

        project = self._workspace.get(project_id)
        directory = import_intake_dir(project.path, frozen.import_ref)
        if directory.resolve() != frozen.intake_dir.resolve():
            raise ValueError("frozen table intake belongs to another project")
        if max_rows is not None and (
            isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")
        admitted_header = read_import_header(directory)
        if (
            admitted_header.intake_kind != "table"
            or admitted_header.project_id != project_id
            or admitted_header.storage_identity != project.storage_identity
        ):
            raise ValueError("table import does not belong to this project")
        limits = [
            value for value in (admitted_header.max_rows, max_rows) if value is not None
        ]
        effective_max_rows = min(limits) if limits else None
        if (
            effective_max_rows is not None
            and frozen.exact_rows is not None
            and frozen.exact_rows > effective_max_rows
        ):
            raise ValueError("table import exceeds the configured row limit")

        columns = [
            {
                "name": column.target_name,
                "source_name": column.source_name,
                "type": column.output_type,
                **({"format": column.format} if column.format is not None else {}),
            }
            for column in frozen.plan.columns
        ]
        if frozen.plan.extra_values_target_name is not None:
            columns.append(
                {"name": frozen.plan.extra_values_target_name, "type": "json"}
            )
        source_ref = {
            "kind": "file",
            "label": frozen.source.filename,
            "fingerprint": f"sha256:{frozen.source.sha256.removeprefix('sha256:')}",
            "importer": "table",
        }
        history_source_ref = {
            **source_ref,
            "reviewed_plan": {
                "settings": asdict(frozen.plan.settings),
                "columns": [column.to_json() for column in frozen.plan.columns],
                "extra_values_target_name": frozen.plan.extra_values_target_name,
            },
        }
        destination_sheet_id = frozen.plan.destination_sheet_id
        action: dict[str, Any] = {
            "action_id": (
                "import.rows" if destination_sheet_id is None else "import.append_rows"
            ),
            "scope": (
                {"kind": "project"}
                if destination_sheet_id is None
                else {"kind": "sheet_rows", "sheet_id": destination_sheet_id}
            ),
            "params": {"columns": columns, "rows": [], "source": source_ref},
            "idempotency_key": frozen.import_ref,
        }
        if destination_sheet_id is None:
            action["sheet_name"] = frozen.plan.sheet_name
        bound = typed_action_for_request(action)

        with import_worker_lock(directory, timeout=10):
            header = read_import_header(directory)
            if (
                header.intake_kind != "table"
                or header.project_id != project_id
                or header.storage_identity != project.storage_identity
            ):
                raise ValueError("table import does not belong to this project")
            current = frozen_table_import(directory)
            if current.plan_hash != frozen.plan_hash:
                raise ValueError("frozen table plan changed")
            if (
                effective_max_rows is not None
                and current.exact_rows is not None
                and current.exact_rows > effective_max_rows
            ):
                raise ValueError("table import exceeds the configured row limit")
            reserved = reserve_typed_action_job(project, project_id, bound)
            if isinstance(reserved, ActionResult):
                raise ValueError("table import reservation is already terminal")
            envelope: ActionJobEnvelope = reserved
            mark_table_committed(
                directory,
                expected_plan_hash=frozen.plan_hash,
                envelope=envelope.to_json(),
                destination_sheet_id=destination_sheet_id,
                max_rows=effective_max_rows,
            )
            receipts = ReceiptStore(project)
            stored = receipts.find_by_id(envelope.receipt_id)
            if stored is None:
                raise RuntimeError("table import reservation disappeared")
            if stored.status == "queued":
                receipts.update_body_status(
                    stored.parsed().model_copy(update={"status": "running"}),
                    require_status="queued",
                )
            elif stored.status != "running":
                raise ValueError("table import reservation is already terminal")

            session = ImportSessionStore(project).get(frozen.import_ref)
            if session is None:
                common = {
                    "project": project,
                    "session_id": frozen.import_ref,
                    "writer_authority": f"bootstrap:{frozen.plan_hash}",
                    "columns": columns,
                    "project_id": project_id,
                    "action_kind": envelope.action_kind,
                    "idempotency_key": envelope.idempotency_key,
                    "params_hash": envelope.params_hash,
                    "action_id": envelope.action_id,
                    "receipt_id": envelope.receipt_id,
                    "source_ref": history_source_ref,
                }
                if destination_sheet_id is None:
                    StreamingSheetWriter.start_session(
                        **common, sheet_name=frozen.plan.sheet_name
                    ).abort()
                else:
                    StreamingSheetWriter.start_append_session(
                        **common, sheet_id=destination_sheet_id
                    ).abort()
            elif session.receipt_id != envelope.receipt_id:
                raise ValueError("table import session identity changed")
            header = read_import_header(directory)

        await self._enqueue_table(project_id, frozen.import_ref)
        return self._status(project, frozen.import_ref, directory, header)

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
                    uploads,
                    scratch,
                    False,
                    self._chunk_limits(len(uploads)),
                    # This request-private scratch is never recovery authority.
                    # Canonical blob durability and the inventory commit happen
                    # before the upload is acknowledged.
                    durable=False,
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
        if header.intake_kind == "table":
            return self._status(project, ref, directory, header)
        with ImportInventory(directory / "inventory.db") as inventory:
            return self._status(project, ref, directory, header, inventory)

    def cancel(self, project_id: str, ref: str) -> ImportSessionStatus:
        project, directory, _header = self._open(project_id, ref)
        with import_admit_lock(directory):
            header = self._validated_header(project, project_id, directory)
            header = request_import_cancel(directory)
        if (
            header.pdf_preparation is not None
            and self._search_preparation is not None
            and hasattr(self._search_preparation, "pdf_import_summary")
        ):
            from frisket.server.services.action_run_cancel import (
                ActionRunCancelRouteError,
                ActionRunCancelService,
            )

            stop_preparation = getattr(
                self._search_preparation, "stop_pdf_import", None
            )
            summary = (
                stop_preparation(project_id, ref)
                if callable(stop_preparation)
                else self._search_preparation.pdf_import_summary(project_id, ref)
            )
            cancellation = ActionRunCancelService(self._workspace)
            child_page = getattr(
                self._search_preparation, "pdf_import_child_page", None
            )
            after = 0
            while True:
                children = (
                    child_page(project_id, ref, after=after, limit=100)
                    if callable(child_page)
                    else summary.children
                )
                for child in children:
                    run_id = child.get("run_id")
                    if isinstance(run_id, int):
                        try:
                            cancellation.cancel_run(project_id, run_id)
                        except ActionRunCancelRouteError as exc:
                            if exc.status_code not in {404, 409}:
                                raise
                    elif isinstance(child.get("job_id"), int):
                        self._workspace.queue.cancel(int(child["job_id"]))
                if not callable(child_page) or len(children) < 100:
                    break
                after = int(children[-1]["rowid"])
        try:
            with import_worker_lock(directory, timeout=0):
                session = ImportSessionStore(project).get(ref)
                if session is not None and session.state in ("active", "paused"):
                    if header.intake_kind == "table":
                        table_cursor = table_import_status(directory).source_cursor
                        if table_cursor < session.cursor:
                            advance_table_progress(
                                directory,
                                expected_cursor=table_cursor,
                                source_cursor=session.cursor,
                            )
                    writer = StreamingSheetWriter._from_session(project, session)
                    cancelled = writer.cancel(expected_cursor=session.cursor)
                    if header.intake_kind == "table":
                        StreamingSheetWriter._from_session(
                            project, cancelled
                        ).finalize_session(
                            expected_cursor=cancelled.cursor, keep_cancelled=True
                        )
                        with import_admit_lock(directory):
                            header = set_import_resolution(directory, "kept")
                        finish_table_intake(directory, exact_rows=None)
                        compact_table_intake(directory)
                elif session is None:
                    action_job_cancelled_result(
                        project,
                        project_id=project_id,
                        receipt_id=str(header.envelope["receipt_id"]),
                        action_kind=str(header.envelope["action_kind"]),
                    )
        except Timeout:
            pass
        session = ImportSessionStore(project).get(ref)
        if (
            header.pdf_preparation is not None
            and session is not None
            and session.state in {"completed", "kept"}
        ):
            with import_admit_lock(directory):
                header = set_import_resolution(directory, "kept")
        if header.intake_kind == "table":
            return self._status(project, ref, directory, header)
        with ImportInventory(directory / "inventory.db") as inventory:
            return self._status(project, ref, directory, header, inventory)

    def resolve(
        self, project_id: str, ref: str, *, decision: str
    ) -> ImportSessionStatus:
        if decision not in {"keep", "remove"}:
            raise ValueError("decision must be keep or remove")
        project, directory, header = self._open(project_id, ref)
        cleanup_failed = False
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
                    if header.intake_kind == "table":
                        compact_table_intake(directory)
                    else:
                        compact_terminal_inventory(directory)
                except Exception:
                    cleanup_failed = True
                    logger.warning(
                        "terminal import inventory compaction failed",
                        exc_info=True,
                    )
        if header.intake_kind == "table":
            status = self._status(project, ref, directory, header)
        else:
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
        if cleanup_failed:
            self._enqueue_terminal_cleanup(
                project_id,
                ref,
                through=status.through,
                sealed=status.sealed,
            )
        return status

    async def resume(self, project_id: str, ref: str) -> ImportSessionStatus:
        project, directory, header = self._open(project_id, ref)
        session = ImportSessionStore(project).get(ref)
        if session is not None and session.state not in ("active", "paused"):
            raise ValueError("import session cannot be resumed")
        if header.cancel_requested:
            raise ValueError("cancelled import requires keep or remove")
        if header.intake_kind == "table":
            status = self._status(project, ref, directory, header)
            await self._enqueue_table(project_id, ref)
        else:
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

        def recent_terminal_refs() -> list[str]:
            return [
                str(row["id"])
                for row in project.db.execute(
                    "SELECT id FROM import_sessions "
                    "WHERE state IN ('completed','kept') "
                    "ORDER BY updated_at DESC LIMIT 10"
                )
            ]

        # Bound projection work using the initial snapshot. A live writer may
        # still complete while its status is being projected below.
        recent_terminal = set(recent_terminal_refs())
        if root.is_dir():
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                try:
                    ref = directory.name
                    header = self._validated_header(project, project_id, directory)
                    db_state = db_states.get(ref)
                    if db_state == "removed" or (
                        db_state in {"completed", "kept"} and ref not in recent_terminal
                    ):
                        continue
                    if db_state == "kept" and header.intake_kind != "table":
                        continue
                    if header.resolution == "removed" or (
                        header.resolution == "kept" and header.intake_kind != "table"
                    ):
                        continue
                    if header.intake_kind == "table":
                        sessions.append(self._status(project, ref, directory, header))
                    else:
                        with ImportInventory(directory / "inventory.db") as inventory:
                            sessions.append(
                                self._status(project, ref, directory, header, inventory)
                            )
                except (KeyError, OSError, ValueError):
                    continue
        # Publish completion order from a fresh snapshot after status
        # projection. Otherwise a session that completed during this call has
        # no rank and directory ordering can put an older completion first.
        terminal_by_recency = recent_terminal_refs()
        recent_terminal = set(terminal_by_recency)
        sessions = [
            status
            for status in sessions
            if status.state not in {"completed", "kept"}
            or status.import_ref in recent_terminal
        ]
        terminal_rank = {ref: rank for rank, ref in enumerate(terminal_by_recency)}
        sessions.sort(
            key=lambda status: (
                status.state in {"completed", "kept"},
                terminal_rank.get(status.import_ref, 0),
            )
        )
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

    async def _enqueue_table(self, project_id: str, ref: str) -> None:
        directory = import_intake_dir(self._workspace.get(project_id).path, ref)
        frozen = frozen_table_import(directory)
        header = read_import_header(directory)
        if self._enqueue_table_callback is not None:
            result = self._enqueue_table_callback(project_id, ref, frozen.plan_hash)
            if inspect.isawaitable(result):
                await result
            return
        queue = getattr(self._workspace, "queue", None)
        root = getattr(self._workspace, "root", None)
        if queue is None or root is None:
            return
        payload_extra = getattr(self._workspace, "queue_payload_extra", {})
        if not isinstance(payload_extra, dict):
            raise ValueError("table import queue payload configuration is invalid")
        queue.enqueue(
            IMPORT_TABLE_STREAM_KIND,
            {
                **payload_extra,
                "workspace_root": str(root),
                "project_id": project_id,
                "import_ref": ref,
                "plan_hash": frozen.plan_hash,
                "action_kind": str(header.envelope["action_kind"]),
                "receipt_id": str(header.envelope["receipt_id"]),
                "dedupe_key": table_import_dedupe_key(
                    project_id, ref, frozen.plan_hash
                ),
            },
        )

    def _enqueue_terminal_cleanup(
        self, project_id: str, ref: str, *, through: int, sealed: bool
    ) -> None:
        """Wake the existing page handler after terminal cleanup failed.

        ``resolve`` is synchronous, so it writes directly to the server-owned
        queue rather than trying to drive the asynchronous upload callback.
        The normal page dedupe key collapses repeated Keep/Remove requests
        while the previous cleanup retry remains queued or running.
        """

        queue = getattr(self._workspace, "queue", None)
        root = getattr(self._workspace, "root", None)
        if queue is None or root is None:
            logger.warning("terminal import cleanup retry could not be enqueued")
            return
        payload_extra = getattr(self._workspace, "queue_payload_extra", {})
        if not isinstance(payload_extra, dict):
            logger.warning("terminal import cleanup retry has invalid queue payload")
            return
        try:
            queue.enqueue(
                IMPORT_FILES_PAGE_KIND,
                {
                    **payload_extra,
                    "workspace_root": str(root),
                    "project_id": project_id,
                    "import_ref": ref,
                    "through": through,
                    "sealed": sealed,
                    "dedupe_key": import_page_dedupe_key(
                        project_id, ref, through, sealed
                    ),
                },
            )
        except Exception:
            logger.warning(
                "terminal import cleanup retry could not be enqueued", exc_info=True
            )

    def _status(
        self,
        project,
        ref: str,
        directory: Path,
        header: ImportIntakeHeader,
        inventory: ImportInventory | None = None,
    ) -> ImportSessionStatus:
        table_status = None
        table_source = None
        table_frozen = None
        if header.intake_kind == "table":
            table_status = table_import_status(directory)
            table_frozen = frozen_table_import(directory)
            table_source = table_frozen.source
            totals = {"count": 1, "bytes": table_source.size}
            through = table_status.source_cursor
            # The reviewed source and plan are immutable once committed.  A
            # queued/retrying writer never requires the browser to reselect it.
            sealed = table_status.phase in {"committed", "terminal"}
        else:
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
        provider_job = (
            self._latest_provider_job(
                header.project_id, ref, through, sealed, header=header
            )
            if header.intake_kind == "table"
            else None
        )
        if header.pdf_preparation is not None and header.resolution is not None:
            state = header.resolution
        elif session is not None:
            state = {
                "active": "running",
                "paused": "paused",
                "cancelled": "cancelled",
                "completed": "completed",
                "kept": "kept",
                "removed": "removed",
            }[session.state]
            if (
                session.state in {"active", "paused"}
                and provider_job is not None
                and provider_job.status in {"queued", "running"}
            ):
                # A paused writer is an internal recovery checkpoint while its
                # owning queue job remains live, not a user-stopped import.
                state = "running"
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
            job = provider_job or self._latest_provider_job(
                header.project_id, ref, through, sealed, header=header
            )
            state = (
                "paused" if job is not None and job.status == "failed" else "admitting"
            )
        receipt_id = str(header.envelope["receipt_id"])
        pdf_summary = None
        if (
            header.pdf_preparation is not None
            and self._search_preparation is not None
            and hasattr(self._search_preparation, "pdf_import_summary")
        ):
            pdf_summary = self._search_preparation.pdf_import_summary(
                header.project_id, ref
            )
        preparation_status, preparation_batch_id = (
            ("none", None)
            if header.intake_kind == "table" or self._search_preparation is None
            else self._search_preparation.handoff_for_receipt(
                header.project_id, receipt_id
            )
        )
        if (
            state in {"completed", "kept"}
            and header.intake_kind == "files"
            and header.pdf_preparation is None
            and preparation_batch_id is None
            and self._search_preparation is not None
            and self._search_preparation.automatic_enabled(header.project_id)
            and session is not None
            and session.sheet_id is not None
            and session.committed_rows > 0
        ):
            try:
                # The setting can be enabled after intake admission. Ensure
                # recovery owns the follow-up before publishing completion,
                # even when synchronous planning below is unavailable.
                self._search_preparation.reserve_import(
                    header.project_id,
                    receipt_id=receipt_id,
                    import_ref=ref,
                )
            except Exception as exc:
                raise ImportBulkRouteError(
                    503,
                    "Import saved; search preparation could not be scheduled yet. "
                    "Retry this request.",
                ) from exc
            try:
                # CLI workers do not construct the server Workspace, so their
                # optional terminal callback cannot publish this follow-up.
                # Materialize it at the server's terminal response boundary;
                # the receipt-backed trigger makes reconnects idempotent.
                self._search_preparation.create_after_import(
                    header.project_id,
                    receipt_id=receipt_id,
                    sheet_id=session.sheet_id,
                )
            except Exception:
                logger.warning(
                    "terminal import search preparation could not be published",
                    exc_info=True,
                )
                # The import is committed and the pending intent is durable.
                # Normal preparation recovery retries independently of this
                # importing client; never project a terminal import as running.
            # Re-read only after attempting publication: null may now mean a
            # durable retry, an empty/disabled outcome, or a newly readable plan.
            preparation_status, preparation_batch_id = (
                self._search_preparation.handoff_for_receipt(
                    header.project_id, receipt_id
                )
            )
        return ImportSessionStatus(
            import_ref=ref,
            intake_kind=header.intake_kind,
            state=state,
            admitted_files=totals["count"],
            admitted_bytes=totals["bytes"],
            through=through,
            committed_rows=0 if session is None else session.committed_rows,
            committed_bytes=0 if session is None else session.committed_bytes,
            sheet_id=None if session is None else session.sheet_id,
            sheet_name=(
                table_frozen.plan.sheet_name
                if table_frozen is not None
                else str(header.envelope["action"].get("sheet_name") or "files")
            ),
            cancel_requested=header.cancel_requested,
            sealed=sealed,
            error=(
                table_status.error
                if table_status is not None and table_status.error is not None
                else pdf_summary.error
                if pdf_summary is not None and pdf_summary.error is not None
                else (
                    "Import processing failed. Retry the import to continue."
                    if session is None and state == "paused"
                    else None
                )
            ),
            search_preparation_status=preparation_status,
            search_preparation_batch_id=preparation_batch_id,
            selected_total=(
                table_status.exact_rows
                if table_status is not None
                else int(
                    header.max_rows
                    if header.max_rows is not None
                    else header.pdf_preparation["selected_total"]
                )
                if header.pdf_preparation is not None
                else None
            ),
            estimated_rows=(
                None if table_status is None else table_status.estimated_rows
            ),
            processed_rows=(0 if table_status is None else table_status.source_cursor),
            skipped_duplicates=(
                0
                if table_status is None
                else max(
                    0,
                    table_status.source_cursor
                    - (0 if session is None else session.committed_rows),
                )
            ),
            problem_count=(0 if table_status is None else table_status.problem_count),
            source_label=(None if table_source is None else table_source.filename),
            can_rerun_workbook=(
                table_source is not None
                and table_source.retain_for_workbook_reuse
                and (directory / "source").is_file()
            ),
            source_error_groups=(
                []
                if table_status is None
                else table_issue_groups(directory, through=table_status.source_cursor)
            ),
            preparation_completed=(
                0
                if pdf_summary is None
                else pdf_summary.native_completed + pdf_summary.ocr_completed
            ),
            preparation_total=(
                0
                if pdf_summary is None
                else pdf_summary.native_total + pdf_summary.ocr_total
            ),
            ocr_completed=0 if pdf_summary is None else pdf_summary.ocr_completed,
            ocr_total=0 if pdf_summary is None else pdf_summary.ocr_total,
            unreadable_total=(
                0 if pdf_summary is None else pdf_summary.unreadable_total
            ),
            preparation_stopped=(False if pdf_summary is None else pdf_summary.stopped),
            preparation_pending_confirmation=(
                None if pdf_summary is None else pdf_summary.pending_confirmation
            ),
            semantic_preparation_error=(
                None if pdf_summary is None else pdf_summary.semantic_error
            ),
            row_error_summary=(
                None if pdf_summary is None else pdf_summary.row_error_summary
            ),
            preparation_children=(
                []
                if pdf_summary is None
                else [
                    {key: value for key, value in child.items() if key != "child_key"}
                    for child in pdf_summary.children
                ]
            ),
        )

    def _latest_provider_job(
        self,
        project_id: str,
        ref: str,
        through: int,
        sealed: bool,
        *,
        header: ImportIntakeHeader,
    ):
        queue = getattr(self._workspace, "queue", None)
        if queue is None:
            return None
        if header.intake_kind == "table":
            frozen = frozen_table_import(
                import_intake_dir(self._workspace.get(project_id).path, ref)
            )
            return queue.find_job_by_refs(
                IMPORT_TABLE_STREAM_KIND,
                statuses=("queued", "running", "failed", "done", "cancelled"),
                project_id=project_id,
                storage_org_id=getattr(self._workspace, "queue_storage_org_id", None),
                dedupe_key=table_import_dedupe_key(project_id, ref, frozen.plan_hash),
            )
        return queue.find_job_by_refs(
            IMPORT_FILES_PAGE_KIND,
            statuses=("queued", "running", "failed", "done", "cancelled"),
            project_id=project_id,
            storage_org_id=getattr(self._workspace, "queue_storage_org_id", None),
            dedupe_key=import_page_dedupe_key(project_id, ref, through, sealed),
        )

    def _latest_page_job(self, project_id: str, ref: str, through: int, sealed: bool):
        project = self._workspace.get(project_id)
        header = self._validated_header(
            project, project_id, import_intake_dir(project.path, ref)
        )
        return self._latest_provider_job(
            project_id, ref, through, sealed, header=header
        )


__all__ = ["ImportSessionService", "import_page_dedupe_key"]

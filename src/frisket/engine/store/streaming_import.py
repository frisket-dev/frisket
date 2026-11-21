"""Bounded-memory publication of large, newly imported sheets.

Rows are committed in caller-sized batches to a hidden sheet.  Publication is
one short transaction that records compact provenance and makes the sheet
visible; failures remove the staging sheet and its cascading row/cell state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Iterable, Mapping, Self

from frisket.contracts.action import ActionError, Receipt, ReceiptIO, ReceiptEvidence
from frisket.engine.store import op_log
from frisket.engine.store.changes import ChangeStore
from frisket.engine.store.cell_writes import (
    bind_base_cell_producer,
    create_base_cell_producer,
    discard_pending_base_cell_producer,
)
from frisket.engine.store.import_blobs import (
    PreparedImportBlobPlan,
    publish_import_blobs,
)
from frisket.engine.store.import_sessions import (
    ImportInProgress,
    ImportSession,
    ImportSessionConflict,
    ImportSessionStore,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.sheet_names import available_sheet_name
from frisket.engine.store.transactions import inserted_rowid, write_transaction


@dataclass(frozen=True)
class _ResumableSession:
    """The import session a resumable writer appends to, its writer, and the
    Change (``ops`` row) that records the import."""

    session_id: str
    writer_authority: str
    change_id: int


@dataclass(frozen=True)
class StreamingSheetPublication:
    sheet_id: int
    sheet_name: str
    row_count: int
    op_id: int
    receipt_id: str
    receipt: Receipt


class DuplicateStreamingPublication(RuntimeError):
    """The caller's publication identity is already present in the project."""

    def __init__(
        self,
        *,
        existing_receipt_id: str,
        idempotency_key: str | None,
        params_hash: str,
    ) -> None:
        super().__init__(f"streaming publication already exists: {existing_receipt_id}")
        self.existing_receipt_id = existing_receipt_id
        self.idempotency_key = idempotency_key
        self.params_hash = params_hash


def _insert_column(
    db: Any, sheet_id: int, name: str, column: Mapping[str, Any], position: int
) -> int:
    cursor = db.execute(
        "INSERT INTO columns "
        "(sheet_id,name,type,position,ai_generated,hidden,default_hidden,format,semantic_type) "
        "VALUES (?,?,?,?,0,?,?,?,?)",
        (
            sheet_id,
            name,
            str(column.get("type", "text")),
            position,
            int(bool(column.get("hidden", False))),
            int(bool(column.get("default_hidden", False))),
            column.get("format"),
            column.get("semantic_type"),
        ),
    )
    return inserted_rowid(cursor)


class StreamingSheetWriter:
    """Write a new sheet incrementally, then expose it atomically."""

    def __init__(
        self,
        project: Any,
        *,
        sheet_id: int,
        sheet_name: str,
        column_ids: dict[str, int],
        project_id: str,
        action_kind: str,
        idempotency_key: str | None,
        params_hash: str,
        action_id: str,
        receipt_id: str,
        source_ref: dict[str, Any],
        producer_id: int,
        warnings: Iterable[str] = (),
        session_id: str | None = None,
        writer_authority: str | None = None,
        op_id: int | None = None,
        row_count: int = 0,
    ) -> None:
        self.project = project
        self.sheet_id = sheet_id
        self._requested_sheet_name = sheet_name
        self._column_ids = column_ids
        self._project_id = project_id
        self._action_kind = action_kind
        self._idempotency_key = idempotency_key
        self._params_hash = params_hash
        self._action_id = action_id
        self._receipt_id = receipt_id
        self._feed_ref = source_ref
        self._producer_id = producer_id
        self._warnings = [str(warning) for warning in warnings]
        self._row_count = int(row_count)
        self._first_row_id: int | None = None
        self._closed = False
        self._publication: StreamingSheetPublication | None = None
        self._session_id = session_id
        self._writer_authority = writer_authority
        self._op_id = op_id

    @classmethod
    def start(
        cls,
        project: Any,
        *,
        sheet_name: str,
        columns: Iterable[Mapping[str, Any]],
        project_id: str,
        action_kind: str,
        idempotency_key: str | None,
        params_hash: str,
        action_id: str,
        receipt_id: str,
        source_ref: Mapping[str, Any],
        warnings: Iterable[str] = (),
    ) -> Self:
        requested_name = str(sheet_name).strip()
        if not requested_name:
            raise ValueError("sheet_name must be non-empty")
        column_specs = [dict(column) for column in columns]
        names = [str(column.get("name", "")).strip() for column in column_specs]
        if not names or any(not name for name in names):
            raise ValueError("columns must have non-empty names")
        if len(names) != len(set(names)):
            raise ValueError("column names must be unique")
        if not params_hash:
            raise ValueError("params_hash must be non-empty")
        if not action_id:
            raise ValueError("action_id must be non-empty")
        if not receipt_id:
            raise ValueError("receipt_id must be non-empty")

        staging_name = f".__streaming_import_{uuid.uuid4().hex}"
        db = project.db
        with write_transaction(db):
            cursor = db.execute(
                "INSERT INTO sheets (name, position, hidden) VALUES "
                "(?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets), 1)",
                (staging_name,),
            )
            sheet_id = inserted_rowid(cursor)
            column_ids: dict[str, int] = {}
            for position, (name, column) in enumerate(zip(names, column_specs), 1):
                column_ids[name] = _insert_column(db, sheet_id, name, column, position)
            producer_id = create_base_cell_producer(
                db, stage_id=f"receipt:{receipt_id}:{staging_name}"
            )

        return cls(
            project,
            sheet_id=sheet_id,
            sheet_name=requested_name,
            column_ids=column_ids,
            project_id=project_id,
            action_kind=action_kind,
            idempotency_key=idempotency_key,
            params_hash=params_hash,
            action_id=action_id,
            receipt_id=receipt_id,
            source_ref=dict(source_ref),
            producer_id=producer_id,
            warnings=warnings,
        )

    @classmethod
    def start_session(
        cls,
        project: Any,
        *,
        session_id: str,
        writer_authority: str,
        sheet_name: str,
        columns: Iterable[Mapping[str, Any]],
        project_id: str,
        action_kind: str,
        idempotency_key: str | None,
        params_hash: str,
        action_id: str,
        receipt_id: str,
        source_ref: Mapping[str, Any],
        operation_kind: str | None = None,
        allocate_sheet_name: bool = False,
    ) -> Self:
        """Create one visible, resumable sheet under a running receipt."""

        if not session_id or not writer_authority:
            raise ValueError("session id and writer authority must be non-empty")
        requested_name = str(sheet_name).strip()
        column_specs = [dict(column) for column in columns]
        names = [str(column.get("name", "")).strip() for column in column_specs]
        if not requested_name or not names or any(not name for name in names):
            raise ValueError("sheet and column names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("column names must be unique")
        if not params_hash or not action_id or not receipt_id:
            raise ValueError("reserved action identity must be complete")

        db = project.db
        with write_transaction(db):
            receipt_store = ReceiptStore(project)
            stored_receipt = receipt_store.find_by_id(receipt_id)
            reserved_receipt: Receipt | None = None
            if stored_receipt is not None:
                reserved_receipt = stored_receipt.parsed()
                if (
                    stored_receipt.status not in {"queued", "running"}
                    or stored_receipt.action_kind != action_kind
                    or stored_receipt.action_id != action_id
                    or stored_receipt.idempotency_key != idempotency_key
                    or stored_receipt.params_hash != params_hash
                    or reserved_receipt.op_ids
                ):
                    raise ImportSessionConflict(
                        "reserved import receipt identity does not match"
                    )
            else:
                duplicate = receipt_store.find_by_idempotency_key(idempotency_key)
                if duplicate is not None:
                    raise DuplicateStreamingPublication(
                        existing_receipt_id=duplicate.id,
                        idempotency_key=idempotency_key,
                        params_hash=duplicate.params_hash,
                    )
            if allocate_sheet_name:
                requested_name = available_sheet_name(db, requested_name)
            cursor = db.execute(
                "INSERT INTO sheets (name,position,hidden) VALUES "
                "(?,(SELECT COALESCE(MAX(position),0)+1 FROM sheets),0)",
                (requested_name,),
            )
            sheet_id = inserted_rowid(cursor)
            column_ids: dict[str, int] = {}
            for position, (name, column) in enumerate(zip(names, column_specs), 1):
                column_ids[name] = _insert_column(db, sheet_id, name, column, position)
            op_id = op_log.append_change(
                project,
                operation_kind or action_kind,
                spec={
                    "schema_version": "frisket.action.v2",
                    "kind": action_kind,
                    "params": {
                        "sheet_name": requested_name,
                        "source": dict(source_ref),
                    },
                },
                label=f"import {requested_name}",
                barrier=True,
                commit=False,
            )
            ChangeStore(project).start(op_id)
            op_log.set_undo_info(project, op_id, {"created_sheets": [sheet_id]})
            producer_id = create_base_cell_producer(
                db, stage_id=f"import-session:{session_id}", op_id=op_id
            )
            if reserved_receipt is None:
                receipt = Receipt(
                    receipt_id=receipt_id,
                    project_id=project_id,
                    action_id=action_id,
                    action_kind=action_kind,
                    op_ids=[op_id],
                    idempotency_key=idempotency_key,
                    params_hash=params_hash,
                    status="running",
                    inputs=[ReceiptIO(name="source", ref=dict(source_ref))],
                )
                receipt_store.insert_running(receipt, commit=False)
            else:
                if reserved_receipt.project_id != project_id:
                    raise ImportSessionConflict(
                        "reserved import receipt belongs to another project"
                    )
                receipt = reserved_receipt.model_copy(
                    update={
                        "status": "running",
                        "op_ids": [op_id],
                        "inputs": [
                            *reserved_receipt.inputs,
                            ReceiptIO(name="source", ref=dict(source_ref)),
                        ],
                    }
                )
                if not receipt_store.update_body_status(
                    receipt, require_status=stored_receipt.status, commit=False
                ):
                    raise ImportSessionConflict("reserved import receipt changed")
            db.execute(
                "INSERT INTO import_sessions "
                "(id,sheet_id,producer_id,op_id,receipt_id,writer_authority,state) "
                "VALUES (?,?,?,?,?,?,'active')",
                (
                    session_id,
                    sheet_id,
                    producer_id,
                    op_id,
                    receipt_id,
                    writer_authority,
                ),
            )
        return cls(
            project,
            sheet_id=sheet_id,
            sheet_name=requested_name,
            column_ids=column_ids,
            project_id=project_id,
            action_kind=action_kind,
            idempotency_key=idempotency_key,
            params_hash=params_hash,
            action_id=action_id,
            receipt_id=receipt_id,
            source_ref=dict(source_ref),
            producer_id=producer_id,
            session_id=session_id,
            writer_authority=writer_authority,
            op_id=op_id,
        )

    @classmethod
    def start_append_session(
        cls,
        project: Any,
        *,
        session_id: str,
        writer_authority: str,
        sheet_id: int,
        columns: Iterable[Mapping[str, Any]],
        project_id: str,
        action_kind: str,
        idempotency_key: str | None,
        params_hash: str,
        action_id: str,
        receipt_id: str,
        source_ref: Mapping[str, Any],
        operation_kind: str | None = None,
    ) -> Self:
        """Start one resumable import that appends into an existing sheet.

        The operation owns only the row ids checkpointed by ``append_page``.
        That keeps ordinary undo and cancelled-import removal from touching rows
        that existed before this import.
        """

        if not session_id or not writer_authority:
            raise ValueError("session id and writer authority must be non-empty")
        if not params_hash or not action_id or not receipt_id:
            raise ValueError("reserved action identity must be complete")
        column_specs = [dict(column) for column in columns]
        names = [str(column.get("name") or "").strip() for column in column_specs]
        if (
            not names
            or any(not name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError("append column names must be non-empty and unique")

        db = project.db
        with write_transaction(db):
            sheet = db.execute(
                "SELECT name FROM sheets WHERE id=? AND hidden=0", (int(sheet_id),)
            ).fetchone()
            if sheet is None:
                raise ImportSessionConflict("append destination sheet is unavailable")
            active = db.execute(
                "SELECT id FROM import_sessions WHERE sheet_id=? "
                "AND state IN ('active','paused','cancelled')",
                (int(sheet_id),),
            ).fetchone()
            if active is not None:
                session = ImportSessionStore(project).get(str(active["id"]))
                if session is not None:
                    raise ImportInProgress(session)
            column_ids = {
                str(row["name"]): int(row["id"])
                for row in db.execute(
                    "SELECT id,name FROM columns WHERE sheet_id=? AND active=1 "
                    "ORDER BY position,id",
                    (int(sheet_id),),
                ).fetchall()
            }
            next_position = int(
                db.execute(
                    "SELECT COALESCE(MAX(position),0) FROM columns WHERE sheet_id=?",
                    (int(sheet_id),),
                ).fetchone()[0]
            )
            created_column_ids: list[int] = []
            for name, column in zip(names, column_specs, strict=True):
                if name in column_ids:
                    continue
                next_position += 1
                column_id = _insert_column(
                    db, int(sheet_id), name, column, next_position
                )
                column_ids[name] = column_id
                created_column_ids.append(column_id)

            receipt_store = ReceiptStore(project)
            stored_receipt = receipt_store.find_by_id(receipt_id)
            reserved_receipt: Receipt | None = None
            if stored_receipt is not None:
                reserved_receipt = stored_receipt.parsed()
                if (
                    stored_receipt.status not in {"queued", "running"}
                    or stored_receipt.action_kind != action_kind
                    or stored_receipt.action_id != action_id
                    or stored_receipt.idempotency_key != idempotency_key
                    or stored_receipt.params_hash != params_hash
                    or reserved_receipt.op_ids
                ):
                    raise ImportSessionConflict(
                        "reserved import receipt identity does not match"
                    )
            else:
                duplicate = receipt_store.find_by_idempotency_key(idempotency_key)
                if duplicate is not None:
                    raise DuplicateStreamingPublication(
                        existing_receipt_id=duplicate.id,
                        idempotency_key=idempotency_key,
                        params_hash=duplicate.params_hash,
                    )

            requested_name = str(sheet["name"])
            op_id = op_log.append_change(
                project,
                operation_kind or action_kind,
                spec={
                    "schema_version": "frisket.action.v2",
                    "kind": action_kind,
                    "params": {
                        "sheet_id": int(sheet_id),
                        "source": dict(source_ref),
                    },
                },
                label=f"import into {requested_name}",
                barrier=True,
                undo_info={
                    "created_rows": [],
                    **(
                        {}
                        if source_ref.get("update_relationship") is not None
                        else {"created_rows_import_session_id": session_id}
                    ),
                    "created_columns": created_column_ids,
                },
                commit=False,
            )
            if created_column_ids:
                ChangeStore(project).start(op_id)
            producer_id = create_base_cell_producer(
                db, stage_id=f"import-session:{session_id}", op_id=op_id
            )
            if reserved_receipt is None:
                receipt = Receipt(
                    receipt_id=receipt_id,
                    project_id=project_id,
                    action_id=action_id,
                    action_kind=action_kind,
                    op_ids=[op_id],
                    idempotency_key=idempotency_key,
                    params_hash=params_hash,
                    status="running",
                    inputs=[ReceiptIO(name="source", ref=dict(source_ref))],
                )
                receipt_store.insert_running(receipt, commit=False)
            else:
                if reserved_receipt.project_id != project_id:
                    raise ImportSessionConflict(
                        "reserved import receipt belongs to another project"
                    )
                receipt = reserved_receipt.model_copy(
                    update={
                        "status": "running",
                        "op_ids": [op_id],
                        "inputs": [
                            *reserved_receipt.inputs,
                            ReceiptIO(name="source", ref=dict(source_ref)),
                        ],
                    }
                )
                if not receipt_store.update_body_status(
                    receipt, require_status=stored_receipt.status, commit=False
                ):
                    raise ImportSessionConflict("reserved import receipt changed")
            db.execute(
                "INSERT INTO import_sessions "
                "(id,sheet_id,producer_id,op_id,receipt_id,writer_authority,state) "
                "VALUES (?,?,?,?,?,?,'active')",
                (
                    session_id,
                    int(sheet_id),
                    producer_id,
                    op_id,
                    receipt_id,
                    writer_authority,
                ),
            )
        return cls(
            project,
            sheet_id=int(sheet_id),
            sheet_name=requested_name,
            column_ids=column_ids,
            project_id=project_id,
            action_kind=action_kind,
            idempotency_key=idempotency_key,
            params_hash=params_hash,
            action_id=action_id,
            receipt_id=receipt_id,
            source_ref=dict(source_ref),
            producer_id=producer_id,
            session_id=session_id,
            writer_authority=writer_authority,
            op_id=op_id,
        )

    @classmethod
    def resume_session(
        cls,
        project: Any,
        *,
        session_id: str,
        writer_authority: str,
        expected_cursor: int,
    ) -> Self:
        session = ImportSessionStore(project).replace_writer(
            session_id, writer_authority, expected_cursor=expected_cursor
        )
        return cls._from_session(project, session)

    @classmethod
    def _from_session(cls, project: Any, session: ImportSession) -> Self:
        if (
            session.sheet_id is None
            or session.producer_id is None
            or session.receipt_id is None
        ):
            raise ImportSessionConflict("import session no longer owns its sheet")
        stored = ReceiptStore(project).find_by_id(session.receipt_id)
        if stored is None or stored.status != "running":
            raise ImportSessionConflict("import session lost its running receipt")
        receipt = stored.parsed()
        sheet = project.db.execute(
            "SELECT name FROM sheets WHERE id=? AND hidden=0", (session.sheet_id,)
        ).fetchone()
        if sheet is None:
            raise ImportSessionConflict("import session sheet is unavailable")
        column_ids = {
            str(row["name"]): int(row["id"])
            for row in project.db.execute(
                "SELECT id,name FROM columns WHERE sheet_id=? AND active=1 ORDER BY position,id",
                (session.sheet_id,),
            ).fetchall()
        }
        source_ref = next(
            (item.ref for item in reversed(receipt.inputs) if item.name == "source"),
            {},
        )
        return cls(
            project,
            sheet_id=session.sheet_id,
            sheet_name=str(sheet["name"]),
            column_ids=column_ids,
            project_id=receipt.project_id,
            action_kind=receipt.action_kind,
            idempotency_key=receipt.idempotency_key,
            params_hash=receipt.params_hash or "",
            action_id=receipt.action_id,
            receipt_id=receipt.receipt_id,
            source_ref=source_ref,
            producer_id=session.producer_id,
            session_id=session.id,
            writer_authority=session.writer_authority,
            op_id=session.op_id,
            row_count=session.committed_rows,
        )

    @property
    def row_count(self) -> int:
        return self._row_count

    def append_rows(self, records: Iterable[Mapping[str, Any]]) -> list[int]:
        self._require_open()
        if self._session_id is not None:
            raise RuntimeError("resumable imports must use append_page")
        batch = [dict(record) for record in records]
        if not batch:
            return []
        db = self.project.db
        with write_transaction(db):
            row_ids = self.project.add_rows(
                self.sheet_id,
                batch,
                self._column_ids,
                producer_id=self._producer_id,
                commit=False,
            )
        self._row_count += len(batch)
        if self._first_row_id is None and row_ids:
            self._first_row_id = row_ids[0]
        return row_ids

    def append_page(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        expected_cursor: int,
        next_cursor: int,
        committed_bytes: int = 0,
        blob_plan: PreparedImportBlobPlan | None = None,
    ) -> list[int]:
        """Atomically publish one page and advance its durable checkpoint."""

        resumable = self._require_session()
        batch = [dict(record) for record in records]
        if not batch or next_cursor <= expected_cursor or committed_bytes < 0:
            raise ValueError("import page must advance a non-empty bounded batch")
        db = self.project.db
        with write_transaction(db):
            session = ImportSessionStore(self.project).require_writer(
                resumable.session_id,
                resumable.writer_authority,
                expected_cursor,
            )
            ImportSessionStore(self.project).upgrade_append_undo(session)
            row_ids = self.project.add_rows(
                self.sheet_id,
                batch,
                self._column_ids,
                producer_id=self._producer_id,
                commit=False,
            )
            db.executemany(
                "INSERT INTO import_session_rows(session_id,ordinal,row_id) "
                "VALUES(?,?,?)",
                (
                    (resumable.session_id, session.committed_rows + offset, row_id)
                    for offset, row_id in enumerate(row_ids, start=1)
                ),
            )
            # Membership is an effect even when every imported cell is null.
            # Register only this page, rather than rereading the whole session.
            from .structural_changes import record_structural_changes

            record_structural_changes(
                db, resumable.change_id, {"created_rows": row_ids}
            )
            if blob_plan is not None:
                publish_import_blobs(
                    self.project,
                    blob_plan.bind_row_ordinals(row_ids),
                    sheet_id=self.sheet_id,
                    column_ids=self._column_ids,
                    op_id=resumable.change_id,
                    receipt_id=self._receipt_id,
                    import_writer_authority=resumable.writer_authority,
                )
            updated = db.execute(
                "UPDATE import_sessions SET cursor=?,committed_rows=committed_rows+?,"
                "committed_bytes=committed_bytes+?,updated_at=datetime('now') "
                "WHERE id=? AND writer_authority=? AND cursor=? AND state='active'",
                (
                    int(next_cursor),
                    len(row_ids),
                    int(committed_bytes),
                    resumable.session_id,
                    resumable.writer_authority,
                    int(expected_cursor),
                ),
            )
            if updated.rowcount != 1:
                raise ImportSessionConflict("import session changed during page write")
        self._row_count += len(row_ids)
        if self._first_row_id is None and row_ids:
            self._first_row_id = row_ids[0]
        return row_ids

    def pause(self, *, expected_cursor: int) -> ImportSession:
        resumable = self._require_session()
        return ImportSessionStore(self.project).pause(
            resumable.session_id,
            resumable.writer_authority,
            expected_cursor,
        )

    def cancel(self, *, expected_cursor: int) -> ImportSession:
        """Stop acquisition while retaining every committed visible page."""

        resumable = self._require_session()
        return ImportSessionStore(self.project).cancel(
            resumable.session_id,
            resumable.writer_authority,
            expected_cursor,
        )

    def finalize_session(
        self,
        *,
        expected_cursor: int,
        keep_cancelled: bool = False,
        failure: ActionError | None = None,
        enumeration_complete: bool = False,
    ) -> StreamingSheetPublication:
        """Finalize the one reserved receipt; cancelled sessions require Keep."""

        resumable = self._require_session()
        db = self.project.db
        with write_transaction(db):
            allowed = ("cancelled",) if keep_cancelled else ("active", "paused")
            session = ImportSessionStore(self.project).require_writer(
                resumable.session_id,
                resumable.writer_authority,
                expected_cursor,
                states=allowed,
            )
            stored = ReceiptStore(self.project).find_by_id(self._receipt_id)
            if stored is None or stored.status != "running":
                raise ImportSessionConflict("import receipt is no longer running")
            receipt = self._completed_session_receipt(
                stored.parsed(),
                row_count=session.committed_rows,
                status="partial" if keep_cancelled else "completed",
            )
            if failure is not None:
                receipt = receipt.model_copy(
                    update={
                        "status": "partial" if receipt.op_ids else "failed",
                        "errors": [*receipt.errors, failure],
                        "warnings": [
                            *receipt.warnings,
                            "The action failed after publication; completed updates were kept.",
                        ],
                    }
                )
            if not ReceiptStore(self.project).update_body_status(
                receipt, require_status="running", commit=False
            ):
                raise ImportSessionConflict("import receipt finalization lost its CAS")
            ChangeStore(self.project).close(resumable.change_id)
            db.execute("UPDATE ops SET barrier=0 WHERE id=?", (resumable.change_id,))
            ImportSessionStore(self.project).finish(
                session,
                keep=keep_cancelled or failure is not None,
                enumeration_complete=enumeration_complete,
            )
        self._closed = True
        publication = StreamingSheetPublication(
            sheet_id=self.sheet_id,
            sheet_name=self._requested_sheet_name,
            row_count=session.committed_rows,
            op_id=resumable.change_id,
            receipt_id=self._receipt_id,
            receipt=receipt,
        )
        self._publication = publication
        return publication

    def remove_session(self, *, expected_cursor: int) -> None:
        """Revert only this import's contribution using ordinary Change history."""
        resumable = self._require_session()
        db = self.project.db
        with write_transaction(db):
            session = ImportSessionStore(self.project).require_writer(
                resumable.session_id,
                resumable.writer_authority,
                expected_cursor,
                states=("cancelled",),
            )
            self.prepare_removal(session)
            if (
                db.execute(
                    "SELECT change_order FROM ops WHERE id=?", (resumable.change_id,)
                ).fetchone()[0]
                is not None
            ):
                op_log.step_operation(
                    self.project, "revert", change_id=resumable.change_id
                )
        self._closed = True

    def prepare_removal(self, session: ImportSession) -> None:
        """Fence acquisition and settle its receipt in the caller's Undo transaction.

        Undo owns visibility changes; payloads and provenance remain available
        for restoration. A restored import never resumes acquisition.
        """
        db = self.project.db
        if not db.in_transaction:
            raise RuntimeError("Import removal requires a history transaction")
        stored = ReceiptStore(self.project).find_by_id(self._receipt_id)
        if stored is None or stored.status != "running":
            raise ImportSessionConflict("import receipt is no longer running")
        receipt = self._completed_session_receipt(
            stored.parsed(), row_count=0, status="cancelled"
        )
        if not ReceiptStore(self.project).update_body_status(
            receipt, require_status="running", commit=False
        ):
            raise ImportSessionConflict("import receipt cancellation lost its CAS")
        ChangeStore(self.project).close(self._op_id)
        db.execute("UPDATE ops SET barrier=0 WHERE id=?", (self._op_id,))
        ImportSessionStore(self.project).remove(session)

    def _completed_session_receipt(
        self, receipt: Receipt, *, row_count: int, status: str
    ) -> Receipt:
        if self._feed_ref.get("update_relationship") is not None:
            return self._completed_update_receipt(
                receipt, row_count=row_count, status=status
            )
        source = self._feed_ref
        outputs = [
            ReceiptIO(
                name=self._requested_sheet_name,
                ref={
                    "kind": "materialized_sheet",
                    "sheet_id": self.sheet_id,
                    "op_id": self._op_id,
                    "row_count": row_count,
                    "columns": dict(self._column_ids),
                    "reads": [],
                },
            ),
            ReceiptIO(
                name="rows",
                ref={
                    "kind": "source_rows",
                    "sheet_id": self.sheet_id,
                    "row_count": row_count,
                    "op_id": self._op_id,
                },
            ),
        ]
        supporting_records = [
            *receipt.evidence,
            ReceiptEvidence(
                ref={
                    "kind": "source_rows",
                    "sheet_id": self.sheet_id,
                    "row_count": row_count,
                    "op_id": self._op_id,
                }
            ),
        ]
        if source:
            supporting_records.append(
                ReceiptEvidence(
                    ref={
                        **source,
                        "kind": "import_source",
                        "source_kind": source.get("importer") or source.get("kind"),
                    }
                )
            )
        return receipt.model_copy(
            update={
                "status": status,
                "outputs": outputs,
                "evidence": supporting_records,
                "warnings": (
                    [
                        *receipt.warnings,
                        *self._warnings,
                        "Import was stopped; committed rows were kept.",
                    ]
                    if status == "partial"
                    else (
                        [
                            *receipt.warnings,
                            *self._warnings,
                            "Import was stopped; added rows were removed.",
                        ]
                        if status == "cancelled"
                        else [*receipt.warnings, *self._warnings]
                    )
                ),
            }
        )

    def _completed_update_receipt(
        self, receipt: Receipt, *, row_count: int, status: str
    ) -> Receipt:
        # The receipt refers to durable version ownership, not a second huge
        # row-ID list or a fictitious newly materialized sheet.
        counts = self.project.db.execute(
            "SELECT COUNT(*) AS cells, COUNT(DISTINCT cell.row_id) AS rows, "
            "COALESCE(SUM(cell.value IS NULL),0) AS cleared "
            "FROM cells cell JOIN base_cell_producers producer ON producer.id=cell.producer_id "
            "WHERE producer.op_id=?",
            (self._op_id,),
        ).fetchone()
        ref = {
            "kind": "updated_import_rows",
            "sheet_id": self.sheet_id,
            "op_id": self._op_id,
            "row_count": counts["rows"],
            "edit_count": counts["cells"],
            "cleared_cells": counts["cleared"],
            "matched": row_count,
        }
        changed = (
            self.project.db.execute(
                "SELECT change_order FROM ops WHERE id=?", (self._op_id,)
            ).fetchone()[0]
            is not None
        )
        return receipt.model_copy(
            update={
                "status": status,
                "op_ids": [self._op_id] if changed else [],
                "outputs": [ReceiptIO(name="updated_rows", ref=ref)],
                "evidence": [
                    *receipt.evidence,
                    ReceiptEvidence(ref=ref, retention="pinned"),
                ],
                "warnings": [
                    *receipt.warnings,
                    *self._warnings,
                    *(
                        ["Import was stopped; completed updates were kept."]
                        if status == "partial"
                        else []
                    ),
                ],
            }
        )

    def _require_session(self) -> _ResumableSession:
        self._require_open()
        if (
            self._session_id is None
            or self._writer_authority is None
            or self._op_id is None
        ):
            raise RuntimeError("streaming writer is not resumable")
        return _ResumableSession(self._session_id, self._writer_authority, self._op_id)

    def set_warnings(self, warnings: Iterable[str]) -> None:
        """Set the bounded diagnostics written with the final receipt."""

        self._require_open()
        self._warnings = [str(warning) for warning in warnings]

    def publish(
        self,
        *,
        request_spec: Mapping[str, Any] | None = None,
        reads: Iterable[Mapping[str, Any]] = (),
        source_ref: Mapping[str, Any] | None = None,
        blob_plan: PreparedImportBlobPlan | None = None,
    ) -> StreamingSheetPublication:
        if self._session_id is not None:
            raise RuntimeError("resumable imports must use finalize_session")
        if self._publication is not None:
            return self._publication
        self._require_open()
        observed_reads = [dict(read) for read in reads]
        source = dict(source_ref) if source_ref is not None else self._feed_ref
        db = self.project.db
        try:
            with write_transaction(db):
                duplicate = self._duplicate_receipt()
                if duplicate is not None:
                    raise DuplicateStreamingPublication(
                        existing_receipt_id=duplicate.id,
                        idempotency_key=duplicate.idempotency_key,
                        params_hash=duplicate.params_hash,
                    )
                sheet_name = self._available_sheet_name()
                action_spec = (
                    dict(request_spec)
                    if request_spec is not None
                    else {
                        "schema_version": "frisket.action.v2",
                        "kind": self._action_kind,
                        "params": {
                            "sheet_name": sheet_name,
                            "row_count": self._row_count,
                            "source": source,
                        },
                    }
                )
                op_id = op_log.append_change(
                    self.project,
                    self._action_kind,
                    spec=action_spec,
                    label=f"import {sheet_name}",
                    barrier=False,
                    commit=False,
                )
                op_log.set_undo_info(
                    self.project,
                    op_id,
                    {"created_sheets": [self.sheet_id]},
                )
                bind_base_cell_producer(db, self._producer_id, op_id=op_id)
                receipt = Receipt(
                    receipt_id=self._receipt_id,
                    project_id=self._project_id,
                    action_id=self._action_id,
                    action_kind=self._action_kind,
                    op_ids=[op_id],
                    idempotency_key=self._idempotency_key,
                    params_hash=self._params_hash,
                    status="completed",
                    inputs=[
                        ReceiptIO(
                            name="source",
                            ref=source
                            or {
                                "kind": "inline_rows",
                                "params_hash": self._params_hash,
                            },
                        ),
                        *[
                            ReceiptIO(name=f"read.{index}", ref=read)
                            for index, read in enumerate(observed_reads)
                        ],
                    ],
                    outputs=[
                        ReceiptIO(
                            name=sheet_name,
                            ref={
                                "kind": "materialized_sheet",
                                "sheet_id": self.sheet_id,
                                "op_id": op_id,
                                "row_count": self._row_count,
                                "columns": dict(self._column_ids),
                                "reads": observed_reads,
                            },
                        ),
                        *[
                            ReceiptIO(
                                name=f"column.{name}",
                                ref={
                                    "kind": "source_column",
                                    "sheet_id": self.sheet_id,
                                    "column_id": column_id,
                                    "op_id": op_id,
                                },
                            )
                            for name, column_id in self._column_ids.items()
                        ],
                        ReceiptIO(
                            name="rows",
                            ref={
                                "kind": "source_rows",
                                "sheet_id": self.sheet_id,
                                "row_count": self._row_count,
                                "op_id": op_id,
                            },
                        ),
                    ],
                    evidence=[
                        ReceiptEvidence(
                            ref={
                                "kind": "source_rows",
                                "sheet_id": self.sheet_id,
                                "row_count": self._row_count,
                                "op_id": op_id,
                            }
                        ),
                        *(
                            [
                                ReceiptEvidence(
                                    ref={
                                        **source,
                                        "kind": "import_source",
                                        "source_kind": source.get("importer")
                                        or source.get("kind"),
                                    }
                                )
                            ]
                            if source
                            else []
                        ),
                        *(
                            [
                                ReceiptEvidence(
                                    ref={
                                        "kind": "source_cell",
                                        "sheet_id": self.sheet_id,
                                        "row_id": self._first_row_id,
                                        "column_id": next(
                                            iter(self._column_ids.values())
                                        ),
                                        "op_id": op_id,
                                    }
                                )
                            ]
                            if self._first_row_id is not None
                            else []
                        ),
                    ],
                    warnings=self._warnings,
                )
                if blob_plan is not None:
                    published = publish_import_blobs(
                        self.project,
                        blob_plan,
                        sheet_id=self.sheet_id,
                        column_ids=self._column_ids,
                        op_id=op_id,
                        receipt_id=self._receipt_id,
                    )
                    receipt.evidence.extend(
                        ReceiptEvidence(ref=ref) for ref in published
                    )
                ChangeStore(self.project).close(op_id)
                ReceiptStore(self.project).insert_completed(receipt)
                updated = db.execute(
                    "UPDATE sheets SET name=?, hidden=0 WHERE id=? AND hidden=1",
                    (sheet_name, self.sheet_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("streaming import staging sheet is missing")
        except BaseException:
            self._delete_staging()
            self._closed = True
            raise

        self._closed = True
        self._publication = StreamingSheetPublication(
            sheet_id=self.sheet_id,
            sheet_name=sheet_name,
            row_count=self._row_count,
            op_id=op_id,
            receipt_id=self._receipt_id,
            receipt=receipt,
        )
        return self._publication

    def abort(self) -> None:
        if self._publication is not None or self._closed:
            return
        if self._session_id is not None:
            # A worker stopping never owns deletion of already committed pages.
            self._closed = True
            return
        self._delete_staging()
        self._closed = True

    def _delete_staging(self) -> None:
        db = self.project.db
        with write_transaction(db):
            db.execute(
                "DELETE FROM sheets WHERE id=? AND hidden=1",
                (self.sheet_id,),
            )
            discard_pending_base_cell_producer(db, self._producer_id)

    def _available_sheet_name(self) -> str:
        used = {
            str(row["name"])
            for row in self.project.db.execute(
                "SELECT name FROM sheets WHERE id<>?", (self.sheet_id,)
            ).fetchall()
        }
        if self._requested_sheet_name not in used:
            return self._requested_sheet_name
        raise ValueError(
            f"duplicate_sheet_name: {self._requested_sheet_name!r} already exists; retry with a new name"
        )

    def _duplicate_receipt(self) -> Any | None:
        receipts = ReceiptStore(self.project)
        if self._idempotency_key is not None:
            existing = receipts.find_by_idempotency_key(self._idempotency_key)
            if existing is not None:
                return existing
        return receipts.find_by_id(self._receipt_id)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("streaming sheet writer is closed")

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc, traceback
        if exc_type is None:
            self.publish()
        else:
            self.abort()
        return False


__all__ = [
    "DuplicateStreamingPublication",
    "StreamingSheetPublication",
    "StreamingSheetWriter",
]

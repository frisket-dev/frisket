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

from frisket.contracts.action import Receipt, ReceiptIO, ReceiptEvidence
from frisket.engine.store import op_log
from frisket.engine.store.cell_writes import (
    bind_base_cell_producer,
    create_base_cell_producer,
    delete_sheet_rows,
    discard_pending_base_cell_producer,
)
from frisket.engine.store.import_blobs import (
    PreparedImportBlobPlan,
    publish_import_blobs,
)
from frisket.engine.store.import_sessions import (
    ImportSession,
    ImportSessionConflict,
    ImportSessionStore,
)
from frisket.engine.store.receipts import ReceiptStore


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
        self._source_ref = source_ref
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
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "INSERT INTO sheets (name, position, hidden) VALUES "
                "(?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets), 1)",
                (staging_name,),
            )
            sheet_id = int(cursor.lastrowid)
            column_ids: dict[str, int] = {}
            for position, (name, column) in enumerate(zip(names, column_specs), 1):
                cursor = db.execute(
                    "INSERT INTO columns "
                    "(sheet_id, name, type, position, ai_generated, hidden, "
                    "default_hidden, format, semantic_type) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
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
                column_ids[name] = int(cursor.lastrowid)
            producer_id = create_base_cell_producer(
                db, stage_id=f"receipt:{receipt_id}:{staging_name}"
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

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
        try:
            db.execute("BEGIN IMMEDIATE")
            receipt_store = ReceiptStore(project)
            stored_receipt = receipt_store.find_by_id(receipt_id)
            reserved_receipt: Receipt | None = None
            if stored_receipt is not None:
                reserved_receipt = stored_receipt.parsed()
                if (
                    stored_receipt.status != "running"
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
            cursor = db.execute(
                "INSERT INTO sheets (name,position,hidden) VALUES "
                "(?,(SELECT COALESCE(MAX(position),0)+1 FROM sheets),0)",
                (requested_name,),
            )
            sheet_id = int(cursor.lastrowid)
            column_ids: dict[str, int] = {}
            for position, (name, column) in enumerate(zip(names, column_specs), 1):
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
                column_ids[name] = int(cursor.lastrowid)
            op_id = op_log.append_op(
                project,
                action_kind,
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
            op_log.set_undo_info(
                project, op_id, {"created_sheets": [sheet_id]}, commit=False
            )
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
                        "op_ids": [op_id],
                        "inputs": [
                            *reserved_receipt.inputs,
                            ReceiptIO(name="source", ref=dict(source_ref)),
                        ],
                    }
                )
                if not receipt_store.update_body_status(
                    receipt, require_status="running", commit=False
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
            db.commit()
        except BaseException:
            db.rollback()
            raise
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
                "SELECT id,name FROM columns WHERE sheet_id=? ORDER BY position,id",
                (session.sheet_id,),
            ).fetchall()
        }
        source_ref = next(
            (
                item.ref
                for item in reversed(receipt.inputs)
                if item.name == "source"
            ),
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
        try:
            db.execute("BEGIN IMMEDIATE")
            row_ids = self.project.add_rows(
                self.sheet_id,
                batch,
                self._column_ids,
                producer_id=self._producer_id,
                commit=False,
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
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

        self._require_session()
        batch = [dict(record) for record in records]
        if not batch or next_cursor <= expected_cursor or committed_bytes < 0:
            raise ValueError("import page must advance a non-empty bounded batch")
        db = self.project.db
        try:
            db.execute("BEGIN IMMEDIATE")
            ImportSessionStore(self.project).require_writer(
                self._session_id,
                self._writer_authority,
                expected_cursor,
            )
            row_ids = self.project.add_rows(
                self.sheet_id,
                batch,
                self._column_ids,
                producer_id=self._producer_id,
                commit=False,
            )
            if blob_plan is not None:
                publish_import_blobs(
                    self.project,
                    blob_plan.bind_row_ordinals(row_ids),
                    sheet_id=self.sheet_id,
                    column_ids=self._column_ids,
                    op_id=self._op_id,
                    receipt_id=self._receipt_id,
                )
            updated = db.execute(
                "UPDATE import_sessions SET cursor=?,committed_rows=committed_rows+?,"
                "committed_bytes=committed_bytes+?,updated_at=datetime('now') "
                "WHERE id=? AND writer_authority=? AND cursor=? AND state='active'",
                (
                    int(next_cursor),
                    len(row_ids),
                    int(committed_bytes),
                    self._session_id,
                    self._writer_authority,
                    int(expected_cursor),
                ),
            )
            if updated.rowcount != 1:
                raise ImportSessionConflict("import session changed during page write")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._row_count += len(row_ids)
        if self._first_row_id is None and row_ids:
            self._first_row_id = row_ids[0]
        return row_ids

    def pause(self, *, expected_cursor: int) -> ImportSession:
        self._require_session()
        return ImportSessionStore(self.project).set_state(
            self._session_id,
            self._writer_authority,
            expected_cursor,
            from_states=("active",),
            state="paused",
        )

    def cancel(self, *, expected_cursor: int) -> ImportSession:
        """Stop acquisition while retaining every committed visible page."""

        self._require_session()
        return ImportSessionStore(self.project).set_state(
            self._session_id,
            self._writer_authority,
            expected_cursor,
            from_states=("active", "paused"),
            state="cancelled",
        )

    def finalize_session(
        self, *, expected_cursor: int, keep_cancelled: bool = False
    ) -> StreamingSheetPublication:
        """Finalize the one reserved receipt; cancelled sessions require Keep."""

        self._require_session()
        db = self.project.db
        try:
            db.execute("BEGIN IMMEDIATE")
            allowed = ("cancelled",) if keep_cancelled else ("active", "paused")
            session = ImportSessionStore(self.project).require_writer(
                self._session_id,
                self._writer_authority,
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
            if not ReceiptStore(self.project).update_body_status(
                receipt, require_status="running", commit=False
            ):
                raise ImportSessionConflict("import receipt finalization lost its CAS")
            db.execute("UPDATE ops SET barrier=0 WHERE id=?", (self._op_id,))
            final_state = "kept" if keep_cancelled else "completed"
            updated = db.execute(
                "UPDATE import_sessions SET state=?,updated_at=datetime('now') "
                "WHERE id=? AND writer_authority=? AND cursor=? AND state=?",
                (
                    final_state,
                    self._session_id,
                    self._writer_authority,
                    int(expected_cursor),
                    allowed[0] if len(allowed) == 1 else session.state,
                ),
            )
            if updated.rowcount != 1:
                raise ImportSessionConflict("import session finalization lost its CAS")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._closed = True
        publication = StreamingSheetPublication(
            sheet_id=self.sheet_id,
            sheet_name=self._requested_sheet_name,
            row_count=session.committed_rows,
            op_id=self._op_id,
            receipt_id=self._receipt_id,
            receipt=receipt,
        )
        self._publication = publication
        return publication

    def remove_session(self, *, expected_cursor: int) -> None:
        """Remove committed import rows while retaining the new empty sheet."""

        self._require_session()
        db = self.project.db
        try:
            db.execute("BEGIN IMMEDIATE")
            session = ImportSessionStore(self.project).require_writer(
                self._session_id,
                self._writer_authority,
                expected_cursor,
                states=("cancelled",),
            )
            artifact_ids = [
                int(row["id"])
                for row in db.execute(
                    "SELECT id FROM source_artifacts WHERE source_sheet_id=?",
                    (session.sheet_id,),
                )
            ]
            db.execute(
                "DELETE FROM evidence_links WHERE sheet_id=? OR op_id=?",
                (session.sheet_id, session.op_id),
            )
            if artifact_ids:
                marks = ",".join("?" for _ in artifact_ids)
                db.execute(
                    f"DELETE FROM artifact_timeline_segments WHERE "
                    f"derived_artifact_id IN ({marks}) OR source_artifact_id IN ({marks})",
                    (*artifact_ids, *artifact_ids),
                )
                db.execute(
                    f"DELETE FROM source_artifacts WHERE id IN ({marks})", artifact_ids
                )
            delete_sheet_rows(
                db,
                sheet_id=session.sheet_id,
                producer_id=session.producer_id,
                import_writer_authority=self._writer_authority,
                import_expected_cursor=expected_cursor,
            )
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
            db.execute("UPDATE ops SET barrier=0 WHERE id=?", (self._op_id,))
            updated = db.execute(
                "UPDATE import_sessions SET state='removed',updated_at=datetime('now') "
                "WHERE id=? AND writer_authority=? AND cursor=? AND state='cancelled'",
                (
                    self._session_id,
                    self._writer_authority,
                    int(expected_cursor),
                ),
            )
            if updated.rowcount != 1:
                raise ImportSessionConflict("import session removal lost its CAS")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._closed = True

    def _completed_session_receipt(
        self, receipt: Receipt, *, row_count: int, status: str
    ) -> Receipt:
        source = self._source_ref
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
        evidence = [
            *receipt.evidence,
            ReceiptEvidence(
                ref={
                    "kind": "source_rows",
                    "sheet_id": self.sheet_id,
                    "row_count": row_count,
                    "op_id": self._op_id,
                }
            )
        ]
        if source:
            evidence.append(
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
                "evidence": evidence,
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

    def _require_session(self) -> None:
        self._require_open()
        if (
            self._session_id is None
            or self._writer_authority is None
            or self._op_id is None
        ):
            raise RuntimeError("streaming writer is not resumable")

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
        source = dict(source_ref) if source_ref is not None else self._source_ref
        db = self.project.db
        try:
            db.execute("BEGIN IMMEDIATE")
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
            op_id = op_log.append_op(
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
                commit=False,
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
                        or {"kind": "inline_rows", "params_hash": self._params_hash},
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
                                    "column_id": next(iter(self._column_ids.values())),
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
                receipt.evidence.extend(ReceiptEvidence(ref=ref) for ref in published)
            ReceiptStore(self.project).insert_completed(receipt, commit=False)
            updated = db.execute(
                "UPDATE sheets SET name=?, hidden=0 WHERE id=? AND hidden=1",
                (sheet_name, self.sheet_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("streaming import staging sheet is missing")
            db.commit()
        except BaseException:
            db.rollback()
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
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM sheets WHERE id=? AND hidden=1",
                (self.sheet_id,),
            )
            discard_pending_base_cell_producer(db, self._producer_id)
            db.commit()
        except BaseException:
            db.rollback()
            raise

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

"""Source-atomic PDF publication under the ordinary import writer/history.

Callers prepare one source's child artifacts outside a transaction, then pass
their bounded records and prepared blob plan here. No PDF work runs under the
database lock. This writer ends before queued text preparation starts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Self

from .import_blobs import PreparedImportBlobPlan, publish_import_blobs
from .import_sessions import (
    ImportInProgress,
    ImportSession,
    ImportSessionConflict,
    ImportSessionStore,
    active_import_session,
)
from .receipts import ReceiptStore
from .streaming_import import StreamingSheetPublication, StreamingSheetWriter


@dataclass(frozen=True)
class PdfSplitSource:
    """One frozen occurrence; uploads have no project source-cell identity."""

    source_id: str
    child_count: int
    sheet_id: int | None = None
    row_id: int | None = None
    column_id: int | None = None
    value: Any = None


class PdfSplitWriter(StreamingSheetWriter):
    @classmethod
    def start(
        cls,
        project: Any,
        *,
        session_id: str,
        writer_authority: str,
        columns: Iterable[Mapping[str, Any]],
        project_id: str,
        idempotency_key: str | None,
        params_hash: str,
        action_id: str,
        receipt_id: str,
        sources: Iterable[PdfSplitSource],
        sheet_id: int | None = None,
        replace: bool = False,
        sheet_name: str = "Split documents",
        source_ref: Mapping[str, Any] | None = None,
        action_kind: str = "import.packet_split",
    ) -> Self:
        plans = list(sources)
        if not plans or len({source.source_id for source in plans}) != len(plans):
            raise ValueError("split requires distinct source occurrences")
        for source in plans:
            if (
                not source.source_id
                or type(source.child_count) is not int
                or source.child_count < 1
            ):
                raise ValueError("split source identity and child count are required")
            identity = (source.sheet_id, source.row_id, source.column_id)
            if any(value is None for value in identity) and any(
                value is not None for value in identity
            ):
                raise ValueError("split source-cell identity must be complete")
            if source.row_id is None and source.value is not None:
                raise ValueError("uploaded split source cannot claim a cell value")
            if (
                sheet_id is not None
                and replace
                and (source.row_id is None or source.sheet_id != sheet_id)
            ):
                raise ValueError("replacement sources must belong to the destination")
        row_ids = [source.row_id for source in plans if source.row_id is not None]
        if len(set(row_ids)) != len(row_ids):
            raise ValueError("split source rows must be distinct")
        for source in plans:
            _validate_source_cell(
                project,
                {
                    "sheet_id": source.sheet_id,
                    "row_id": source.row_id,
                    "column_id": source.column_id,
                    "expected_value": json.dumps(source.value)
                    if source.row_id is not None
                    else None,
                },
            )
        if sheet_id is not None:
            existing = active_import_session(project.db, sheet_id)
            if existing is not None:
                raise ImportInProgress(existing)
        kwargs = dict(
            session_id=session_id,
            writer_authority=writer_authority,
            columns=columns,
            project_id=project_id,
            action_kind=action_kind,
            operation_kind="pdf.split",
            idempotency_key=idempotency_key,
            params_hash=params_hash,
            action_id=action_id,
            receipt_id=receipt_id,
            source_ref=dict(
                source_ref or {"kind": "pdf_split", "source_count": len(plans)}
            ),
        )
        writer = (
            super().start_append_session(project, sheet_id=sheet_id, **kwargs)
            if sheet_id is not None
            else super().start_session(project, sheet_name=sheet_name, **kwargs)
        )
        try:
            writer._reserve_sources(plans, replace=sheet_id is not None and replace)
        except BaseException:
            # The in-transaction recheck can still lose a race after admission.
            # Keep its failed receipt, but no applied no-op or empty destination.
            writer.finalize(expected_cursor=0, stopped=True, setup_failed=True)
            writer._discard_failed_setup()
            raise
        return writer

    @property
    def session(self) -> ImportSession:
        self._require_session()
        session = ImportSessionStore(self.project).get(self._session_id)
        if session is None:
            raise ImportSessionConflict("split session is unavailable")
        return session

    def _require_split_writer(self, expected_cursor: int) -> ImportSession:
        self._require_session()
        session = ImportSessionStore(self.project).require_writer(
            self._session_id,
            self._writer_authority,
            expected_cursor,
        )
        operation = self.project.db.execute(
            "SELECT kind,status FROM ops WHERE id=?",
            (session.op_id,),
        ).fetchone()
        if (
            operation is None
            or operation["kind"] != "pdf.split"
            or operation["status"] != "applied"
        ):
            raise ImportSessionConflict("split operation is no longer applied")
        return session

    def _validate_source(self, source: Mapping[str, Any]) -> None:
        _validate_source_cell(self.project, source)

    def _discard_failed_setup(self) -> None:
        db = self.project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            operation = db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (self._op_id,)
            ).fetchone()
            undo = json.loads(operation["undo_info"])
            for sheet_id in undo.get("created_sheets", []):
                db.execute(
                    "UPDATE sheets SET hidden=1,name=name || ' (failed:' || id || ')' WHERE id=?",
                    (sheet_id,),
                )
            for column_id in undo.get("created_columns", []):
                db.execute(
                    "UPDATE columns SET active=0,hidden=1 WHERE id=?", (column_id,)
                )
            db.execute(
                "UPDATE ops SET status='discarded',barrier=0 WHERE id=?", (self._op_id,)
            )
            db.execute(
                "UPDATE import_sessions SET state='removed' WHERE id=?",
                (self._session_id,),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def finalize_session(
        self, *, expected_cursor: int, keep_cancelled: bool = False
    ) -> StreamingSheetPublication:
        raise RuntimeError("PDF split sessions must use split finalize")

    def remove_session(self, *, expected_cursor: int) -> None:
        raise RuntimeError("PDF split sessions must stop remaining and use split Undo")

    def _reserve_sources(self, sources: list[PdfSplitSource], *, replace: bool) -> None:
        db = self.project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            self._require_split_writer(0)
            db.executemany(
                "INSERT INTO import_session_pdf_sources "
                "(session_id,ordinal,source_id,sheet_id,row_id,column_id,expected_value,child_count,first_position) "
                "VALUES(?,?,?,?,?,?,?,?,1)",
                (
                    (
                        self._session_id,
                        index,
                        source.source_id,
                        source.sheet_id,
                        source.row_id,
                        source.column_id,
                        json.dumps(source.value) if source.row_id is not None else None,
                        source.child_count,
                    )
                    for index, source in enumerate(sources)
                ),
            )
            for source in db.execute(
                "SELECT * FROM import_session_pdf_sources WHERE session_id=? ORDER BY ordinal",
                (self._session_id,),
            ).fetchall():
                self._validate_source(source)
            if replace:
                # One metadata-only ordered pass, including hidden ancestors.
                # Renumber ties deterministically and reserve slots after each
                # original; hiding that original exposes children in its slot.
                db.execute(
                    "CREATE TEMP TABLE pdf_split_positions (row_id INTEGER PRIMARY KEY, position INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT INTO pdf_split_positions "
                    "SELECT r.id,ROW_NUMBER() OVER (ORDER BY r.position,r.id) + "
                    "COALESCE(SUM(COALESCE(s.child_count,0)) OVER ("
                    "ORDER BY r.position,r.id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) "
                    "FROM rows AS r LEFT JOIN import_session_pdf_sources AS s "
                    "ON s.row_id=r.id AND s.session_id=? WHERE r.sheet_id=?",
                    (self._session_id, self.sheet_id),
                )
                db.execute(
                    "UPDATE rows SET position=(SELECT position FROM pdf_split_positions WHERE row_id=rows.id) "
                    "WHERE sheet_id=?",
                    (self.sheet_id,),
                )
                db.execute(
                    "UPDATE import_session_pdf_sources SET first_position="
                    "(SELECT position+1 FROM pdf_split_positions WHERE row_id=import_session_pdf_sources.row_id) "
                    "WHERE session_id=?",
                    (self._session_id,),
                )
                db.execute("DROP TABLE pdf_split_positions")
            else:
                base = int(
                    db.execute(
                        "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?",
                        (self.sheet_id,),
                    ).fetchone()[0]
                )
                db.execute(
                    "WITH slots AS (SELECT ordinal,?+1+COALESCE(SUM(child_count) OVER ("
                    "ORDER BY ordinal ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) AS position "
                    "FROM import_session_pdf_sources WHERE session_id=?) "
                    "UPDATE import_session_pdf_sources SET first_position="
                    "(SELECT position FROM slots WHERE slots.ordinal=import_session_pdf_sources.ordinal) "
                    "WHERE session_id=?",
                    (base, self._session_id, self._session_id),
                )
            operation = db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (self._op_id,)
            ).fetchone()
            undo = json.loads(operation["undo_info"])
            undo["created_rows_import_session_id"] = self._session_id
            if replace:
                undo["replaced_rows_import_session_id"] = self._session_id
            db.execute(
                "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(undo), self._op_id)
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def publish_source(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        expected_cursor: int,
        committed_bytes: int = 0,
        blob_plan: PreparedImportBlobPlan | None = None,
        source_error: str | None = None,
    ) -> list[int]:
        """Swap exactly one frozen occurrence; failures leave it untouched."""
        batch = [dict(record) for record in records]
        if committed_bytes < 0:
            raise ValueError("committed bytes must be nonnegative")
        db = self.project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            session = self._require_split_writer(expected_cursor)
            source = db.execute(
                "SELECT * FROM import_session_pdf_sources WHERE session_id=? AND ordinal=? AND state='pending'",
                (self._session_id, expected_cursor),
            ).fetchone()
            if source is None:
                raise ImportSessionConflict("split source checkpoint is unavailable")
            if len(batch) != source["child_count"]:
                raise ValueError("split child count differs from the frozen source")
            self._validate_source(source)
            row_ids = self.project.add_rows(
                self.sheet_id,
                batch,
                self._column_ids,
                producer_id=self._producer_id,
                commit=False,
            )
            db.executemany(
                "UPDATE rows SET position=? WHERE id=?",
                (
                    (int(source["first_position"]) + offset, row_id)
                    for offset, row_id in enumerate(row_ids)
                ),
            )
            db.executemany(
                "INSERT INTO import_session_rows(session_id,ordinal,row_id) VALUES(?,?,?)",
                (
                    (self._session_id, session.committed_rows + offset, row_id)
                    for offset, row_id in enumerate(row_ids, 1)
                ),
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
            operation = db.execute(
                "SELECT undo_info FROM ops WHERE id=?", (self._op_id,)
            ).fetchone()
            replace = "replaced_rows_import_session_id" in json.loads(
                operation["undo_info"]
            )
            if replace:
                changed = db.execute(
                    "UPDATE rows SET hidden=1 WHERE id=? AND hidden=0",
                    (source["row_id"],),
                )
                if changed.rowcount != 1:
                    raise ImportSessionConflict("split source visibility changed")
            db.execute(
                "UPDATE import_session_pdf_sources SET state='published',replaced=?,error=? WHERE session_id=? AND ordinal=?",
                (
                    int(replace),
                    None if source_error is None else str(source_error)[:1000],
                    self._session_id,
                    expected_cursor,
                ),
            )
            self._advance(
                expected_cursor, rows=len(row_ids), committed_bytes=committed_bytes
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._row_count += len(row_ids)
        return row_ids

    def _advance(
        self, expected_cursor: int, *, rows: int = 0, committed_bytes: int = 0
    ) -> None:
        updated = self.project.db.execute(
            "UPDATE import_sessions SET cursor=cursor+1,committed_rows=committed_rows+?,"
            "committed_bytes=committed_bytes+?,updated_at=datetime('now') "
            "WHERE id=? AND writer_authority=? AND cursor=? AND state='active'",
            (
                rows,
                committed_bytes,
                self._session_id,
                self._writer_authority,
                expected_cursor,
            ),
        )
        if updated.rowcount != 1:
            raise ImportSessionConflict("split source cursor changed")

    def fail_source(self, *, expected_cursor: int, error: str) -> None:
        """Record one failed occurrence and continue without changing its row."""
        if not error:
            raise ValueError("source error must be non-empty")
        db = self.project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            self._require_split_writer(expected_cursor)
            changed = db.execute(
                "UPDATE import_session_pdf_sources SET state='failed',error=? "
                "WHERE session_id=? AND ordinal=? AND state='pending'",
                (str(error)[:1000], self._session_id, expected_cursor),
            )
            if changed.rowcount != 1:
                raise ImportSessionConflict("split source checkpoint is unavailable")
            self._advance(expected_cursor)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def finalize(
        self,
        *,
        expected_cursor: int,
        stopped: bool = False,
        setup_failed: bool = False,
    ) -> StreamingSheetPublication:
        """Release the protected writer; Redo only restores saved publications."""
        db = self.project.db
        db.execute("BEGIN IMMEDIATE")
        try:
            self._require_session()
            session = ImportSessionStore(self.project).require_writer(
                self._session_id,
                self._writer_authority,
                expected_cursor,
                states=("active", "paused", "cancelled"),
            )
            counts = {
                str(row[0]): int(row[1])
                for row in db.execute(
                    "SELECT state,COUNT(*) FROM import_session_pdf_sources WHERE session_id=? GROUP BY state",
                    (self._session_id,),
                )
            }
            if counts.get("pending", 0) and not stopped:
                raise ImportSessionConflict("split has unpublished sources")
            published = counts.get("published", 0)
            published_errors = int(
                db.execute(
                    "SELECT COUNT(*) FROM import_session_pdf_sources WHERE session_id=? AND state='published' AND error IS NOT NULL",
                    (self._session_id,),
                ).fetchone()[0]
            )
            counts["failed"] = counts.get("failed", 0) + published_errors
            stopped_remaining = stopped and bool(counts.get("pending", 0))
            if setup_failed:
                status = "failed"
            elif not published:
                status = (
                    "cancelled"
                    if stopped_remaining and not counts.get("failed")
                    else "failed"
                )
            elif counts.get("failed", 0) or stopped_remaining:
                status = "partial"
            else:
                status = "completed"
            stored = ReceiptStore(self.project).find_by_id(self._receipt_id)
            if stored is None or stored.status != "running":
                raise ImportSessionConflict("split receipt is no longer running")
            receipt = self._completed_session_receipt(
                stored.parsed(), row_count=session.committed_rows, status="completed"
            )
            receipt = receipt.model_copy(
                update={
                    "status": status,
                    "warnings": [
                        *receipt.warnings,
                        *(
                            ["Split stopped; published rows were kept."]
                            if stopped_remaining and published
                            else []
                        ),
                        *(
                            [f"{counts['failed']} PDF source(s) could not be split."]
                            if counts.get("failed")
                            else []
                        ),
                    ],
                }
            )
            if not ReceiptStore(self.project).update_body_status(
                receipt, require_status="running", commit=False
            ):
                raise ImportSessionConflict("split receipt finalization lost its CAS")
            db.execute("UPDATE ops SET barrier=0 WHERE id=?", (self._op_id,))
            db.execute(
                "UPDATE import_sessions SET state=?,updated_at=datetime('now') WHERE id=?",
                (
                    "kept"
                    if status in ("partial", "cancelled", "failed")
                    else "completed",
                    self._session_id,
                ),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        self._closed = True
        self._publication = StreamingSheetPublication(
            sheet_id=self.sheet_id,
            sheet_name=self._requested_sheet_name,
            row_count=session.committed_rows,
            op_id=self._op_id,
            receipt_id=self._receipt_id,
            receipt=receipt,
        )
        return self._publication


__all__ = ["PdfSplitSource", "PdfSplitWriter"]


def _validate_source_cell(project: Any, source: Mapping[str, Any]) -> None:
    if source["row_id"] is None:
        return
    row = project.db.execute(
        "SELECT 1 FROM rows JOIN sheets ON sheets.id=rows.sheet_id "
        "JOIN columns ON columns.sheet_id=rows.sheet_id "
        "WHERE rows.id=? AND rows.sheet_id=? AND rows.hidden=0 "
        "AND sheets.hidden=0 AND columns.id=? AND columns.active=1 "
        "AND columns.type='file'",
        (source["row_id"], source["sheet_id"], source["column_id"]),
    ).fetchone()
    if row is None:
        raise ImportSessionConflict("split source cell is unavailable")
    values = project.get_values(
        int(source["sheet_id"]), int(source["column_id"]), [int(source["row_id"])]
    )
    if values.get(int(source["row_id"])) != json.loads(source["expected_value"]):
        raise ImportSessionConflict("split source file value changed")

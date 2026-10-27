"""Durable checkpoint and writer fencing for progressively visible imports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal


ImportSessionState = Literal[
    "active", "paused", "cancelled", "kept", "completed", "removed"
]


class ImportSessionConflict(RuntimeError):
    """The caller no longer owns the session or supplied a stale cursor."""


class ImportInProgress(RuntimeError):
    """A mutation or action targeted a sheet owned by an unfinished import."""

    def __init__(self, session: ImportSession) -> None:
        self.sheet_id = session.sheet_id
        self.session_id = session.id
        self.state = session.state
        super().__init__(f"sheet {session.sheet_id} has an import in progress")


@dataclass(frozen=True)
class ImportSession:
    id: str
    sheet_id: int | None
    producer_id: int | None
    op_id: int
    receipt_id: str | None
    writer_authority: str
    cursor: int
    committed_rows: int
    committed_bytes: int
    state: ImportSessionState

    @classmethod
    def from_row(cls, row: Any) -> ImportSession:
        return cls(
            id=str(row["id"]),
            sheet_id=None if row["sheet_id"] is None else int(row["sheet_id"]),
            producer_id=(
                None if row["producer_id"] is None else int(row["producer_id"])
            ),
            op_id=int(row["op_id"]),
            receipt_id=(None if row["receipt_id"] is None else str(row["receipt_id"])),
            writer_authority=str(row["writer_authority"]),
            cursor=int(row["cursor"]),
            committed_rows=int(row["committed_rows"]),
            committed_bytes=int(row["committed_bytes"]),
            state=str(row["state"]),  # type: ignore[arg-type]
        )


class ImportSessionStore:
    def __init__(self, project: Any) -> None:
        self.project = project

    @property
    def db(self) -> Any:
        return self.project.db

    def get(self, session_id: str) -> ImportSession | None:
        row = self.db.execute(
            "SELECT id,sheet_id,producer_id,op_id,receipt_id,writer_authority,"
            "cursor,committed_rows,committed_bytes,state "
            "FROM import_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return None if row is None else ImportSession.from_row(row)

    def require_writer(
        self,
        session_id: str,
        authority: str,
        expected_cursor: int,
        *,
        states: tuple[ImportSessionState, ...] = ("active",),
    ) -> ImportSession:
        session = self.get(session_id)
        if (
            session is None
            or session.writer_authority != authority
            or session.cursor != int(expected_cursor)
            or session.state not in states
        ):
            raise ImportSessionConflict("import session writer or cursor is stale")
        return session

    def replace_writer(
        self, session_id: str, authority: str, *, expected_cursor: int
    ) -> ImportSession:
        if not authority:
            raise ValueError("writer authority must be non-empty")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.db.execute(
                "UPDATE import_sessions SET writer_authority=?,state='active',"
                "updated_at=datetime('now') WHERE id=? AND cursor=? "
                "AND state IN ('active','paused')",
                (authority, session_id, int(expected_cursor)),
            )
            if updated.rowcount != 1:
                raise ImportSessionConflict("import session cannot be resumed")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        session = self.get(session_id)
        if session is None:  # pragma: no cover - protected by the update above
            raise ImportSessionConflict("import session disappeared")
        return session

    def upgrade_append_undo(self, session: ImportSession) -> None:
        """Adopt ledger membership for an append resumed across an upgrade.

        Runs inside the page transaction: older append sessions stored row IDs
        in the operation, but already recorded the same durable membership.
        """
        operation = self.db.execute(
            "SELECT undo_info FROM ops WHERE id=?", (session.op_id,)
        ).fetchone()
        if operation is None:
            raise ImportSessionConflict("import operation is unavailable")
        undo = json.loads(operation["undo_info"] or "{}")
        if (
            "created_rows" not in undo
            or "created_sheets" in undo
            or "created_rows_import_session_id" in undo
        ):
            return
        count = self.db.execute(
            "SELECT COUNT(*) FROM import_session_rows WHERE session_id=?",
            (session.id,),
        ).fetchone()[0]
        if count != session.committed_rows:
            raise ImportSessionConflict("import row membership is incomplete")
        undo["created_rows"] = []
        undo["created_rows_import_session_id"] = session.id
        self.db.execute(
            "UPDATE ops SET undo_info=? WHERE id=?",
            (json.dumps(undo), session.op_id),
        )

    def set_state(
        self,
        session_id: str,
        authority: str,
        expected_cursor: int,
        *,
        from_states: tuple[ImportSessionState, ...],
        state: ImportSessionState,
    ) -> ImportSession:
        placeholders = ",".join("?" for _ in from_states)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.db.execute(
                "UPDATE import_sessions SET state=?,updated_at=datetime('now') "
                "WHERE id=? AND writer_authority=? AND cursor=? "
                f"AND state IN ({placeholders})",
                (state, session_id, authority, int(expected_cursor), *from_states),
            )
            if updated.rowcount != 1:
                raise ImportSessionConflict("import session state changed")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        session = self.get(session_id)
        if session is None:  # pragma: no cover
            raise ImportSessionConflict("import session disappeared")
        return session


def active_import_session(db: Any, sheet_id: int) -> ImportSession | None:
    row = db.execute(
        "SELECT id,sheet_id,producer_id,op_id,receipt_id,writer_authority,"
        "cursor,committed_rows,committed_bytes,state FROM import_sessions "
        "WHERE sheet_id=? AND state IN ('active','paused','cancelled')",
        (int(sheet_id),),
    ).fetchone()
    if row is None:
        return None
    # Existing-sheet imports own only their checkpointed rows. They deliberately
    # do not lock unrelated sheet mutations; ``created_rows`` marks that narrow
    # scope while import_session_rows carries its durable membership.
    operation = db.execute(
        "SELECT kind,undo_info FROM ops WHERE id=?", (int(row["op_id"]),)
    ).fetchone()
    if operation is not None:
        if operation["kind"] == "pdf.split":
            return ImportSession.from_row(row)
        undo_info = json.loads(operation["undo_info"] or "{}")
        if "created_rows" in undo_info and "created_sheets" not in undo_info:
            return None
    receipt_id = row["receipt_id"]
    if receipt_id is not None:
        receipt = db.execute(
            "SELECT body FROM receipts WHERE id=?", (str(receipt_id),)
        ).fetchone()
        if receipt is not None:
            body = json.loads(receipt["body"] or "{}")
            if any(
                isinstance(item, dict)
                and isinstance(item.get("ref"), dict)
                and item["ref"].get("kind") == "pdf_import_commit"
                for item in body.get("inputs", ())
            ):
                # PDF commits intentionally expose their sheet immediately;
                # result actions and ordinary edits may target committed rows
                # while later file occurrences continue to arrive.
                return None
    return ImportSession.from_row(row)


def set_import_session_rows_hidden(
    db: Any,
    *,
    op_id: int,
    session_id: str,
    hidden: bool,
) -> int:
    """Toggle rows owned by one import operation through its durable ledger."""

    session = db.execute(
        "SELECT committed_rows FROM import_sessions WHERE id=? AND op_id=?",
        (session_id, int(op_id)),
    ).fetchone()
    if session is None:
        raise ImportSessionConflict("import operation row membership is unavailable")
    updated = db.execute(
        "UPDATE rows SET hidden=? WHERE id IN ("
        "SELECT member.row_id FROM import_session_rows AS member "
        "JOIN import_sessions AS session ON session.id=member.session_id "
        "WHERE member.session_id=? AND session.op_id=?"
        ")",
        (int(hidden), session_id, int(op_id)),
    )
    updated_rows = int(updated.rowcount)
    if updated_rows != int(session["committed_rows"]):
        raise ImportSessionConflict("import operation row membership is incomplete")
    return updated_rows


def set_import_session_replaced_rows_hidden(
    db: Any, *, op_id: int, session_id: str, hidden: bool
) -> int:
    """Toggle only originals whose source publication committed successfully."""
    session = db.execute(
        "SELECT committed_rows FROM import_sessions WHERE id=? AND op_id=?",
        (session_id, int(op_id)),
    ).fetchone()
    if session is None:
        raise ImportSessionConflict("split operation membership is unavailable")
    membership = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN state='published' THEN child_count ELSE 0 END),0),"
        "SUM(state='published'),SUM(replaced=1) FROM import_session_pdf_sources "
        "WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if int(membership[0]) != int(session["committed_rows"]) or int(
        membership[1] or 0
    ) != int(membership[2] or 0):
        raise ImportSessionConflict("split operation membership is incomplete")
    expected = int(membership[2] or 0)
    updated = db.execute(
        "UPDATE rows SET hidden=? WHERE id IN ("
        "SELECT row_id FROM import_session_pdf_sources "
        "WHERE session_id=? AND replaced=1)",
        (int(hidden), session_id),
    )
    if updated.rowcount != expected:
        raise ImportSessionConflict("split operation membership is incomplete")
    return int(updated.rowcount)


def import_session_generation_is_applied(
    db: Any, *, session_id: str, writer_authority: str, op_id: int
) -> bool:
    """Check admitted PDF preparation against its terminal publication owner.

    Recheck inside the prepared-content publication transaction. A completed
    split's writer token is its preparation generation; Undo rotates that
    token, and Redo restores saved rows without admitting the old worker again.
    """
    return (
        db.execute(
            "SELECT 1 FROM import_sessions AS session JOIN ops ON ops.id=session.op_id "
            "WHERE session.id=? AND session.writer_authority=? AND session.op_id=? "
            "AND session.state IN ('kept','completed') "
            "AND ops.kind='pdf.split' AND ops.status='applied'",
            (session_id, writer_authority, int(op_id)),
        ).fetchone()
        is not None
    )


def invalidate_pdf_split_generation(db: Any, *, op_id: int) -> None:
    """Invalidate queued preparation in the same transaction as split Undo."""
    if not db.in_transaction:
        raise RuntimeError(
            "split generation invalidation requires a history transaction"
        )
    db.execute(
        "UPDATE import_sessions SET writer_authority='undone:' || lower(hex(randomblob(16))),"
        "updated_at=datetime('now') WHERE op_id=? "
        "AND EXISTS(SELECT 1 FROM ops WHERE id=? AND kind='pdf.split')",
        (int(op_id), int(op_id)),
    )


def require_import_sheet_write(
    db: Any, sheet_id: int, *, producer_id: int | None = None
) -> None:
    """Block writes during import, except its active append producer."""

    session = active_import_session(db, sheet_id)
    if session is None:
        return
    if (
        session.state == "active"
        and producer_id is not None
        and session.producer_id == int(producer_id)
    ):
        return
    raise ImportInProgress(session)


def require_cancelled_import_removal(
    db: Any,
    sheet_id: int,
    *,
    producer_id: int,
    writer_authority: str,
    expected_cursor: int,
) -> ImportSession:
    """Authorize only the explicit Remove choice for a cancelled session."""

    session = active_import_session(db, sheet_id)
    if (
        session is None
        or session.state != "cancelled"
        or session.producer_id != int(producer_id)
        or session.writer_authority != writer_authority
        or session.cursor != int(expected_cursor)
    ):
        if session is not None:
            raise ImportInProgress(session)
        raise ImportSessionConflict("cancelled import removal authority is stale")
    return session


__all__ = [
    "ImportSession",
    "ImportSessionConflict",
    "ImportInProgress",
    "ImportSessionState",
    "ImportSessionStore",
    "active_import_session",
    "require_cancelled_import_removal",
    "require_import_sheet_write",
    "set_import_session_rows_hidden",
    "set_import_session_replaced_rows_hidden",
    "import_session_generation_is_applied",
    "invalidate_pdf_split_generation",
]

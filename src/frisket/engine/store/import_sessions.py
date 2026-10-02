"""Durable checkpoint and writer fencing for progressively visible imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ImportSessionState = Literal[
    "active", "paused", "cancelled", "kept", "completed", "removed"
]


class ImportSessionConflict(RuntimeError):
    """The caller no longer owns the session or supplied a stale cursor."""


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


__all__ = [
    "ImportSession",
    "ImportSessionConflict",
    "ImportSessionState",
    "ImportSessionStore",
]

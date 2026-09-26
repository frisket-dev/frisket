"""Bounded reads of current cell text; callers enforce the submitted Ask scope."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from typing import Any

from frisket.engine.store import Project

MAX_SOURCE_CHARS = 8_000
MAX_SCAN_CHARS = 1_048_576
SCAN_SECONDS = 2.0
_TEXT = "CASE WHEN c.validity='valid' THEN CAST(json_extract(c.value,'$') AS TEXT) END"
_FROM = (
    " FROM current_cells c JOIN rows r ON r.id=c.row_id "
    "JOIN columns col ON col.id=c.column_id JOIN sheets s ON s.id=r.sheet_id "
    "WHERE r.sheet_id=? AND r.id=? AND c.column_id=? "
    "AND col.sheet_id=r.sheet_id AND r.hidden=0 AND col.hidden=0 AND s.hidden=0"
)


@contextmanager
def _read(project: Project, cancel: threading.Event | None):
    deadline = time.monotonic() + SCAN_SECONDS
    with closing(project.read_snapshot()) as snapshot:
        snapshot.db.set_progress_handler(
            lambda: int(
                bool(cancel and cancel.is_set()) or time.monotonic() >= deadline
            ),
            1_000,
        )
        try:
            if cancel and cancel.is_set():
                raise InterruptedError("source read cancelled")
            yield snapshot.db, deadline
        except sqlite3.OperationalError as error:
            if "interrupted" in str(error):
                raise InterruptedError(
                    "source read stopped or exceeded its time limit"
                ) from error
            raise
        finally:
            snapshot.db.set_progress_handler(None, 0)


def _metadata(db: sqlite3.Connection, cell: tuple[int, int, int]) -> dict[str, Any]:
    row = db.execute(
        f"SELECT length({_TEXT}) AS length, c.origin_kind,c.origin_op_id,"
        "c.origin_run_id,c.base_producer_id,c.validity" + _FROM,
        cell,
    ).fetchone()
    if row is None:
        raise ValueError("source is hidden or no longer available")
    ref = {
        "kind": row["origin_kind"],
        "op_id": row["origin_op_id"],
        "row_id": cell[1],
        "column_id": cell[2],
        "run_id": row["origin_run_id"],
        "validity": row["validity"],
    }
    return {
        "length": row["length"] or 0,
        "value_ref": ref,
        "version": {**ref, "base_producer_id": row["base_producer_id"]},
    }


def _start(meta: dict[str, Any], cursor: dict[str, Any] | None, start: int) -> int:
    if cursor is not None:
        if not isinstance(cursor, dict) or set(cursor) != {"version", "start"}:
            raise ValueError("source cursor must contain version and start")
        if cursor["version"] != meta["version"]:
            raise ValueError(
                "source_changed: search or open the source again before continuing"
            )
        start = cursor["start"]
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or not 0 <= start <= meta["length"]
    ):
        raise ValueError("source offset is outside the current text")
    return start


def _text(
    db: sqlite3.Connection, cell: tuple[int, int, int], start: int, size: int
) -> str:
    row = db.execute(
        f"SELECT substr({_TEXT},?,?) AS text" + _FROM, (start + 1, size, *cell)
    ).fetchone()
    return str(row["text"] or "")


def read_source_text(
    project: Project,
    cell: tuple[int, int, int],
    *,
    cursor: dict[str, Any] | None = None,
    start: int = 0,
    anchor: str | None = None,
    limit: int = MAX_SOURCE_CHARS,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Return only a bounded SQL substring, never a whole Python document copy."""
    if not 1 <= limit <= MAX_SOURCE_CHARS:
        raise ValueError("invalid source read size")
    with _read(project, cancel) as (db, _deadline):
        meta = _metadata(db, cell)
        start = _start(meta, cursor, start)
        if cursor is None and anchor:
            located = db.execute(
                f"SELECT instr({_TEXT},?) AS position" + _FROM, (anchor[:500], *cell)
            ).fetchone()["position"]
            if located:
                start = max(0, located - 1 - 500)
        text = _text(db, cell, start, limit)
        end = start + len(text)
        return {
            **meta,
            "text": text,
            "range": {"start": start, "end": end},
            "reached_end": end >= meta["length"],
            "next_cursor": {"version": meta["version"], "start": end}
            if end < meta["length"]
            else None,
        }


def find_source_text(
    project: Project,
    cell: tuple[int, int, int],
    literal: str,
    *,
    cursor: dict[str, Any] | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Find literal matches in a bounded window with exact Unicode offsets."""
    if not isinstance(literal, str) or not literal or len(literal) > 500:
        raise ValueError("literal must contain 1 to 500 characters")
    with _read(project, cancel) as (db, deadline):
        meta = _metadata(db, cell)
        start = _start(meta, cursor, 0)
        # The overlap is revisited on continuation so boundary-spanning matches survive.
        text = _text(db, cell, start, MAX_SCAN_CHARS)
        matches = []
        position = 0
        exhausted = True
        while position < len(text):
            if cancel and cancel.is_set():
                raise InterruptedError("source find cancelled")
            if time.monotonic() >= deadline:
                exhausted = False
                break
            found = text.find(literal, position)
            if found < 0:
                position = len(text)
                break
            left, right = (
                max(0, found - 120),
                min(len(text), found + len(literal) + 120),
            )
            matches.append(
                {
                    "start": start + found,
                    "end": start + found + len(literal),
                    "text": text[left:right],
                    "context_start": start + left,
                }
            )
            position = found + len(literal)
            if len(matches) == 10:
                exhausted = False
                break
        scanned_end = start + (len(text) if exhausted else position)
        reached_end = exhausted and scanned_end >= meta["length"]
        next_start = (
            max(start + 1, scanned_end - len(literal) + 1) if exhausted else scanned_end
        )
        return {
            **meta,
            "matches": matches,
            "scanned_range": {"start": start, "end": scanned_end},
            "reached_end": reached_end,
            "next_cursor": None
            if reached_end
            else {"version": meta["version"], "start": next_start},
        }

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
        "c.origin_run_id,c.base_producer_id,c.validity,col.type AS column_type" + _FROM,
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
        "column_type": row["column_type"],
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


def read_prepared_passages(
    project: Project,
    cell: tuple[int, int, int],
    *,
    expected_version: dict[str, Any],
    start: int,
    end: int,
    limit: int,
    evidence_link_id: str | None = None,
    span_id: str | None = None,
    cancel: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Project bounded, current evidence quotes without building a media viewer.

    Reuse the canonical evidence links/spans and their hash check. Even hash
    verification reads bounded SQL slices, rather than a whole Python cell.
    """
    import hashlib
    import json

    with _read(project, cancel) as (db, deadline):
        meta = _metadata(db, cell)
        if meta["version"] != expected_version:
            raise ValueError(
                "source_changed: reopen this source before reading its evidence"
            )
        ref = {
            key: value for key, value in meta["value_ref"].items() if key != "validity"
        }
        ref_json = json.dumps(
            ref, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        conditions = [
            "l.sheet_id=?",
            "l.row_id=?",
            "l.column_id=?",
            "l.status='active'",
            "l.subject_ref_json=?",
        ]
        params: list[Any] = [*cell, ref_json]
        if evidence_link_id:
            conditions.append("l.stable_id=?")
            params.append(evidence_link_id)
        if span_id:
            conditions.append("sp.stable_id=?")
            params.append(span_id)
        elif meta["length"] > MAX_SOURCE_CHARS:
            # Character coordinates belong to their explicitly named text surface.
            # Older page/time-only spans can still match the bounded passage quote.
            conditions.append(
                "((ts.value_ref_json=? AND sp.char_start < ? AND sp.char_end > ?) "
                "OR instr(?, substr(coalesce(sp.quote,sp.snippet),1,80)) > 0)"
            )
            params.extend([ref_json, end, start, _text(db, cell, start, end - start)])
        rows = db.execute(
            "SELECT l.stable_id AS evidence_link_id, a.stable_id AS artifact_id, "
            "sp.stable_id AS span_id, substr(coalesce(sp.quote,sp.snippet),1,?) AS text, "
            "sp.text_layer_hash, sp.page_start,sp.page_end,sp.start_ms,sp.end_ms, "
            "sp.bbox_json, substr(coalesce(a.title,a.filename,''),1,240) AS title "
            "FROM evidence_links l JOIN evidence_link_spans ls ON ls.link_id=l.id "
            "JOIN source_spans sp ON sp.id=ls.span_id JOIN source_artifacts a ON a.id=sp.artifact_id "
            "LEFT JOIN text_surfaces ts ON ts.id=sp.text_surface_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY l.id,ls.rank,sp.id LIMIT 32",
            [limit, *params],
        ).fetchall()
        current_hash = None
        if any(row["text_layer_hash"] for row in rows):
            digest = hashlib.sha256()
            for offset in range(0, meta["length"], 65536):
                if (cancel and cancel.is_set()) or time.monotonic() >= deadline:
                    raise InterruptedError(
                        "evidence read stopped or exceeded its time limit"
                    )
                digest.update(_text(db, cell, offset, 65536).encode("utf-8"))
            current_hash = "sha256:" + digest.hexdigest()
        passages = []
        for row in rows:
            if row["text_layer_hash"] and row["text_layer_hash"] != current_hash:
                continue
            text = str(row["text"] or "")[:limit]
            if not text:
                continue
            passage = dict(row)
            passage.pop("text_layer_hash")
            passage["bbox"] = json.loads(passage.pop("bbox_json"))
            passage["text"] = text
            passages.append(passage)
            limit -= len(text)
            if limit <= 0:
                break
        return passages

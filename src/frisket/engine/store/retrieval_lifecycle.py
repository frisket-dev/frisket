"""Authoritative current-cell revisions for retrieval bindings."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

from .current_cells import stamp_retrieval_revision
from .search_index_work import enqueue_dirty_scope, latest_revision

RETRIEVAL_EVIDENCE_LINK_ROLES = frozenset(
    {
        "media_ocr_grounding",
        "media_transcribe_temporal",
        "temporal_transcript_projection",
    }
)

_PAIR_CHUNK_SIZE = 400


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _pairs(values: Collection[tuple[int, int]]) -> list[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for row_id, column_id in values:
        pairs.add(
            (
                _positive_id(row_id, "row_id"),
                _positive_id(column_id, "column_id"),
            )
        )
    return sorted(pairs, key=lambda pair: (pair[1], pair[0]))


def _chunks(
    values: list[tuple[int, int]], size: int = _PAIR_CHUNK_SIZE
) -> list[list[tuple[int, int]]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


def _requested_sql(pairs: list[tuple[int, int]]) -> tuple[str, list[int]]:
    values = ",".join("(?,?)" for _ in pairs)
    params = [item for pair in pairs for item in pair]
    return f"requested(row_id,column_id) AS (VALUES {values})", params


def read_binding_revision(
    db: sqlite3.Connection, *, row_id: int, column_id: int
) -> int | None:
    """Read one live binding revision by its indexed current-cell key."""

    row = db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE column_id=? AND row_id=?",
        (
            _positive_id(column_id, "column_id"),
            _positive_id(row_id, "row_id"),
        ),
    ).fetchone()
    return None if row is None else int(row[0])


def read_binding_revisions(
    db: sqlite3.Connection, pairs: Collection[tuple[int, int]]
) -> dict[tuple[int, int], int]:
    """Read existing live revisions for exact ``(row_id, column_id)`` pairs."""

    requested = _pairs(pairs)
    revisions: dict[tuple[int, int], int] = {}
    for chunk in _chunks(requested):
        cte, params = _requested_sql(chunk)
        rows = db.execute(
            f"WITH {cte} "
            "SELECT head.row_id,head.column_id,head.retrieval_revision "
            "FROM requested target "
            "CROSS JOIN current_cells head ON head.column_id=target.column_id "
            "AND head.row_id=target.row_id",
            params,
        ).fetchall()
        revisions.update({(int(row[0]), int(row[1])): int(row[2]) for row in rows})
    return revisions


def _contiguous_ranges(row_ids: list[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = end = row_ids[0]
    for row_id in row_ids[1:]:
        if row_id != end + 1:
            ranges.append((start, end))
            start = row_id
        end = row_id
    ranges.append((start, end))
    return ranges


def touch_binding_revisions(
    db: sqlite3.Connection, pairs: Collection[tuple[int, int]]
) -> int:
    """Invalidate and restamp existing bindings whose represented source changed.

    This is for source publications such as OCR/transcript evidence that can
    change a representation without replacing the cell value. Ordinary cell
    writers receive the same treatment as part of current-cell refresh.
    """

    if not db.in_transaction:
        raise RuntimeError("binding revision writes require a caller transaction")
    requested = _pairs(pairs)
    if not requested:
        return 0

    existing: dict[tuple[int, int], int] = {}
    for chunk in _chunks(requested):
        cte, params = _requested_sql(chunk)
        rows = db.execute(
            f"WITH {cte} "
            "SELECT head.row_id,head.column_id,column_meta.sheet_id "
            "FROM requested target "
            "CROSS JOIN current_cells head ON head.column_id=target.column_id "
            "AND head.row_id=target.row_id "
            "JOIN columns column_meta ON column_meta.id=head.column_id",
            params,
        ).fetchall()
        existing.update({(int(row[0]), int(row[1])): int(row[2]) for row in rows})
    if not existing:
        return 0

    rows_by_column: dict[tuple[int, int], list[int]] = {}
    for (row_id, column_id), sheet_id in existing.items():
        rows_by_column.setdefault((sheet_id, column_id), []).append(row_id)
    for (sheet_id, column_id), row_ids in sorted(rows_by_column.items()):
        for start, end in _contiguous_ranges(sorted(row_ids)):
            enqueue_dirty_scope(
                db,
                sheet_id=sheet_id,
                column_id=column_id,
                row_id_start=start,
                row_id_end=end,
            )
    revision = latest_revision(db)
    stamp_retrieval_revision(db, revision, pairs=existing)
    return len(existing)


__all__ = [
    "RETRIEVAL_EVIDENCE_LINK_ROLES",
    "read_binding_revision",
    "read_binding_revisions",
    "touch_binding_revisions",
]

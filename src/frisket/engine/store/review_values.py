"""Bounded reads for values displayed by Review bundle pages."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from frisket.engine.store.current_value_windows import (
    MANUAL_EDIT_BYTE_LIMIT,
    PREVIEW_CODEPOINT_LIMIT,
    prepared_text_sizes,
    read_prepared_text,
    read_sql_text,
    sql_text_sizes,
)
from frisket.engine.store.value_codec import decode_stored_value


_TEXTUAL_KINDS = frozenset({"text", "json", "bigint", "legacy_invalid"})
_QUERY_CHUNK_SIZE = 300


@dataclass(frozen=True, slots=True)
class ReviewBoundedText:
    preview: str
    total_characters: int
    total_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "preview": self.preview,
            "total_characters": self.total_characters,
            "total_bytes": self.total_bytes,
            "truncated": True,
            "download_href": None,
        }


@dataclass(frozen=True, slots=True)
class ReviewResultRead:
    value: Any
    bounded_value: ReviewBoundedText | None
    justification: str | None
    bounded_justification: ReviewBoundedText | None


def _text_read(
    db: sqlite3.Connection,
    *,
    select_value: str,
    params: dict[str, int],
) -> ReviewBoundedText:
    sizes = sql_text_sizes(db, select_value, params)
    if sizes is None:
        return ReviewBoundedText(preview="", total_characters=0, total_bytes=0)
    return ReviewBoundedText(
        preview=read_sql_text(
            db,
            select_value,
            params,
            start=0,
            limit=PREVIEW_CODEPOINT_LIMIT,
            sizes=sizes,
        ),
        total_characters=sizes[0],
        total_bytes=sizes[1],
    )


def read_review_results(
    db: sqlite3.Connection,
    coordinates: Collection[tuple[int, int, int]],
) -> dict[tuple[int, int, int], ReviewResultRead]:
    """Read result values and justifications without materializing large text."""

    requested = list(
        dict.fromkeys(tuple(map(int, coordinate)) for coordinate in coordinates)
    )
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(requested), _QUERY_CHUNK_SIZE):
        batch = requested[offset : offset + _QUERY_CHUNK_SIZE]
        values = ",".join("(?,?,?)" for _ in batch)
        params = [part for coordinate in batch for part in coordinate]
        rows.extend(
            db.execute(
                f"WITH requested(run_id,row_id,column_id) AS (VALUES {values}) "
                "SELECT result.run_id,result.row_id,result.column_id,result.value_kind,"
                "CASE WHEN result.value_kind IN "
                "('text','json','bigint','legacy_invalid') "
                "AND length(CAST(result.value AS BLOB))>? "
                "THEN NULL ELSE result.value END AS bounded_value,"
                "length(CAST(result.value AS BLOB)) AS value_bytes,"
                "CASE WHEN result.justification IS NOT NULL "
                "AND length(CAST(result.justification AS BLOB))>? "
                "THEN NULL ELSE result.justification END AS bounded_justification,"
                "length(CAST(result.justification AS BLOB)) AS justification_bytes "
                "FROM requested CROSS JOIN results result "
                "ON result.run_id=requested.run_id "
                "AND result.row_id=requested.row_id "
                "AND result.column_id=requested.column_id",
                (*params, MANUAL_EDIT_BYTE_LIMIT, MANUAL_EDIT_BYTE_LIMIT),
            ).fetchall()
        )

    prepared_ids = [
        int(row["bounded_value"])
        for row in rows
        if row["value_kind"] == "prepared_content_ref"
        and row["bounded_value"] is not None
    ]
    prepared_sizes = prepared_text_sizes(db, prepared_ids)
    output: dict[tuple[int, int, int], ReviewResultRead] = {}
    for row in rows:
        coordinate = (
            int(row["run_id"]),
            int(row["row_id"]),
            int(row["column_id"]),
        )
        query_params = {
            "run_id": coordinate[0],
            "row_id": coordinate[1],
            "column_id": coordinate[2],
        }
        value_kind = row["value_kind"]
        stored_value = row["bounded_value"]
        bounded_value: ReviewBoundedText | None = None
        if (
            value_kind in _TEXTUAL_KINDS
            and stored_value is None
            and row["value_bytes"] is not None
            and int(row["value_bytes"]) > MANUAL_EDIT_BYTE_LIMIT
        ):
            bounded_value = _text_read(
                db,
                select_value=(
                    "SELECT value FROM results WHERE run_id=:run_id "
                    "AND row_id=:row_id AND column_id=:column_id"
                ),
                params=query_params,
            )
            value = None
        elif value_kind == "prepared_content_ref" and stored_value is not None:
            ref_id = int(stored_value)
            sizes = prepared_sizes.get(ref_id)
            if sizes is None:
                value = None
            elif sizes[1] > MANUAL_EDIT_BYTE_LIMIT:
                bounded_value = ReviewBoundedText(
                    preview=read_prepared_text(db, ref_id, 0, PREVIEW_CODEPOINT_LIMIT),
                    total_characters=sizes[0],
                    total_bytes=sizes[1],
                )
                value = None
            else:
                value = read_prepared_text(db, ref_id, 0, sizes[0])
        elif value_kind == "legacy_invalid":
            try:
                value = decode_stored_value(value_kind, stored_value)
            except (TypeError, ValueError, OverflowError, RecursionError):
                value = stored_value
        else:
            value = decode_stored_value(value_kind, stored_value, tolerate_errors=True)

        justification = row["bounded_justification"]
        bounded_justification: ReviewBoundedText | None = None
        if (
            justification is None
            and row["justification_bytes"] is not None
            and int(row["justification_bytes"]) > MANUAL_EDIT_BYTE_LIMIT
        ):
            bounded_justification = _text_read(
                db,
                select_value=(
                    "SELECT justification AS value FROM results "
                    "WHERE run_id=:run_id AND row_id=:row_id "
                    "AND column_id=:column_id"
                ),
                params=query_params,
            )
        output[coordinate] = ReviewResultRead(
            value=value,
            bounded_value=bounded_value,
            justification=(str(justification) if justification is not None else None),
            bounded_justification=bounded_justification,
        )
    return output


__all__ = ["ReviewBoundedText", "ReviewResultRead", "read_review_results"]

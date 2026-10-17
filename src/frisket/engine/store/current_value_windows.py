"""Bounded reads of authoritative current-cell values.

Large text stays in its existing authority table.  This module reads compact
metadata and SQL substrings without handing a whole multi-megabyte value to
Python.  Prepared document text is assembled only for the requested range,
including the canonical two-newline separators between physical pages.
"""

from __future__ import annotations

import codecs
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

from .value_codec import decode_stored_value


MANUAL_EDIT_BYTE_LIMIT = 64 * 1024
PREVIEW_CODEPOINT_LIMIT = 2_048
PREVIEW_BYTE_LIMIT = 8 * 1024
TEXT_WINDOW_CODEPOINT_LIMIT = 16_384
TEXT_WINDOW_LINE_LIMIT = 400
_ROW_CHUNK_SIZE = 400
_TEXTUAL_KINDS = frozenset({"text", "json", "bigint", "legacy_invalid"})
_SCALAR_KINDS = frozenset({"integer", "real", "boolean"})
_BLOB_CHUNK_BYTES = 64 * 1024


class CurrentValueUnavailable(ValueError):
    """The coordinate is hidden, missing, or has no current value."""


class CurrentValueChanged(ValueError):
    """The requested retrieval revision is no longer current."""


class CurrentValueNotTextual(ValueError):
    """The current value has no textual representation served by this API."""


@dataclass(frozen=True, slots=True)
class BoundedValuePreview:
    preview: str
    value_kind: str
    total_characters: int
    total_bytes: int
    retrieval_revision: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "preview": self.preview,
            "value_kind": self.value_kind,
            "total_characters": self.total_characters,
            "total_bytes": self.total_bytes,
            "retrieval_revision": self.retrieval_revision,
            "truncated": True,
            "manual_editable": False,
            "manual_edit_byte_limit": MANUAL_EDIT_BYTE_LIMIT,
        }


@dataclass(frozen=True, slots=True)
class CurrentCellRead:
    value: Any
    value_present: bool
    value_ref: dict[str, Any]
    validity: str
    bounded_value: BoundedValuePreview | None = None


@dataclass(frozen=True, slots=True)
class CurrentValueWindow:
    row_id: int
    column_id: int
    retrieval_revision: int
    value_kind: str
    total_characters: int
    total_bytes: int
    start: int
    text: str

    @property
    def end(self) -> int:
        return self.start + len(self.text)


@dataclass(frozen=True, slots=True)
class CurrentTextMetadata:
    value_kind: str
    prepared_ref_id: int | None
    retrieval_revision: int
    total_characters: int
    total_bytes: int
    has_nul: bool = False


_AUTHORITY_CTE = """
heads AS MATERIALIZED (
  SELECT r.id AS row_id,head.retrieval_revision,head.origin_kind,
         head.origin_op_id,head.origin_run_id,head.base_producer_id,
         COALESCE(head.validity,'missing') AS validity
  FROM rows AS r
  JOIN sheets AS sheet ON sheet.id=r.sheet_id AND sheet.hidden=0
  JOIN columns AS col ON col.sheet_id=r.sheet_id
    AND col.id=:column_id AND col.hidden=0
  LEFT JOIN current_cells AS head
    ON head.row_id=r.id AND head.column_id=:column_id
  WHERE r.sheet_id=:sheet_id AND r.hidden=0 AND r.id IN ({row_ids})
),
authority AS MATERIALIZED (
  SELECT head.*,
    CASE head.origin_kind
      WHEN 'source_cell' THEN (
        SELECT source.value_kind FROM cells AS source
        WHERE source.row_id=head.row_id AND source.column_id=:column_id
          AND source.producer_id IS head.base_producer_id)
      WHEN 'run_result' THEN (
        SELECT CASE result.publication_effect
          WHEN 'publish_value' THEN result.value_kind
          WHEN 'publish_null' THEN 'null' END
        FROM results AS result
        WHERE result.run_id=head.origin_run_id AND result.row_id=head.row_id
          AND result.column_id=:column_id)
      WHEN 'manual_edit' THEN (
        SELECT edit.value_kind FROM edits AS edit
        WHERE edit.op_id=head.origin_op_id AND edit.row_id=head.row_id
          AND edit.column_id=:column_id)
    END AS stored_kind,
    CASE head.origin_kind
      WHEN 'source_cell' THEN (
        SELECT source.value FROM cells AS source
        WHERE source.row_id=head.row_id AND source.column_id=:column_id
          AND source.producer_id IS head.base_producer_id)
      WHEN 'run_result' THEN (
        SELECT CASE WHEN result.publication_effect='publish_value'
          THEN result.value END FROM results AS result
        WHERE result.run_id=head.origin_run_id AND result.row_id=head.row_id
          AND result.column_id=:column_id)
      WHEN 'manual_edit' THEN (
        SELECT edit.value FROM edits AS edit
        WHERE edit.op_id=head.origin_op_id AND edit.row_id=head.row_id
          AND edit.column_id=:column_id)
    END AS stored_value
  FROM heads AS head
)
"""


def _authority_cte(row_ids: list[int]) -> str:
    placeholders = ",".join(f":row_{index}" for index in range(len(row_ids)))
    return _AUTHORITY_CTE.format(row_ids=placeholders)


def _params(sheet_id: int, column_id: int, row_ids: list[int]) -> dict[str, int]:
    return {
        "sheet_id": sheet_id,
        "column_id": column_id,
        **{f"row_{index}": row_id for index, row_id in enumerate(row_ids)},
    }


def _blob_text_chunks(
    db: sqlite3.Connection,
    select_value: str,
    params: dict[str, int],
    total_bytes: int,
) -> Iterator[str]:
    """Decode NUL-bearing UTF-8 through bounded BLOB slices.

    SQLite TEXT length/substr stop at NUL. BLOB slices preserve all bytes;
    incremental decoding also preserves characters crossing chunk boundaries.
    Ordinary text takes the direct SQL codepoint path instead.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    for offset in range(0, total_bytes, _BLOB_CHUNK_BYTES):
        row = db.execute(
            "SELECT substr(CAST(value AS BLOB),:byte_start,:byte_limit) FROM ("
            + select_value
            + ")",
            {**params, "byte_start": offset + 1, "byte_limit": _BLOB_CHUNK_BYTES},
        ).fetchone()
        yield decoder.decode(
            bytes(row[0]), final=offset + _BLOB_CHUNK_BYTES >= total_bytes
        )


def _sql_text_slice(
    db: sqlite3.Connection,
    select_value: str,
    params: dict[str, int],
    *,
    start: int,
    limit: int,
    has_nul: bool,
    total_bytes: int,
) -> str:
    if not has_nul:
        row = db.execute(
            "SELECT substr(value,:text_start,:text_limit) FROM (" + select_value + ")",
            {**params, "text_start": start + 1, "text_limit": limit},
        ).fetchone()
        return str(row[0] or "")
    fragments = []
    offset = 0
    for text in _blob_text_chunks(db, select_value, params, total_bytes):
        if offset + len(text) > start:
            fragments.append(text[max(0, start - offset) : start + limit - offset])
        offset += len(text)
        if offset >= start + limit:
            break
    return "".join(fragments)


def _prepared_pages(
    db: sqlite3.Connection, ref_ids: list[int]
) -> dict[int, list[tuple[int, int, int, bool]]]:
    """Return page locators and sizes, never their full text."""
    if not ref_ids:
        return {}
    placeholders = ",".join("?" for _ in ref_ids)
    rows = db.execute(
        "SELECT ref.id,version.id,length(version.prepared_text),"
        "length(CAST(version.prepared_text AS BLOB)),"
        "instr(version.prepared_text,char(0))>0 "
        "FROM prepared_content_refs AS ref "
        "JOIN prepared_content_set_pages AS member ON member.set_id=ref.set_id "
        "AND (ref.page_number IS NULL OR member.page_number=ref.page_number) "
        "JOIN prepared_page_versions AS version ON version.id=member.version_id "
        f"WHERE ref.id IN ({placeholders}) ORDER BY ref.id,member.page_number",
        ref_ids,
    ).fetchall()
    pages: dict[int, list[tuple[int, int, int, bool]]] = {}
    for ref_id, version_id, characters, byte_size, has_nul in rows:
        if has_nul:
            characters = sum(
                len(chunk)
                for chunk in _blob_text_chunks(
                    db,
                    "SELECT prepared_text AS value FROM prepared_page_versions "
                    "WHERE id=:version_id",
                    {"version_id": version_id},
                    byte_size,
                )
            )
        pages.setdefault(ref_id, []).append(
            (version_id, characters, byte_size, bool(has_nul))
        )
    return pages


def prepared_text_sizes(
    db: sqlite3.Connection, ref_ids: list[int]
) -> dict[int, tuple[int, int]]:
    return {
        ref_id: (
            sum(page[1] for page in pages) + max(0, len(pages) - 1) * 2,
            sum(page[2] for page in pages) + max(0, len(pages) - 1) * 2,
        )
        for ref_id, pages in _prepared_pages(db, ref_ids).items()
    }


def _prepared_window(
    db: sqlite3.Connection, ref_id: int, start: int, limit: int
) -> str:
    """Read intersecting pages and exact two-newline assembly separators."""
    pages = _prepared_pages(db, [ref_id]).get(ref_id, [])
    fragments = []
    offset = 0
    for index, (version_id, characters, byte_size, has_nul) in enumerate(pages):
        if offset >= start + limit:
            break
        if offset + characters > start:
            local_start = max(0, start - offset)
            fragments.append(
                _sql_text_slice(
                    db,
                    "SELECT prepared_text AS value FROM prepared_page_versions "
                    "WHERE id=:version_id",
                    {"version_id": version_id},
                    start=local_start,
                    limit=min(characters, start + limit - offset) - local_start,
                    has_nul=has_nul,
                    total_bytes=byte_size,
                )
            )
        offset += characters
        if index < len(pages) - 1:
            if offset + 2 > start and offset < start + limit:
                fragments.append(
                    "\n\n"[max(0, start - offset) : start + limit - offset]
                )
            offset += 2
    return "".join(fragments)


def _prepared_value(db: sqlite3.Connection, ref_id: int, total_characters: int) -> str:
    return _prepared_window(db, ref_id, 0, total_characters)


def read_current_column(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
    bounded: bool,
) -> dict[int, CurrentCellRead]:
    """Read one column for a row page, omitting only oversized bounded values."""

    output: dict[int, CurrentCellRead] = {}
    for offset in range(0, len(row_ids), _ROW_CHUNK_SIZE):
        chunk = row_ids[offset : offset + _ROW_CHUNK_SIZE]
        if not chunk:
            continue
        sql = (
            "WITH "
            + _authority_cte(chunk)
            + " SELECT row_id,retrieval_revision,origin_kind,origin_op_id,"
            "origin_run_id,validity,stored_kind,"
            "CASE WHEN stored_kind IN ('text','json','bigint','legacy_invalid') "
            "AND length(CAST(stored_value AS BLOB))>:byte_limit "
            "THEN NULL ELSE stored_value END AS bounded_value,"
            "CASE WHEN stored_kind IN ('text','json','bigint','legacy_invalid') "
            "AND length(CAST(stored_value AS BLOB))>:byte_limit "
            "THEN substr(stored_value,1,:preview_limit) END AS preview,"
            "CASE WHEN stored_kind IN ('text','json','bigint','legacy_invalid') "
            "THEN length(stored_value) END AS total_characters,"
            "CASE WHEN stored_kind IN ('text','json','bigint','legacy_invalid') "
            "THEN length(CAST(stored_value AS BLOB)) END AS total_bytes,"
            "instr(stored_value,char(0))>0 AS has_nul "
            "FROM authority ORDER BY row_id"
        )
        query_params: dict[str, int] = {
            **_params(sheet_id, column_id, chunk),
            "byte_limit": MANUAL_EDIT_BYTE_LIMIT if bounded else 2**63 - 1,
            "preview_limit": PREVIEW_CODEPOINT_LIMIT,
        }
        rows = db.execute(sql, query_params).fetchall()
        prepared_ids = [
            int(row["bounded_value"])
            for row in rows
            if row["stored_kind"] == "prepared_content_ref"
            and row["bounded_value"] is not None
        ]
        prepared_meta = prepared_text_sizes(db, prepared_ids)
        for row in rows:
            row_id = int(row["row_id"])
            stored_kind = row["stored_kind"]
            stored_value = row["bounded_value"]
            revision = row["retrieval_revision"]
            ref = {
                "kind": row["origin_kind"] or "missing",
                "op_id": row["origin_op_id"],
                "row_id": row_id,
                "column_id": column_id,
                "run_id": row["origin_run_id"],
            }
            preview: BoundedValuePreview | None = None
            value_present = True
            if stored_kind == "prepared_content_ref" and stored_value is not None:
                ref_id = int(stored_value)
                ref["prepared_ref_id"] = ref_id
                sizes = prepared_meta.get(ref_id)
                if sizes is None:
                    raise CurrentValueUnavailable(
                        f"prepared content reference {ref_id} is unavailable"
                    )
                total_characters, total_bytes = sizes
                if bounded and total_bytes > MANUAL_EDIT_BYTE_LIMIT:
                    preview = BoundedValuePreview(
                        preview=_prepared_window(
                            db, ref_id, 0, PREVIEW_CODEPOINT_LIMIT
                        ),
                        value_kind="text",
                        total_characters=total_characters,
                        total_bytes=total_bytes,
                        retrieval_revision=int(revision),
                    )
                    value = None
                    value_present = False
                else:
                    value = _prepared_value(db, ref_id, total_characters)
            elif (
                bounded
                and stored_kind in _TEXTUAL_KINDS
                and stored_value is None
                and row["preview"] is not None
            ):
                text_preview = str(row["preview"])
                characters = int(row["total_characters"])
                if row["has_nul"]:
                    text_meta = current_text_metadata(
                        db, sheet_id=sheet_id, row_id=row_id, column_id=column_id
                    )
                    characters = text_meta.total_characters
                    text_preview = read_current_text(
                        db,
                        sheet_id=sheet_id,
                        row_id=row_id,
                        column_id=column_id,
                        metadata=text_meta,
                        start=0,
                        limit=PREVIEW_CODEPOINT_LIMIT,
                    )
                preview = BoundedValuePreview(
                    preview=text_preview,
                    value_kind=str(stored_kind),
                    total_characters=characters,
                    total_bytes=int(row["total_bytes"]),
                    retrieval_revision=int(revision),
                )
                value = None
                value_present = False
            else:
                value = decode_stored_value(stored_kind, stored_value)
            output[row_id] = CurrentCellRead(
                value=value,
                value_present=value_present,
                value_ref=ref,
                validity=str(row["validity"]),
                bounded_value=preview,
            )
    return output


def current_text_metadata(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    include_scalars: bool = False,
) -> CurrentTextMetadata:
    sql = (
        "WITH "
        + _authority_cte([row_id])
        + " SELECT stored_kind,CASE WHEN stored_kind='prepared_content_ref' "
        "THEN stored_value END AS prepared_ref_id,retrieval_revision,"
        "length(CAST(stored_value AS TEXT)) AS total_characters,"
        "length(CAST(stored_value AS BLOB)) AS total_bytes,"
        "instr(stored_value,char(0))>0 AS has_nul "
        "FROM authority"
    )
    params = _params(sheet_id, column_id, [row_id])
    row = db.execute(sql, params).fetchone()
    if row is None or row["retrieval_revision"] is None or row["stored_kind"] is None:
        raise CurrentValueUnavailable("cell value is unavailable")
    kind = str(row["stored_kind"])
    revision = int(row["retrieval_revision"])
    if kind == "prepared_content_ref":
        ref_id = int(row["prepared_ref_id"])
        sizes = prepared_text_sizes(db, [ref_id]).get(ref_id)
        if sizes is None:
            raise CurrentValueUnavailable("prepared cell value is unavailable")
        return CurrentTextMetadata("text", ref_id, revision, *sizes)
    if kind not in _TEXTUAL_KINDS and not (include_scalars and kind in _SCALAR_KINDS):
        raise CurrentValueNotTextual("cell value is not textual")
    characters = int(row["total_characters"])
    byte_size = int(row["total_bytes"])
    has_nul = bool(row["has_nul"])
    if has_nul:
        characters = sum(
            len(chunk)
            for chunk in _blob_text_chunks(
                db,
                "WITH "
                + _authority_cte([row_id])
                + " SELECT stored_value AS value FROM authority",
                params,
                byte_size,
            )
        )
    return CurrentTextMetadata(kind, None, revision, characters, byte_size, has_nul)


def read_current_text(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    metadata: CurrentTextMetadata,
    start: int,
    limit: int,
) -> str:
    """Shared bounded SQL read for grid/viewer and scoped Ask source reads.

    Callers own their size limits, cancellation and snapshot/revision fences.
    JSON coordinates address its exact stored encoding, never a decoded object.
    """
    if start < 0 or limit < 0:
        raise ValueError("text range must be non-negative")
    if limit == 0:
        return ""
    if metadata.prepared_ref_id is not None:
        return _prepared_window(db, metadata.prepared_ref_id, start, limit)
    return _sql_text_slice(
        db,
        "WITH "
        + _authority_cte([row_id])
        + " SELECT CAST(stored_value AS TEXT) AS value FROM authority",
        _params(sheet_id, column_id, [row_id]),
        start=start,
        limit=limit,
        has_nul=metadata.has_nul,
        total_bytes=metadata.total_bytes,
    )


def read_current_value_window(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    start: int = 0,
    limit: int = TEXT_WINDOW_CODEPOINT_LIMIT,
    expected_revision: int | None = None,
    max_lines: int | None = TEXT_WINDOW_LINE_LIMIT,
) -> CurrentValueWindow:
    """Read an absolute codepoint window from one current textual value."""

    if start < 0:
        raise ValueError("text window start must be non-negative")
    if not 1 <= limit <= TEXT_WINDOW_CODEPOINT_LIMIT:
        raise ValueError("invalid text window limit")
    if start > 0 and expected_revision is None:
        raise ValueError("revision is required for a continuation window")
    metadata = current_text_metadata(
        db, sheet_id=sheet_id, row_id=row_id, column_id=column_id
    )
    if (
        expected_revision is not None
        and expected_revision != metadata.retrieval_revision
    ):
        raise CurrentValueChanged("cell value changed; reopen it before continuing")
    bounded_start = min(start, metadata.total_characters)
    text = read_current_text(
        db,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        metadata=metadata,
        start=bounded_start,
        limit=limit,
    )
    if max_lines is not None and max_lines > 0:
        position = 0
        for _ in range(max_lines):
            position = text.find("\n", position) + 1
            if position == 0:
                break
        else:
            text = text[:position]
    return CurrentValueWindow(
        row_id=row_id,
        column_id=column_id,
        retrieval_revision=metadata.retrieval_revision,
        value_kind=metadata.value_kind,
        total_characters=metadata.total_characters,
        total_bytes=metadata.total_bytes,
        start=bounded_start,
        text=text,
    )


def iter_current_value(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    expected_revision: int,
) -> Iterator[str]:
    """Stream exact UTF-8 chunks from the caller's revision-fenced snapshot."""

    metadata = current_text_metadata(
        db, sheet_id=sheet_id, row_id=row_id, column_id=column_id
    )
    if metadata.retrieval_revision != expected_revision:
        raise CurrentValueChanged("cell value changed; reopen it before continuing")
    if metadata.prepared_ref_id is None:
        yield from _blob_text_chunks(
            db,
            "WITH "
            + _authority_cte([row_id])
            + " SELECT CAST(stored_value AS TEXT) AS value FROM authority",
            _params(sheet_id, column_id, [row_id]),
            metadata.total_bytes,
        )
        return
    pages = _prepared_pages(db, [metadata.prepared_ref_id]).get(
        metadata.prepared_ref_id, []
    )
    for index, (version_id, _characters, byte_size, _has_nul) in enumerate(pages):
        if index:
            yield "\n\n"
        yield from _blob_text_chunks(
            db,
            "SELECT prepared_text AS value FROM prepared_page_versions WHERE id=:version_id",
            {"version_id": version_id},
            byte_size,
        )


__all__ = [
    "BoundedValuePreview",
    "CurrentCellRead",
    "CurrentValueChanged",
    "CurrentValueNotTextual",
    "CurrentValueUnavailable",
    "CurrentValueWindow",
    "CurrentTextMetadata",
    "current_text_metadata",
    "read_current_text",
    "prepared_text_sizes",
    "MANUAL_EDIT_BYTE_LIMIT",
    "PREVIEW_BYTE_LIMIT",
    "PREVIEW_CODEPOINT_LIMIT",
    "TEXT_WINDOW_CODEPOINT_LIMIT",
    "TEXT_WINDOW_LINE_LIMIT",
    "iter_current_value",
    "read_current_column",
    "read_current_value_window",
]

"""Bounded reads of authoritative current-cell values.

Large text stays in its existing authority table.  This module reads compact
metadata and SQL substrings without handing a whole multi-megabyte value to
Python.  Prepared document text is assembled only for the requested range,
including the canonical two-newline separators between physical pages.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator

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
_HASH_CACHE: OrderedDict[tuple[Any, ...], str] = OrderedDict()
_HASH_CACHE_LOCK = threading.Lock()
_TEXT_META_CACHE: OrderedDict[tuple[Any, ...], CurrentTextMetadata] = OrderedDict()
_PREPARED_META_CACHE: OrderedDict[tuple[Any, ...], list[tuple[int, int, int, bool]]] = (
    OrderedDict()
)
_PREPARED_CACHE_PAGE_LIMIT = 32_768


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
    blob_rowid: int | None = None
    prepared_pages: tuple[tuple[int, int, int, bool], ...] = ()


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
authority AS NOT MATERIALIZED (
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


def decode_text_prefix(value: str | bytes | None) -> str | None:
    """Decode a bounded SQL BLOB prefix without losing NUL or a split UTF-8 tail."""
    if isinstance(value, bytes):
        return codecs.getincrementaldecoder("utf-8")().decode(value, final=False)[
            :PREVIEW_CODEPOINT_LIMIT
        ]
    return value


def _blob_text_chunks(
    db: sqlite3.Connection,
    select_value: str,
    params: dict[str, Any],
    total_bytes: int,
    *,
    blob_location: tuple[str, str, int] | None = None,
) -> Iterator[str]:
    """Decode one UTF-8 stream, preserving NUL and split character boundaries.

    Addressable authority rows use incremental SQLite blob I/O. WITHOUT ROWID
    authorities use one scalar subquery per stream: SQLite retains one value,
    while Python receives only bounded chunks, rather than reloading it per slice.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    if blob_location is not None:
        table, column, rowid = blob_location
        with db.blobopen(table, column, rowid, readonly=True) as blob:
            for offset in range(0, total_bytes, _BLOB_CHUNK_BYTES):
                yield decoder.decode(
                    blob.read(_BLOB_CHUNK_BYTES),
                    final=offset + _BLOB_CHUNK_BYTES >= total_bytes,
                )
        return
    offsets = json.dumps(list(range(0, total_bytes, _BLOB_CHUNK_BYTES)))
    cursor = db.execute(
        "SELECT substr((SELECT CAST(value AS BLOB) FROM ("
        + select_value
        + ")),chunk.value+1,:chunk_size) FROM json_each(:chunk_offsets) AS chunk",
        {**params, "chunk_offsets": offsets, "chunk_size": _BLOB_CHUNK_BYTES},
    )
    try:
        for index, row in enumerate(cursor):
            yield decoder.decode(
                bytes(row[0]), final=(index + 1) * _BLOB_CHUNK_BYTES >= total_bytes
            )
    finally:
        cursor.close()


def _sql_text_slice(
    db: sqlite3.Connection,
    select_value: str,
    params: dict[str, int],
    *,
    start: int,
    limit: int,
    has_nul: bool,
    total_bytes: int,
    blob_location: tuple[str, str, int] | None = None,
) -> str:
    if not has_nul:
        row = db.execute(
            "SELECT substr(value,:text_start,:text_limit) FROM (" + select_value + ")",
            {**params, "text_start": start + 1, "text_limit": limit},
        ).fetchone()
        return str(row[0] or "")
    fragments = []
    offset = 0
    with closing(
        _blob_text_chunks(
            db, select_value, params, total_bytes, blob_location=blob_location
        )
    ) as chunks:
        for text in chunks:
            if offset + len(text) > start:
                fragments.append(text[max(0, start - offset) : start + limit - offset])
            offset += len(text)
            if offset >= start + limit:
                break
    return "".join(fragments)


def sql_text_sizes(
    db: sqlite3.Connection, select_value: str, params: dict[str, Any]
) -> tuple[int, int, bool] | None:
    """Read UTF-8 text sizes without returning its full value to Python."""
    row = db.execute(
        "SELECT length(value),length(CAST(value AS BLOB)),instr(value,char(0))>0 FROM ("
        + select_value
        + ")",
        params,
    ).fetchone()
    if row is None or row[0] is None:
        return None
    chars, byte_size, has_nul = int(row[0]), int(row[1]), bool(row[2])
    if has_nul:
        chars = sum(
            len(part) for part in _blob_text_chunks(db, select_value, params, byte_size)
        )
    return chars, byte_size, has_nul


def read_sql_text(
    db: sqlite3.Connection,
    select_value: str,
    params: dict[str, Any],
    *,
    start: int,
    limit: int,
    sizes: tuple[int, int, bool],
) -> str:
    return _sql_text_slice(
        db,
        select_value,
        params,
        start=start,
        limit=limit,
        total_bytes=sizes[1],
        has_nul=sizes[2],
    )


def iter_sql_text(
    db: sqlite3.Connection, select_value: str, params: dict[str, Any], total_bytes: int
) -> Iterator[str]:
    yield from _blob_text_chunks(db, select_value, params, total_bytes)


def _cache_key(
    db: sqlite3.Connection, identity: tuple[Any, ...]
) -> tuple[Any, ...] | None:
    filename = str(db.execute("PRAGMA database_list").fetchone()[2])
    if not filename:
        return None
    stat = os.stat(filename)
    bundle = db.execute("SELECT value FROM meta WHERE key='storage_id'").fetchone()
    return (
        filename,
        stat.st_dev,
        stat.st_ino,
        str(bundle[0]) if bundle is not None else None,
        *identity,
    )


def cached_text_hash(
    db: sqlite3.Connection, identity: tuple[Any, ...], parts: Iterator[str]
) -> str:
    """Verify legacy evidence digests once per immutable current-head revision.

    The small process cache stores digests only. File identity plus the current
    revision prevents another project or a replacement database reusing a key.
    In-memory databases bypass the cache.
    """
    key = _cache_key(db, identity)
    if key is not None:
        with _HASH_CACHE_LOCK:
            if key in _HASH_CACHE:
                _HASH_CACHE.move_to_end(key)
                return _HASH_CACHE[key]
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
    result = "sha256:" + digest.hexdigest()
    if key is not None:
        with _HASH_CACHE_LOCK:
            _HASH_CACHE[key] = result
            _HASH_CACHE.move_to_end(key)
            while len(_HASH_CACHE) > 128:
                _HASH_CACHE.popitem(last=False)
    return result


def current_text_hash(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    metadata: CurrentTextMetadata | None = None,
    check: Callable[[], None] | None = None,
) -> str:
    metadata = metadata or current_text_metadata(
        db, sheet_id=sheet_id, row_id=row_id, column_id=column_id
    )

    def checked_parts() -> Iterator[str]:
        with closing(
            iter_current_value(
                db,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
                expected_revision=metadata.retrieval_revision,
                metadata=metadata,
            )
        ) as parts:
            for part in parts:
                if check is not None:
                    check()
                yield part

    if check is not None:
        check()
    return cached_text_hash(
        db,
        ("cell", sheet_id, row_id, column_id, metadata.retrieval_revision),
        checked_parts(),
    )


def _prepared_pages(
    db: sqlite3.Connection, ref_ids: list[int]
) -> dict[int, list[tuple[int, int, int, bool]]]:
    """Return page locators and sizes, never their full text."""
    if not ref_ids:
        return {}
    pages: dict[int, list[tuple[int, int, int, bool]]] = {}
    keys = {ref_id: _cache_key(db, ("prepared_ref", ref_id)) for ref_id in ref_ids}
    with _HASH_CACHE_LOCK:
        for ref_id, key in keys.items():
            if key in _PREPARED_META_CACHE:
                pages[ref_id] = _PREPARED_META_CACHE[key]
                _PREPARED_META_CACHE.move_to_end(key)
    ref_ids = [ref_id for ref_id in ref_ids if ref_id not in pages]
    if not ref_ids:
        return pages
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
                    blob_location=(
                        "prepared_page_versions",
                        "prepared_text",
                        version_id,
                    ),
                )
            )
        pages.setdefault(ref_id, []).append(
            (version_id, characters, byte_size, bool(has_nul))
        )
    with _HASH_CACHE_LOCK:
        for ref_id in ref_ids:
            key = keys[ref_id]
            value = pages.get(ref_id, [])
            if key is not None and len(value) <= _PREPARED_CACHE_PAGE_LIMIT:
                _PREPARED_META_CACHE[key] = value
                _PREPARED_META_CACHE.move_to_end(key)
        while (
            len(_PREPARED_META_CACHE) > 128
            or sum(map(len, _PREPARED_META_CACHE.values())) > _PREPARED_CACHE_PAGE_LIMIT
        ):
            _PREPARED_META_CACHE.popitem(last=False)
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


def read_prepared_text(
    db: sqlite3.Connection,
    ref_id: int,
    start: int,
    limit: int,
    *,
    pages: tuple[tuple[int, int, int, bool], ...] | None = None,
) -> str:
    """Read intersecting pages and exact two-newline assembly separators."""
    pages = (
        pages if pages is not None else _prepared_pages(db, [ref_id]).get(ref_id, [])
    )
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
                    blob_location=(
                        "prepared_page_versions",
                        "prepared_text",
                        version_id,
                    ),
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
    return read_prepared_text(db, ref_id, 0, total_characters)


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
                        preview=read_prepared_text(
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
    params = _params(sheet_id, column_id, [row_id])
    row = db.execute(
        "WITH "
        + _authority_cte([row_id])
        + " SELECT stored_kind,CASE WHEN stored_kind='prepared_content_ref' "
        "THEN stored_value END AS prepared_ref_id,retrieval_revision,"
        "CASE WHEN origin_kind='source_cell' THEN (SELECT source.rowid FROM cells AS source "
        "WHERE source.row_id=authority.row_id AND source.column_id=:column_id "
        "AND source.producer_id IS authority.base_producer_id) END AS blob_rowid FROM authority",
        params,
    ).fetchone()
    if row is None or row["retrieval_revision"] is None or row["stored_kind"] is None:
        raise CurrentValueUnavailable("cell value is unavailable")
    kind = str(row["stored_kind"])
    revision = int(row["retrieval_revision"])
    if kind == "prepared_content_ref":
        ref_id = int(row["prepared_ref_id"])
        pages = tuple(_prepared_pages(db, [ref_id]).get(ref_id, []))
        if not pages:
            raise CurrentValueUnavailable("prepared cell value is unavailable")
        separators = max(0, len(pages) - 1) * 2
        return CurrentTextMetadata(
            "text",
            ref_id,
            revision,
            sum(page[1] for page in pages) + separators,
            sum(page[2] for page in pages) + separators,
            prepared_pages=pages,
        )
    if kind not in _TEXTUAL_KINDS and not (include_scalars and kind in _SCALAR_KINDS):
        raise CurrentValueNotTextual("cell value is not textual")
    key = _cache_key(db, ("cell_metadata", sheet_id, row_id, column_id, revision))
    with _HASH_CACHE_LOCK:
        if key in _TEXT_META_CACHE:
            _TEXT_META_CACHE.move_to_end(key)
            # VACUUM may renumber implicit rowids without changing cell content.
            return replace(
                _TEXT_META_CACHE[key],
                blob_rowid=row["blob_rowid"] if kind in _TEXTUAL_KINDS else None,
            )
    sizes = db.execute(
        "WITH "
        + _authority_cte([row_id])
        + " SELECT length(CAST(stored_value AS TEXT)),"
        "length(CAST(stored_value AS BLOB)),instr(stored_value,char(0))>0 FROM authority",
        params,
    ).fetchone()
    characters, byte_size, has_nul = int(sizes[0]), int(sizes[1]), bool(sizes[2])
    blob_rowid = row["blob_rowid"] if kind in _TEXTUAL_KINDS else None
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
                blob_location=("cells", "value", blob_rowid)
                if blob_rowid is not None
                else None,
            )
        )
    metadata = CurrentTextMetadata(
        kind, None, revision, characters, byte_size, has_nul, blob_rowid
    )
    if key is not None:
        with _HASH_CACHE_LOCK:
            _TEXT_META_CACHE[key] = metadata
            _TEXT_META_CACHE.move_to_end(key)
            while len(_TEXT_META_CACHE) > 128:
                _TEXT_META_CACHE.popitem(last=False)
    return metadata


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
        return read_prepared_text(
            db,
            metadata.prepared_ref_id,
            start,
            limit,
            pages=metadata.prepared_pages or None,
        )
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
        blob_location=("cells", "value", metadata.blob_rowid)
        if metadata.blob_rowid is not None
        else None,
    )


def locate_current_text(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    metadata: CurrentTextMetadata,
    literal: str,
    check: Callable[[], None] | None = None,
) -> int:
    """Locate a literal once, with absolute codepoint offsets and bounded memory."""
    if check is not None:
        check()
    if metadata.prepared_ref_id is None and not metadata.has_nul:
        row = db.execute(
            "WITH "
            + _authority_cte([row_id])
            + " SELECT instr(CAST(stored_value AS TEXT),:literal)-1 FROM authority",
            {**_params(sheet_id, column_id, [row_id]), "literal": literal},
        ).fetchone()
        return int(row[0])
    offset = 0
    overlap = ""
    with closing(
        iter_current_value(
            db,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            expected_revision=metadata.retrieval_revision,
            metadata=metadata,
        )
    ) as parts:
        for part in parts:
            if check is not None:
                check()
            text = overlap + part
            found = text.find(literal)
            if found >= 0:
                return offset - len(overlap) + found
            overlap = text[-(len(literal) - 1) :] if len(literal) > 1 else ""
            offset += len(part)
    return -1


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
    text = limit_text_lines(text, max_lines)
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


def limit_text_lines(text: str, max_lines: int | None = TEXT_WINDOW_LINE_LIMIT) -> str:
    if max_lines is not None and max_lines > 0:
        position = 0
        for _ in range(max_lines):
            position = text.find("\n", position) + 1
            if position == 0:
                break
        else:
            text = text[:position]
    return text


def iter_current_value(
    db: sqlite3.Connection,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    expected_revision: int,
    metadata: CurrentTextMetadata | None = None,
) -> Iterator[str]:
    """Stream exact UTF-8 chunks from the caller's revision-fenced snapshot."""

    metadata = metadata or current_text_metadata(
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
            blob_location=("cells", "value", metadata.blob_rowid)
            if metadata.blob_rowid is not None
            else None,
        )
        return
    yield from iter_prepared_text(
        db, metadata.prepared_ref_id, pages=metadata.prepared_pages or None
    )


def iter_prepared_text(
    db: sqlite3.Connection,
    ref_id: int,
    *,
    pages: tuple[tuple[int, int, int, bool], ...] | None = None,
) -> Iterator[str]:
    pages = (
        pages if pages is not None else _prepared_pages(db, [ref_id]).get(ref_id, [])
    )
    for index, (version_id, _characters, byte_size, _has_nul) in enumerate(pages):
        if index:
            yield "\n\n"
        yield from _blob_text_chunks(
            db,
            "SELECT prepared_text AS value FROM prepared_page_versions WHERE id=:version_id",
            {"version_id": version_id},
            byte_size,
            blob_location=("prepared_page_versions", "prepared_text", version_id),
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
    "current_text_hash",
    "decode_text_prefix",
    "cached_text_hash",
    "limit_text_lines",
    "sql_text_sizes",
    "read_sql_text",
    "iter_sql_text",
    "read_prepared_text",
    "iter_prepared_text",
    "read_current_text",
    "locate_current_text",
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

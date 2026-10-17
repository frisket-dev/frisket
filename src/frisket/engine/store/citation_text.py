"""Stored-once text contexts for source-artifact citations.

Native cell text stays in the authoritative value tables. Artifacts retain
only its expected hash and cell locator; a viewer displays it while that cell
still contains the same passage. Legacy and synthesized contexts have no
native cell, so their text is retained once by content hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from .current_value_windows import (
    CurrentValueChanged,
    CurrentValueNotTextual,
    CurrentValueUnavailable,
    MANUAL_EDIT_BYTE_LIMIT,
    TEXT_WINDOW_CODEPOINT_LIMIT,
    cached_text_hash,
    current_text_hash,
    current_text_metadata,
    iter_prepared_text,
    iter_sql_text,
    prepared_text_sizes,
    read_current_text,
    read_prepared_text,
    read_sql_text,
    sql_text_sizes,
    limit_text_lines,
)

from .value_codec import repair_unicode_text


CAPTURED_TEXT_HASH_KEY = "captured_text_hash"
CAPTURED_TEXT_FROZEN_KEY = "captured_text_frozen"
_CONTENT_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
_READ_BATCH_SIZE = 500


CITATION_TEXT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS citation_texts (
      content_hash TEXT PRIMARY KEY,
      text TEXT NOT NULL,
      CHECK (
        length(content_hash) = 71
        AND substr(content_hash, 1, 7) = 'sha256:'
        AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'
      )
    ) WITHOUT ROWID
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_citation_texts_no_update
    BEFORE UPDATE ON citation_texts
    BEGIN
      SELECT RAISE(ABORT, 'citation text snapshots are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_citation_texts_no_delete
    BEFORE DELETE ON citation_texts
    BEGIN
      SELECT RAISE(ABORT, 'citation text snapshots are retained');
    END
    """,
)
CITATION_TEXT_SCHEMA_SQL = (
    ";\n".join(
        statement.strip().rstrip(";") for statement in CITATION_TEXT_SCHEMA_STATEMENTS
    )
    + ";\n"
)


def install_citation_text_schema(db: sqlite3.Connection) -> None:
    """Install citation snapshot storage inside the caller's transaction."""

    for statement in CITATION_TEXT_SCHEMA_STATEMENTS:
        db.execute(statement)


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _store_snapshot(db: sqlite3.Connection, *, content_hash: str, text: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO citation_texts(content_hash,text) VALUES (?,?)",
        (content_hash, text),
    )
    existing = db.execute(
        "SELECT text FROM citation_texts WHERE content_hash=?", (content_hash,)
    ).fetchone()
    if existing is None or existing[0] != text:
        raise sqlite3.IntegrityError("citation text hash collision")


def store_captured_text(
    db: sqlite3.Connection,
    *,
    metadata: dict[str, Any],
    text: str,
    native: bool,
) -> dict[str, Any]:
    """Replace one inline captured string with its minimal storage metadata."""

    if not isinstance(text, str):
        raise TypeError("captured citation text must be a string")
    text = repair_unicode_text(text)
    stored = dict(metadata)
    stored.pop("captured_text", None)
    content_hash = _text_hash(text)
    stored[CAPTURED_TEXT_HASH_KEY] = content_hash
    if native:
        stored.pop(CAPTURED_TEXT_FROZEN_KEY, None)
    else:
        _store_snapshot(db, content_hash=content_hash, text=text)
        stored[CAPTURED_TEXT_FROZEN_KEY] = True
    return stored


def read_text_snapshot(db: sqlite3.Connection, content_hash: str) -> str | None:
    """Read one immutable saved context by hash."""

    if not _CONTENT_HASH_RE.fullmatch(content_hash):
        return None
    row = db.execute(
        "SELECT text FROM citation_texts WHERE content_hash=?", (content_hash,)
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    return row[0] if _text_hash(row[0]) == content_hash else None


def _capture_spec(artifact: dict[str, Any]) -> tuple[str, bool] | None:
    metadata = artifact.get("metadata")
    if not isinstance(metadata, dict):
        return None
    content_hash = metadata.get(CAPTURED_TEXT_HASH_KEY)
    frozen = metadata.get(CAPTURED_TEXT_FROZEN_KEY, False)
    if not isinstance(content_hash, str) or not _CONTENT_HASH_RE.fullmatch(
        content_hash
    ):
        return None
    if not isinstance(frozen, bool):
        return None
    return content_hash, frozen


def passage_window(
    start: int,
    text: str,
    *,
    total_characters: int,
    total_bytes: int,
    revision: int | None,
    limit: int,
) -> dict[str, Any]:
    end = start + len(text)
    return {
        "start": start,
        "end": end,
        "total_characters": total_characters,
        "total_bytes": total_bytes,
        "retrieval_revision": revision,
        "previous_start": max(0, start - limit) if start else None,
        "next_start": end if end < total_characters else None,
        "reached_start": start == 0,
        "reached_end": end >= total_characters,
    }


def read_artifact_passage(
    db: sqlite3.Connection,
    artifact: dict[str, Any],
    *,
    start: int | None = None,
    limit: int | None = None,
    revision: int | None = None,
    span_id: str | None = None,
    ignore_hash: bool = False,
) -> tuple[dict[str, Any] | None, bool]:
    """Read a verified native/prepared/frozen passage, retaining hash authority.

    Existing hash-only captures are verified by a bounded streaming digest.
    A small digest cache is keyed by database identity and immutable source
    identity; native sources additionally include the current head revision.
    """
    if start is not None and start < 0:
        raise ValueError("text passage start must be non-negative")
    if limit is not None and not 1 <= limit <= TEXT_WINDOW_CODEPOINT_LIMIT:
        raise ValueError("invalid text passage limit")
    spec = _capture_spec(artifact)
    metadata = artifact.get("metadata") or {}
    prepared = metadata.get("prepared_source") or {}
    ref_id = prepared.get("prepared_ref_id")
    current_revision = None
    if spec is not None and spec[1]:
        sql = (
            "SELECT text AS value FROM citation_texts WHERE content_hash=:content_hash"
        )
        params = {"content_hash": spec[0]}
        sizes = sql_text_sizes(db, sql, params)
        if sizes is None:
            return None, True
        characters, byte_size, _has_nul = sizes
        digest = cached_text_hash(
            db,
            ("citation_snapshot", spec[0]),
            iter_sql_text(db, sql, params, byte_size),
        )

        def read(offset, size):
            return read_sql_text(db, sql, params, start=offset, limit=size, sizes=sizes)
    elif spec is not None:
        cell = artifact.get("source_cell") or {}
        if any(
            type(cell.get(key)) is not int
            for key in ("sheet_id", "row_id", "column_id")
        ):
            return None, True
        coordinate = {key: cell[key] for key in ("sheet_id", "row_id", "column_id")}
        try:
            current = current_text_metadata(db, **coordinate)
        except (CurrentValueUnavailable, CurrentValueNotTextual):
            return None, True
        if current.value_kind != "text":
            return None, True
        current_revision = current.retrieval_revision
        if revision is not None and revision != current_revision:
            raise CurrentValueChanged(
                "source_changed: reopen this evidence before continuing"
            )
        if start is not None and start > 0 and revision is None:
            raise ValueError("revision is required for a continuation passage")
        characters, byte_size = current.total_characters, current.total_bytes
        digest = current_text_hash(db, **coordinate, metadata=current)

        def read(offset, size):
            return read_current_text(
                db, **coordinate, metadata=current, start=offset, limit=size
            )
    elif type(ref_id) is int and ref_id > 0:
        sizes = prepared_text_sizes(db, [ref_id]).get(ref_id)
        if sizes is None:
            return None, True
        characters, byte_size = sizes
        digest = cached_text_hash(
            db, ("prepared_ref", ref_id), iter_prepared_text(db, ref_id)
        )

        def read(offset, size):
            return read_prepared_text(db, ref_id, offset, size)
    else:
        return None, False
    if not ignore_hash and spec is not None and digest != spec[0]:
        return None, True
    bounded = (
        byte_size > MANUAL_EDIT_BYTE_LIMIT or start is not None or limit is not None
    )
    size = limit or (TEXT_WINDOW_CODEPOINT_LIMIT if bounded else max(1, characters))
    offset = start or 0
    if bounded and start is None:
        candidates = [
            span
            for span in artifact.get("spans", [])
            if span_id is None or span.get("stable_id") == span_id
        ]
        for span in candidates:
            anchor = (span.get("selector") or {}).get("char_start")
            if type(anchor) is int and 0 <= anchor < characters:
                offset = max(0, anchor - min(500, size // 2))
                break
    offset = min(offset, characters)
    text = read(offset, size)
    if bounded:
        text = limit_text_lines(text)
    return {
        "text": text,
        "content_hash": digest,
        "window": passage_window(
            offset,
            text,
            total_characters=characters,
            total_bytes=byte_size,
            revision=current_revision,
            limit=size,
        )
        if bounded
        else None,
    }, False


def resolve_current_native_texts(
    db: sqlite3.Connection, artifacts: list[dict[str, Any]]
) -> dict[int, str]:
    """Read current text for native citation cells in bounded batches.

    This primitive deliberately does not compare the saved hash. The normal
    viewer performs that check; explicit recovery can inspect changed text
    without mutating the artifact or its saved spans.
    """

    artifact_ids_by_coordinate: dict[tuple[int, int, int], list[int]] = {}
    for artifact in artifacts:
        spec = _capture_spec(artifact)
        if spec is None or spec[1]:
            continue
        source_cell = artifact.get("source_cell")
        if not isinstance(source_cell, dict) or not all(
            isinstance(source_cell.get(key), int)
            and not isinstance(source_cell.get(key), bool)
            for key in ("sheet_id", "row_id", "column_id")
        ):
            continue
        coordinate = (
            int(source_cell["sheet_id"]),
            int(source_cell["row_id"]),
            int(source_cell["column_id"]),
        )
        artifact_ids_by_coordinate.setdefault(coordinate, []).append(
            int(artifact["id"])
        )

    available: dict[int, str] = {}
    coordinates = list(artifact_ids_by_coordinate)
    for offset in range(0, len(coordinates), _READ_BATCH_SIZE):
        batch = coordinates[offset : offset + _READ_BATCH_SIZE]
        encoded = json.dumps(batch, separators=(",", ":"))
        rows = db.execute(
            "WITH requested(sheet_id,row_id,column_id) AS ("
            "SELECT CAST(json_extract(item.value,'$[0]') AS INTEGER),"
            "CAST(json_extract(item.value,'$[1]') AS INTEGER),"
            "CAST(json_extract(item.value,'$[2]') AS INTEGER) "
            "FROM json_each(?) item"
            ") SELECT requested.sheet_id,requested.row_id,"
            "requested.column_id,current.value_kind,current.value "
            "FROM requested "
            "LEFT JOIN rows source_row ON source_row.id=requested.row_id "
            "AND source_row.sheet_id=requested.sheet_id "
            "LEFT JOIN current_cell_values current "
            "ON current.row_id=source_row.id "
            "AND current.column_id=requested.column_id",
            (encoded,),
        ).fetchall()
        for row in rows:
            text = row[4]
            if row[3] != "text" or not isinstance(text, str):
                continue
            coordinate = (int(row[0]), int(row[1]), int(row[2]))
            for artifact_id in artifact_ids_by_coordinate[coordinate]:
                available[artifact_id] = text
    return available


def resolve_artifact_texts(
    db: sqlite3.Connection, artifacts: list[dict[str, Any]]
) -> tuple[dict[int, str], set[int]]:
    """Resolve a viewer panel's contexts in bounded batches.

    Returns ``(available_text_by_artifact_id, stale_artifact_ids)``. Native
    text is read only from the current cell projection and accepted only when
    its hash still matches. This read never writes or attempts re-anchoring.
    """

    frozen: list[tuple[int, str]] = []
    native_hashes: dict[int, str] = {}
    for artifact in artifacts:
        spec = _capture_spec(artifact)
        if spec is None:
            continue
        artifact_id = int(artifact["id"])
        content_hash, is_frozen = spec
        if is_frozen:
            frozen.append((artifact_id, content_hash))
            continue
        native_hashes[artifact_id] = content_hash

    available: dict[int, str] = {}
    stale: set[int] = set()
    for offset in range(0, len(frozen), _READ_BATCH_SIZE):
        batch = frozen[offset : offset + _READ_BATCH_SIZE]
        hashes = list(
            dict.fromkeys(content_hash for _artifact_id, content_hash in batch)
        )
        placeholders = ",".join("?" for _ in hashes)
        rows = db.execute(
            "SELECT content_hash,text FROM citation_texts "
            f"WHERE content_hash IN ({placeholders})",
            hashes,
        ).fetchall()
        by_hash = {
            str(row[0]): row[1]
            for row in rows
            if isinstance(row[1], str) and _text_hash(row[1]) == row[0]
        }
        for artifact_id, content_hash in batch:
            text = by_hash.get(content_hash)
            if isinstance(text, str):
                available[artifact_id] = text
            else:
                stale.add(artifact_id)

    prepared_artifacts: dict[int, list[int]] = {}
    for artifact in artifacts:
        metadata = artifact.get("metadata") or {}
        prepared = metadata.get("prepared_source") or {}
        ref_id = prepared.get("prepared_ref_id")
        if type(ref_id) is int and ref_id > 0:
            prepared_artifacts.setdefault(ref_id, []).append(int(artifact["id"]))
    refs = list(prepared_artifacts)
    for offset in range(0, len(refs), _READ_BATCH_SIZE):
        batch = refs[offset : offset + _READ_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        texts = dict(
            db.execute(
                "SELECT ref_id,value FROM prepared_content_ref_values "
                f"WHERE ref_id IN ({placeholders})",
                batch,
            )
        )
        for ref_id in batch:
            for artifact_id in prepared_artifacts[ref_id]:
                text = texts.get(ref_id)
                if isinstance(text, str):
                    available[artifact_id] = text
                else:
                    stale.add(artifact_id)

    current_native = resolve_current_native_texts(db, artifacts)
    for artifact_id, content_hash in native_hashes.items():
        text = current_native.get(artifact_id)
        if isinstance(text, str) and _text_hash(text) == content_hash:
            available[artifact_id] = text
        else:
            stale.add(artifact_id)
    return available, stale


def migrate_legacy_citation_texts(db: sqlite3.Connection) -> None:
    """Deduplicate legacy inline contexts without discarding saved history."""

    after_id = 0
    while True:
        rows = db.execute(
            "SELECT id,metadata FROM source_artifacts WHERE id>? ORDER BY id LIMIT 500",
            (after_id,),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            after_id = int(row[0])
            try:
                metadata = json.loads(row[1])
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("captured_text"), str
            ):
                continue
            stored = store_captured_text(
                db,
                metadata=metadata,
                text=metadata["captured_text"],
                native=False,
            )
            db.execute(
                "UPDATE source_artifacts SET metadata=? WHERE id=?",
                (
                    json.dumps(
                        stored,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    int(row[0]),
                ),
            )

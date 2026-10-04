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

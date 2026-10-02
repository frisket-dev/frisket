"""Blob metadata store for a project bundle: canonical blob rows (bytes live
in the pluggable blob backend), blob-to-blob derivation lineage, and the
reference-scan GC that prunes metadata for blobs no live value mentions.
Free functions over the facade's per-thread SQLite connection; ``project``
stays duck-typed (``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .blob_backend import BlobIntegrityError, BlobNotFoundError, sha256_blob_path
from .runs import FAILURE_OUTCOMES, outcome_sql_list


def publish_prepared_blob(
    project: Any,
    *,
    digest: str,
    size: int,
    filename: str | None = None,
    mime: str | None = None,
    source_url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Publish metadata for bytes already verified by the project blob backend."""

    if not project.db.in_transaction:
        raise ValueError("prepared blob publication requires a transaction")
    project.db.execute(
        "INSERT OR IGNORE INTO blobs "
        "(hash, filename, mime, size, source_url, metadata) VALUES (?,?,?,?,?,?)",
        (digest, filename, mime, int(size), source_url, "{}"),
    )
    if metadata:
        from frisket.engine.store.media_blobs import MediaBlobStore

        MediaBlobStore(project).merge_metadata(digest, metadata)


def add_blob(
    project: Any,
    data: bytes,
    filename: str | None = None,
    mime: str | None = None,
    source_url: str | None = None,
    metadata: dict[str, Any] | None = None,
    *,
    commit: bool = True,
) -> str:
    expected_digest = hashlib.sha256(data).hexdigest()
    digest = project.blob_store.put(data)
    if digest != expected_digest:
        raise BlobIntegrityError(
            "blob store returned a digest that does not match the submitted bytes"
        )
    metadata_json = json.dumps(metadata or {})
    project.db.execute(
        "INSERT OR IGNORE INTO blobs (hash, filename, mime, size, source_url, metadata) "
        "VALUES (?,?,?,?,?,?)",
        (digest, filename, mime, len(data), source_url, metadata_json),
    )
    if metadata:
        from frisket.engine.store.media_blobs import MediaBlobStore

        MediaBlobStore(project).merge_metadata(digest, metadata)
    if commit:
        project.db.commit()
    return digest


def add_blob_from_path(
    project: Any,
    path: str | Path,
    filename: str | None = None,
    mime: str | None = None,
    source_url: str | None = None,
    metadata: dict[str, Any] | None = None,
    *,
    commit: bool = True,
    expected_digest: str | None = None,
) -> str:
    """Add a canonical blob from a file without reading it wholly into RAM.

    The project computes the expected digest and byte count first, then the
    backend independently verifies the bytes it stores.  As with ``add_blob``,
    canonical bytes are durable before their SQLite reference is inserted.
    ``commit=False`` leaves both that reference and metadata in the caller's
    active transaction.
    """

    source_path = Path(path)
    observed_digest, size = sha256_blob_path(source_path)
    if expected_digest is not None and observed_digest != expected_digest:
        raise BlobIntegrityError("blob source changed after staging")
    expected_digest = expected_digest or observed_digest
    digest = project.blob_store.put_path(
        source_path,
        expected_digest=expected_digest,
    )
    if digest != expected_digest:
        raise BlobIntegrityError(
            "blob store returned a digest that does not match the submitted file"
        )
    metadata_json = json.dumps(metadata or {})
    project.db.execute(
        "INSERT OR IGNORE INTO blobs (hash, filename, mime, size, source_url, metadata) "
        "VALUES (?,?,?,?,?,?)",
        (digest, filename, mime, size, source_url, metadata_json),
    )
    if metadata:
        from frisket.engine.store.media_blobs import MediaBlobStore

        MediaBlobStore(project).merge_metadata(digest, metadata)
    if commit:
        project.db.commit()
    return digest


def record_blob_derivation(
    project: Any,
    *,
    derived_hash: str,
    source_hash: str,
    op: str,
    params: dict[str, Any] | None = None,
    commit: bool = True,
) -> int:
    """Record a blob-to-blob derivation edge
    (provenance-derived-audio-lineage-v1): ``derived_hash`` was produced
    FROM ``source_hash`` by ``op`` (e.g. "ffmpeg_clip") with ``params``
    (e.g. cut start/end ms). Fails CLOSED -- raises ``BlobNotFoundError``
    -- when either blob is not in this project's store rather than
    silently recording a dangling edge. This records one hop; a derived
    blob can itself later be the SOURCE of a further derivation
    (clip-of-a-clip), so the read side (``blob_derivation_chain``) walks
    hop by hop rather than this table modeling a tree directly."""
    if (
        project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (source_hash,)
        ).fetchone()
        is None
    ):
        raise BlobNotFoundError(
            f"cannot record derivation: source blob {source_hash!r} is "
            "not in this project's blob store"
        )
    if (
        project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (derived_hash,)
        ).fetchone()
        is None
    ):
        raise BlobNotFoundError(
            f"cannot record derivation: derived blob {derived_hash!r} is "
            "not in this project's blob store (add it via add_blob first)"
        )
    cur = project.db.execute(
        "INSERT INTO blob_derivations (derived_hash, source_hash, op, params_json) "
        "VALUES (?,?,?,?)",
        (derived_hash, source_hash, op, json.dumps(params or {})),
    )
    if commit:
        project.db.commit()
    return int(cur.lastrowid)


def blob_derivation(project: Any, derived_hash: str) -> sqlite3.Row | None:
    """The immediate (one-hop) derivation edge for ``derived_hash``, or
    None if it has no recorded derivation (an originally-uploaded blob,
    not a derived one)."""
    return project.db.execute(
        "SELECT * FROM blob_derivations WHERE derived_hash=? ORDER BY id DESC LIMIT 1",
        (derived_hash,),
    ).fetchone()


def blob_derivation_chain(project: Any, digest: str) -> list[dict[str, Any]]:
    """Walk derivation edges from ``digest`` back to its ultimate
    source, hop by hop -- a clip-of-a-clip chains through more than one
    edge, so provenance can trace derived audio all the way back to the
    original recording. Returns ``[]`` when ``digest`` has no recorded
    derivation. Guards against a cyclical chain (a data-integrity bug,
    never a real ffmpeg lineage) by stopping once a hash repeats rather
    than looping forever."""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = digest
    while current not in seen:
        seen.add(current)
        row = project.blob_derivation(current)
        if row is None:
            break
        try:
            params = json.loads(row["params_json"] or "{}")
        except (TypeError, ValueError):
            params = {}
        if not isinstance(params, dict):
            params = {}
        chain.append(
            {
                "derived_hash": row["derived_hash"],
                "source_hash": row["source_hash"],
                "op": row["op"],
                "params": params,
                "created_at": row["created_at"],
            }
        )
        current = row["source_hash"]
    return chain


_HASH_CANDIDATE = re.compile(r"(?=([0-9a-f]{64}))")
_ROOT_BATCH_SIZE = 512
_logger = logging.getLogger(__name__)


def _mark_hashes(db: sqlite3.Connection, hashes) -> None:
    """Indexed membership, bounded Python memory, no corpus-sized known set."""
    batch: set[str] = set()
    for digest in hashes:
        batch.add(digest)
        if len(batch) >= _ROOT_BATCH_SIZE:
            _insert_live_hashes(db, batch)
            batch.clear()
    _insert_live_hashes(db, batch)


def _insert_live_hashes(db: sqlite3.Connection, hashes) -> None:
    db.executemany(
        "INSERT OR IGNORE INTO temp.gc_live_blob_hashes(hash) "
        "SELECT hash FROM blobs WHERE hash=?",
        ((digest,) for digest in hashes),
    )


def _root_texts(db: sqlite3.Connection):
    # Preserve field-name-agnostic retention, including hashes embedded inside
    # longer strings. Overlapping matches preserve the old substring semantics.
    queries = (
        "SELECT value FROM cells WHERE value IS NOT NULL",
        (
            "SELECT res.value FROM results res "
            "JOIN runs ru ON ru.id = res.run_id "
            "JOIN ops o ON o.id = ru.op_id "
            "WHERE res.value IS NOT NULL "
            f"AND res.outcome NOT IN ({outcome_sql_list(FAILURE_OUTCOMES)}) "
            "AND (o.status != 'discarded' "
            "     OR res.run_id IN ("
            "       SELECT generation.expected_base_run_id "
            "       FROM run_output_generations generation "
            "       JOIN runs owner ON owner.id=generation.run_id "
            "       JOIN ops owner_op ON owner_op.id=owner.op_id "
            "       WHERE generation.expected_base_run_id IS NOT NULL "
            "       AND owner_op.status!='discarded'"
            "     ) "
            "     OR (NOT EXISTS ("
            "       SELECT 1 FROM run_output_generations generation "
            "       WHERE generation.run_id=res.run_id "
            "       AND generation.column_id=res.column_id"
            "     ) AND res.run_id IN ("
            "       SELECT current_run_id FROM columns "
            "       WHERE current_run_id IS NOT NULL"
            "     )))"
        ),
        (
            "SELECT e.value FROM edits e JOIN ops o ON o.id = e.op_id "
            "WHERE o.status!='discarded' AND e.value IS NOT NULL"
        ),
        (
            "SELECT external_ref_json FROM source_artifacts "
            "UNION ALL SELECT metadata FROM source_artifacts "
            "UNION ALL SELECT selector_json FROM source_spans "
            "UNION ALL SELECT preview_json FROM source_spans "
            "UNION ALL SELECT metadata FROM source_spans"
        ),
    )
    for query in queries:
        for row in db.execute(query):
            if row[0]:
                yield row[0]


def _retained_payloads(db: sqlite3.Connection):
    """All retained receipt/effect references, not only their current UI shape."""
    for row in db.execute(
        "SELECT body, 'receipt' AS family, status AS state FROM receipts "
        "UNION ALL SELECT payload,family,state FROM effect_checkpoints "
        "WHERE payload IS NOT NULL"
    ):
        payload = json.loads(row[0])
        if row[1] == "receipt" and not isinstance(payload, dict):
            raise ValueError("invalid retained receipt")
        if row[1] == "row_effect" and row[2] == "returned":
            # Preserve the existing fail-closed behavior for damaged unresolved
            # file returns whose ownership cannot be reconstructed safely.
            for response in payload.values():
                for occurrence in response.get("row_files", []):
                    for blob in (occurrence["primary"], *occurrence["supplemental"]):
                        if not isinstance(blob["blob_hash"], str):
                            raise ValueError("invalid returned file")
        yield row[0]


def _import_hashes(project: Any):
    """Read admitted/unresolved inventories without opening a writable store."""
    from .import_intake import read_import_header, validate_import_ref
    from .blob_backend import validate_blob_digest

    root = project.path / ".imports"
    terminal = {"completed", "kept", "removed"}
    # A missing inventory for an unresolved session is unknown ownership.
    for row in project.db.execute(
        "SELECT id FROM import_sessions WHERE state IN ('active','paused','cancelled')"
    ):
        ref = validate_import_ref(row[0])
        if not (root / ref / "inventory.db").is_file():
            raise ValueError("unresolved import inventory is missing")
    if not root.exists():
        return
    if root.is_symlink():
        raise ValueError("import root must not be a symlink")
    with os.scandir(root) as directories:
        for entry in directories:
            if not entry.name.startswith("import-"):
                continue
            validate_import_ref(entry.name)
            state = project.db.execute(
                "SELECT state FROM import_sessions WHERE id=?", (entry.name,)
            ).fetchone()
            if state is not None and state[0] in terminal:
                continue
            if entry.is_symlink():
                raise ValueError("import inventory must not be a symlink")
            directory = Path(entry.path)
            header = read_import_header(directory)
            if header.storage_identity != project.storage_identity:
                raise ValueError("import inventory belongs to another bundle")
            if state is None and header.resolution in {"kept", "removed"}:
                continue
            inventory_path = directory / "inventory.db"
            if inventory_path.is_symlink():
                raise ValueError("import inventory must not be a symlink")
            inventory = sqlite3.connect(
                inventory_path.resolve().as_uri() + "?mode=ro", uri=True
            )
            try:
                for row in inventory.execute("SELECT sha256 FROM inventory_items"):
                    yield validate_blob_digest(row[0])
            finally:
                inventory.close()


def _populate_live_blob_hashes(project: Any) -> None:
    db = project.db
    if db.execute("SELECT 1 FROM blobs LIMIT 1").fetchone() is None:
        return
    # Corrupt recoverable state cannot authorize metadata reclamation.
    try:
        _mark_hashes(
            db,
            (
                match[1]
                for text in _retained_payloads(db)
                for match in _HASH_CANDIDATE.finditer(text)
            ),
        )
        _mark_hashes(db, _import_hashes(project))
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OSError,
        sqlite3.DatabaseError,
    ):
        _logger.warning(
            "Retaining all blob metadata because recoverable ownership could not be read"
        )
        db.execute(
            "INSERT OR IGNORE INTO temp.gc_live_blob_hashes SELECT hash FROM blobs"
        )
        return
    _mark_hashes(
        db,
        (
            match[1]
            for text in _root_texts(db)
            for match in _HASH_CANDIDATE.finditer(text)
        ),
    )
    db.execute(
        "INSERT OR IGNORE INTO temp.gc_live_blob_hashes SELECT b.hash FROM source_artifacts a JOIN blobs b ON b.hash=a.blob_hash"
    )
    db.execute("""WITH RECURSIVE reachable(hash) AS (
        SELECT hash FROM temp.gc_live_blob_hashes
        UNION SELECT d.source_hash FROM blob_derivations d JOIN reachable r ON d.derived_hash=r.hash
    ) INSERT OR IGNORE INTO temp.gc_live_blob_hashes
      SELECT r.hash FROM reachable r JOIN blobs b ON b.hash=r.hash""")


@contextmanager
def _live_blob_hashes(project: Any, *, writing: bool = False):
    db = project.db
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
    try:
        db.execute(
            "CREATE TEMP TABLE gc_live_blob_hashes(hash TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        try:
            _populate_live_blob_hashes(project)
            yield db
        finally:
            db.execute("DROP TABLE temp.gc_live_blob_hashes")
        if owns_transaction:
            db.commit()
    except BaseException:
        if owns_transaction:
            db.rollback()
        raise


def _referenced_blob_hashes(project: Any) -> set[str]:
    """Compatibility inspection helper; production GC never builds this set."""
    with _live_blob_hashes(project) as db:
        return {
            row[0] for row in db.execute("SELECT hash FROM temp.gc_live_blob_hashes")
        }


def gc_blobs(project: Any, dry_run: bool = False) -> dict[str, Any]:
    """Prune metadata for blobs no longer reachable from live data.

    A blob is collectable when no live value, evidence artifact, or retained
    project-file export or published row-file occurrence references it.
    Canonical bytes remain retained
    until the later
    reference-safe, restore-window-aware object GC protocol; this operation
    therefore reports zero bytes freed. With ``dry_run=True`` even the
    SQLite metadata remains unchanged.
    """
    with _live_blob_hashes(project, writing=not dry_run) as db:
        # This list is the existing public result, not a working corpus copy.
        removed = [
            row[0]
            for row in db.execute(
                "SELECT hash FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM temp.gc_live_blob_hashes live WHERE live.hash=b.hash)"
            )
        ]
        if not dry_run and removed:
            db.execute(
                "DELETE FROM blob_derivations WHERE NOT EXISTS (SELECT 1 FROM temp.gc_live_blob_hashes live WHERE live.hash=derived_hash)"
            )
            db.execute(
                "DELETE FROM blobs WHERE NOT EXISTS (SELECT 1 FROM temp.gc_live_blob_hashes live WHERE live.hash=blobs.hash)"
            )
    return {
        "blobs_removed": len(removed),
        "bytes_freed": 0,
        "hashes": removed,
        "dry_run": dry_run,
    }

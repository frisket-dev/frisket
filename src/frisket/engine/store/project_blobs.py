"""Blob metadata store for a project bundle: canonical blob rows (bytes live
in the pluggable blob backend), blob-to-blob derivation lineage, and the
reference-scan GC that prunes metadata for blobs no live value mentions.
Free functions over the facade's per-thread SQLite connection; ``project``
stays duck-typed (``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .blob_backend import BlobIntegrityError, BlobNotFoundError, sha256_blob_path
from .runs import FAILURE_OUTCOMES, outcome_sql_list


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


def _referenced_blob_hashes(project: Any) -> set[str]:
    """Every blob hash still reachable from live data.

    Blob references are embedded in JSON values (cells, run results, manual
    edits) as ``{"blob": <hash>}`` or sibling fields like ``document_blob``.
    Rather than guess the field name we scan each value's text for any known
    blob hash — a 64-char sha256 only appears if the value mentions it, so
    this is exact and field-name agnostic. Results behind a column's CURRENT
    run, behind an UNDONE (still-redoable) run, and source cells / applied
    edits are all live. Only DISCARDED runs (unreachable by undo/redo) are
    collectable — undoing must never strand a blob a redo needs. Published
    project-file exports and published row-file occurrences remain live while
    their owning receipts are retained.
    """
    known = {r["hash"] for r in project.db.execute("SELECT hash FROM blobs")}
    if not known:
        return set()
    referenced: set[str] = set()
    # Returned row effects own recoverable files before result publication.
    # The descriptor is host-written checkpoint data, not arbitrary output JSON.
    for checkpoint in project.db.execute(
        "SELECT payload FROM effect_checkpoints WHERE family='row_effect' AND state='returned'"
    ):
        try:
            response = json.loads(checkpoint["payload"])
            for payload in response.values():
                for occurrence in payload.get("row_files", []):
                    for blob in (occurrence["primary"], *occurrence["supplemental"]):
                        if blob["blob_hash"] in known:
                            referenced.add(blob["blob_hash"])
        except (TypeError, ValueError, KeyError, AttributeError):
            # Corrupt unresolved effects cannot authorize deletion of bytes that
            # may be needed for explicit reconciliation.
            referenced.update(known)
    # Published row files transfer ownership from the consumed checkpoint to
    # their retained host evidence, including supplemental bytes absent from cells.
    # Published exports belong to their receipt, not their source sheet or the
    # eventual handler outcome: a later callable failure cannot undo a delivery.
    for receipt in project.db.execute("SELECT body, status FROM receipts"):
        body = json.loads(receipt["body"])
        for evidence in body.get("evidence", []):
            ref = evidence.get("ref", {})
            if ref.get("kind") != "row_file_output":
                continue
            for blob in (ref["primary"], *ref["supplemental"]):
                if blob["blob_hash"] in known:
                    referenced.add(blob["blob_hash"])
        exports = body.get("exports", [])
        if not isinstance(exports, list):
            continue
        for artifact in exports:
            if not isinstance(artifact, dict):
                continue
            digest = artifact.get("blob_hash")
            if (
                artifact.get("kind") == "export_project_file"
                and isinstance(digest, str)
                and digest in known
            ):
                referenced.add(digest)
    # cells (source data) are always live
    texts = [
        r[0]
        for r in project.db.execute("SELECT value FROM cells WHERE value IS NOT NULL")
    ]
    # Managed results are rooted by their non-discarded journal operation;
    # their rebuildable head projection is never reachability authority.
    # Legacy results retain the scalar-pointer fallback during cutover.
    texts += [
        r[0]
        for r in project.db.execute(
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
        )
    ]
    # manual edits from applied ops
    texts += [
        r[0]
        for r in project.db.execute(
            "SELECT e.value FROM edits e JOIN ops o ON o.id = e.op_id "
            "WHERE o.status='applied' AND e.value IS NOT NULL"
        )
    ]
    # Evidence artifacts are durable citation roots. Their primary blob_hash
    # and derivative refs in artifact/span JSON must survive compaction even
    # when no visible cell currently mentions the hash.
    referenced.update(
        r["blob_hash"]
        for r in project.db.execute(
            "SELECT blob_hash FROM source_artifacts WHERE blob_hash IS NOT NULL"
        )
        if r["blob_hash"] in known
    )
    texts += [
        r[0]
        for r in project.db.execute(
            "SELECT external_ref_json FROM source_artifacts "
            "UNION ALL SELECT metadata FROM source_artifacts "
            "UNION ALL SELECT selector_json FROM source_spans "
            "UNION ALL SELECT preview_json FROM source_spans "
            "UNION ALL SELECT metadata FROM source_spans"
        )
        if r[0]
    ]
    for text in texts:
        if not text:
            continue
        for h in known:
            if h in referenced:
                continue
            if h in text:
                referenced.add(h)
        if len(referenced) == len(known):
            break
    return referenced


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
    referenced = project._referenced_blob_hashes()
    rows = project.db.execute("SELECT hash, size FROM blobs").fetchall()
    orphans = [r for r in rows if r["hash"] not in referenced]
    bytes_freed = 0
    removed: list[str] = []
    for r in orphans:
        if not dry_run:
            project.db.execute("DELETE FROM blobs WHERE hash=?", (r["hash"],))
        removed.append(r["hash"])
    if not dry_run and removed:
        project.db.commit()
    return {
        "blobs_removed": len(removed),
        "bytes_freed": bytes_freed,
        "hashes": removed,
        "dry_run": dry_run,
    }

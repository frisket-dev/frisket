"""Exact SQLite/BLOB vector backend — the rebuildable ``project.embeddings.db``.

Raw vectors live only here, never in project.db. The default backend is
brute-force-exact: little-endian float32
blobs ranked by a registered ``f32_cosine_distance(vec, query, dim)`` scalar so
ranking is expressed as SQL. That query contract is DB-shaped, so an accelerated
backend (sqlite-vec / SQLite Vec1) can replace it later without changing the
public ``query_similar`` signature. sqlite-vec is intentionally NOT a dependency.

The sidecar is rebuildable from definitions + source content: deleting the file
must never corrupt the project. We do not try to make project.db and this DB one
atomic transaction; the canonical truth is definitions/receipts/source content.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from frisket.engine.store import Project

# Little-endian float32, explicit so blobs are portable across architectures
# (struct '<f' fixes byte order regardless of the host's native endianness).
_F32 = struct.Struct("<f")

# Keep the ordinary SQL exclusion path comfortably below SQLite builds whose
# bind-variable ceiling is only 999. Larger hard-NOT result sets are filtered
# after ranked retrieval instead of becoming one giant ``NOT IN`` clause.
_MAX_SQL_EXCLUDE_KEYS = 900

VECTOR_TABLE = "embedding_vectors_f32"

EMBEDDINGS_SIDECAR_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS embedding_items (
  index_id TEXT NOT NULL,
  space_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  source_ref_json TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  vector_table TEXT,
  vector_id INTEGER,
  embedded_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (index_id, source_key)
);

CREATE TABLE IF NOT EXISTS embedding_vectors_f32 (
  id INTEGER PRIMARY KEY,
  dimension INTEGER NOT NULL,
  vec BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embedding_items_space_status
  ON embedding_items(space_id, status);
CREATE INDEX IF NOT EXISTS idx_embedding_items_index_status
  ON embedding_items(index_id, status);
"""


def pack_f32(vec: list[float]) -> bytes:
    """Encode a vector as a little-endian float32 blob."""
    return b"".join(_F32.pack(float(x)) for x in vec)


def unpack_f32(blob: bytes) -> list[float]:
    return [v[0] for v in _F32.iter_unpack(blob)]


def _f32_cosine_distance(vec_blob: bytes, query_blob: bytes, dimension: int) -> float:
    """1 - cosine similarity over two little-endian float32 blobs. Returns 1.0
    (maximally distant) for a zero-norm vector rather than dividing by zero.

    Dimension is enforced, not advisory: both blobs MUST be ``dimension`` float32
    values. A length mismatch is a strict same-space contract violation (a stored
    vector of the wrong width, or a query embedded in another space) and raises
    rather than silently zipping to the shorter length — which would let a 3D
    vector and a 1D query report distance 0.0."""
    width = dimension * _F32.size
    if len(vec_blob) != width or len(query_blob) != width:
        raise ValueError(
            "embedding_space_mismatch: expected "
            f"{dimension}-d float32 vectors ({width} bytes), got "
            f"vec={len(vec_blob)} query={len(query_blob)} bytes"
        )
    a = unpack_f32(vec_blob)
    b = unpack_f32(query_blob)
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    sim = dot / (math.sqrt(na) * math.sqrt(nb))
    # clamp tiny FP overshoot so an identical vector reads exactly 0 distance
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


class VectorBackend:
    """Per-project exact vector store. Open it, ``ensure_schema()``, upsert
    items with vectors, then ``query_similar``. One connection; the scalar
    distance function is registered on it."""

    BACKEND_ID = "sqlite-f32-exact"
    BACKEND_VERSION = "1"

    def __init__(self, project_or_path: Project | str | Path):
        if isinstance(project_or_path, Project) or hasattr(project_or_path, "path"):
            base = project_or_path.path
        else:
            base = Path(project_or_path)
        self.db_path = base / "project.embeddings.db"
        self._conn: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            # deterministic so the same inputs always rank identically
            conn.create_function(
                "f32_cosine_distance", 3, _f32_cosine_distance, deterministic=True
            )
            self._conn = conn
        return self._conn

    def ensure_schema(self) -> None:
        self.db.executescript(EMBEDDINGS_SIDECAR_SCHEMA)
        self.db.commit()

    def begin_read_snapshot(self) -> None:
        """Pin subsequent index reads to one sidecar snapshot."""
        self.db.execute("BEGIN")
        # A deferred transaction is not pinned until its first read.
        self.db.execute("SELECT 1 FROM embedding_items LIMIT 1").fetchone()

    def index_binding(self, index_id: str) -> str:
        """Digest the ordered item identity/state for one index."""
        rows = self.db.execute(
            "SELECT i.space_id, i.source_key, i.source_ref_json, i.source_hash, "
            "i.status, i.error_code, i.error_message, i.vector_table, i.vector_id, "
            "i.embedded_at, i.updated_at, v.dimension, v.vec "
            "FROM embedding_items i LEFT JOIN embedding_vectors_f32 v "
            "ON v.id=i.vector_id WHERE i.index_id=? ORDER BY i.source_key",
            (index_id,),
        )
        digest = hashlib.sha256()
        for row in rows:
            values = tuple(row)
            for value in values:
                encoded = (
                    value if isinstance(value, bytes) else str(value).encode("utf-8")
                )
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        return f"sha256:{digest.hexdigest()}"

    def upsert_item(
        self,
        *,
        index_id: str,
        space_id: str,
        source_key: str,
        source_ref: dict[str, Any],
        source_hash: str,
        status: str,
        vector: list[float] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Insert or replace one item's vector state. A new vector replaces the
        item's previous vector row (no orphan accumulation on re-embed)."""
        existing = self.db.execute(
            "SELECT vector_id FROM embedding_items WHERE index_id=? AND source_key=?",
            (index_id, source_key),
        ).fetchone()
        if existing is not None and existing["vector_id"] is not None:
            self.db.execute(
                "DELETE FROM embedding_vectors_f32 WHERE id=?", (existing["vector_id"],)
            )

        vector_table: str | None = None
        vector_id: int | None = None
        embedded_at: str | None = None
        if vector is not None:
            cur = self.db.execute(
                "INSERT INTO embedding_vectors_f32 (dimension, vec) VALUES (?, ?)",
                (len(vector), pack_f32(vector)),
            )
            vector_id = int(cur.lastrowid)
            vector_table = VECTOR_TABLE
            embedded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        self.db.execute(
            "INSERT INTO embedding_items (index_id, space_id, source_key, "
            "source_ref_json, source_hash, status, error_code, error_message, "
            "vector_table, vector_id, embedded_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(index_id, source_key) DO UPDATE SET "
            "space_id=excluded.space_id, source_ref_json=excluded.source_ref_json, "
            "source_hash=excluded.source_hash, status=excluded.status, "
            "error_code=excluded.error_code, error_message=excluded.error_message, "
            "vector_table=excluded.vector_table, vector_id=excluded.vector_id, "
            "embedded_at=excluded.embedded_at, updated_at=datetime('now')",
            (
                index_id,
                space_id,
                source_key,
                json.dumps(source_ref, sort_keys=True),
                source_hash,
                status,
                error_code,
                error_message,
                vector_table,
                vector_id,
                embedded_at,
            ),
        )
        self.db.commit()

    def get_item(self, index_id: str, source_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM embedding_items WHERE index_id=? AND source_key=?",
            (index_id, source_key),
        ).fetchone()

    def get_vector(self, index_id: str, source_key: str) -> list[float] | None:
        """The stored vector for one ready item, or None when it has no vector
        (missing/stale/errored). The seam a row-anchor similarity uses so it can
        rank WITHOUT calling a provider."""
        row = self.db.execute(
            "SELECT v.vec AS vec FROM embedding_items i "
            "JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
            "WHERE i.index_id=? AND i.source_key=? AND i.status='ready'",
            (index_id, source_key),
        ).fetchone()
        return unpack_f32(row["vec"]) if row is not None else None

    def query_similar(
        self,
        index_id: str,
        space_id: str,
        query_vec: list[float],
        k: int = 50,
        *,
        status: str = "ready",
        exclude_source_keys: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Exact cosine-distance ranking over one (index, space). Only items in
        ``status`` (default ``ready``) participate — never rank against stale or
        errored vectors. Returns ``[(source_key, distance)]``, nearest first, with
        ``source_key`` as a deterministic tie-break so equal distances order
        stably. ``exclude_source_keys`` drops anchors (e.g. the row a "show
        similar" query is anchored to).

        The query vector's dimension is the space's dimension: only vectors of
        matching width are scored (a ``v.dimension`` filter), so a mis-dimensioned
        query yields no false near-matches rather than silently truncated cosines.
        An empty query is rejected — there is no vector to compare."""
        qdim = len(query_vec)
        if qdim == 0:
            raise ValueError("query vector must be non-empty")
        requested = max(1, int(k))
        params: list[Any] = [pack_f32(query_vec), index_id, space_id, status, qdim]
        exclude_sql = ""
        excluded = sorted(exclude_source_keys or ())
        filter_after_query = len(excluded) > _MAX_SQL_EXCLUDE_KEYS
        if excluded and not filter_after_query:
            exclude_sql = f"AND i.source_key NOT IN ({','.join('?' * len(excluded))}) "
            params.extend(excluded)
        query_limit = requested
        if filter_after_query:
            available = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM embedding_items i "
                    "JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
                    "WHERE i.index_id=? AND i.space_id=? AND i.status=? "
                    "AND v.dimension=?",
                    (index_id, space_id, status, qdim),
                ).fetchone()[0]
                or 0
            )
            query_limit = min(available, requested + len(excluded))
        params.append(max(1, query_limit))
        rows = self.db.execute(
            "SELECT i.source_key AS source_key, "
            "f32_cosine_distance(v.vec, ?, v.dimension) AS distance "
            "FROM embedding_items i "
            "JOIN embedding_vectors_f32 v ON v.id = i.vector_id "
            "WHERE i.index_id=? AND i.space_id=? AND i.status=? AND v.dimension=? "
            f"{exclude_sql}"
            "ORDER BY distance ASC, i.source_key ASC LIMIT ?",
            params,
        ).fetchall()
        ranked = [(r["source_key"], float(r["distance"])) for r in rows]
        if filter_after_query:
            excluded_set = set(excluded)
            ranked = [item for item in ranked if item[0] not in excluded_set]
        return ranked[:requested]

    def get_source_hashes(
        self, index_id: str, source_keys: list[str]
    ) -> dict[str, str]:
        """The stored ``source_hash`` for each given ready item — the seam a
        similarity query uses to exclude candidates whose source content has
        changed since their vector was written."""
        out: dict[str, str] = {}
        chunk = 500  # stay under SQLite's bound-parameter cap
        for i in range(0, len(source_keys), chunk):
            keys = source_keys[i : i + chunk]
            placeholders = ",".join("?" * len(keys))
            for r in self.db.execute(
                "SELECT source_key, source_hash FROM embedding_items "
                f"WHERE index_id=? AND status='ready' AND source_key IN ({placeholders})",
                (index_id, *keys),
            ):
                out[str(r["source_key"])] = r["source_hash"]
        return out

    def item_counts(self, index_id: str) -> dict[str, int]:
        """Per-status item counts for an index (advisory index-page numbers)."""
        return {
            str(r["status"]): int(r["n"])
            for r in self.db.execute(
                "SELECT status, COUNT(*) AS n FROM embedding_items "
                "WHERE index_id=? GROUP BY status",
                (index_id,),
            )
        }

    def delete_index(self, index_id: str) -> int:
        """Drop only this index's items and their vectors."""
        ids = [
            int(r["vector_id"])
            for r in self.db.execute(
                "SELECT vector_id FROM embedding_items "
                "WHERE index_id=? AND vector_id IS NOT NULL",
                (index_id,),
            )
        ]
        if ids:
            self.db.executemany(
                "DELETE FROM embedding_vectors_f32 WHERE id=?", [(i,) for i in ids]
            )
        cur = self.db.execute(
            "DELETE FROM embedding_items WHERE index_id=?", (index_id,)
        )
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

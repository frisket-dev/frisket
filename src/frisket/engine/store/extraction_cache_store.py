"""Disposable SQLite sidecar store for project-scoped blob extraction cache.

This leaf module deliberately imports only the Python standard library so
``frisket.engine.store`` (including ``project_blobs`` and ``purge``) can prune
``project.cache.db`` without pulling AI, action, or execution modules into the
store import graph.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("frisket.extraction_cache")

EXTRACTION_CACHE_KEY_VERSION = 1
EXTRACTION_CACHE_SOURCE = "project_extraction_cache"
CACHE_DB_FILENAME = "project.cache.db"
_DELETE_BATCH_SIZE = 500
_SQLITE_TIMEOUT_SECONDS = 30.0

EXTRACTION_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS blob_extraction_cache (
  cache_key TEXT PRIMARY KEY,
  cache_key_version INTEGER NOT NULL,
  blob_hash TEXT NOT NULL,
  page_selector TEXT NOT NULL,
  capability TEXT NOT NULL,
  engine TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  options_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_blob_extraction_cache_blob_hash
  ON blob_extraction_cache(blob_hash);
"""


@dataclass(frozen=True)
class ExtractionCacheKey:
    """Versioned, strict cache key for one blob extraction."""

    cache_key_version: int
    blob_hash: str
    page_selector: str
    capability: str
    engine: str
    engine_version: str
    options_hash: str
    digest: str


def cache_db_path(project: Any) -> Path | None:
    """Return the per-project disposable sidecar path, or ``None`` if unavailable."""
    raw = getattr(project, "path", None)
    if raw is None:
        return None
    return Path(raw) / CACHE_DB_FILENAME


def sidecar_has_symlink(path: Path) -> bool:
    """Return ``True`` if the sidecar database or its WAL/SHM siblings are symlinks."""
    return (
        path.is_symlink()
        or Path(f"{path}-wal").is_symlink()
        or Path(f"{path}-shm").is_symlink()
    )


class BlobExtractionCache:
    """Disposable per-project SQLite cache inside ``project.cache.db``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        self._schema_ready = False

    @classmethod
    def for_project(cls, project: Any) -> BlobExtractionCache | None:
        path = cache_db_path(project)
        return cls(path) if path is not None else None

    def __enter__(self) -> BlobExtractionCache:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
        self._schema_ready = False

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blob_extraction_cache'"
        ).fetchone()
        if row is None:
            conn.executescript(EXTRACTION_CACHE_SCHEMA)
        self._schema_ready = True

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if sidecar_has_symlink(self.path):
            self.close()
            return None
        if not create and not self.path.exists():
            return None
        if self._db is not None:
            if create and not self._schema_ready:
                self._ensure_schema(self._db)
            return self._db
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=_SQLITE_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        conn.execute(f"PRAGMA busy_timeout={int(_SQLITE_TIMEOUT_SECONDS * 1000)}")
        if create:
            self._ensure_schema(conn)
        self._db = conn
        return conn

    def get(self, key: ExtractionCacheKey) -> dict[str, Any] | None:
        """Read one cached payload by exact key, failing closed as a miss on corruption."""
        try:
            conn = self._connect(create=False)
            if conn is None:
                return None
            row = conn.execute(
                "SELECT payload_json FROM blob_extraction_cache "
                "WHERE cache_key=? AND cache_key_version=?",
                (key.digest, key.cache_key_version),
            ).fetchone()
        except (sqlite3.Error, OSError):
            self.close()
            return None
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def put(self, key: ExtractionCacheKey, payload: Mapping[str, Any]) -> None:
        """Store one extraction payload in ``project.cache.db`` (best-effort)."""
        try:
            serialized = json.dumps(
                dict(payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            conn = self._connect(create=True)
            if conn is None:
                return
            with conn:
                conn.execute(
                    "INSERT INTO blob_extraction_cache ("
                    "cache_key, cache_key_version, blob_hash, page_selector, "
                    "capability, engine, engine_version, options_hash, payload_json"
                    ") VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET "
                    "payload_json=excluded.payload_json, "
                    "created_at=datetime('now')",
                    (
                        key.digest,
                        key.cache_key_version,
                        key.blob_hash,
                        key.page_selector,
                        key.capability,
                        key.engine,
                        key.engine_version,
                        key.options_hash,
                        serialized,
                    ),
                )
        except (sqlite3.Error, OSError, TypeError, ValueError):
            logger.warning(
                "blob_extraction_cache write failed; continuing without cache update",
                exc_info=True,
            )
            self.close()

    def delete_for_blobs(self, blob_hashes: Iterable[str]) -> int:
        """Best-effort delete all cached extractions for the given blob hashes."""
        unique = [
            digest
            for digest in dict.fromkeys(blob_hashes)
            if isinstance(digest, str) and digest
        ]
        if not unique:
            return 0
        deleted = 0
        try:
            conn = self._connect(create=False)
            if conn is None:
                return 0
            with conn:
                for start in range(0, len(unique), _DELETE_BATCH_SIZE):
                    batch = unique[start : start + _DELETE_BATCH_SIZE]
                    placeholders = ",".join("?" for _ in batch)
                    cur = conn.execute(
                        "DELETE FROM blob_extraction_cache "
                        f"WHERE blob_hash IN ({placeholders})",
                        batch,
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        deleted += cur.rowcount
        except (sqlite3.Error, OSError):
            self.close()
            return 0
        return deleted


def delete_extraction_cache_for_blobs(project: Any, blob_hashes: Iterable[str]) -> int:
    """Best-effort prune of ``blob_extraction_cache`` entries for purged/reclaimed blobs."""
    cache = BlobExtractionCache.for_project(project)
    if cache is None:
        return 0
    with cache:
        return cache.delete_for_blobs(blob_hashes)

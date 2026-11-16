"""Recover rebuildable retrieval storage; retired embedding caches are ignored."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from frisket.engine.store.search_index_work import enqueue_dirty_scope
from .store import PROJECT_SIDECAR_META_KEY, RetrievalStore, UnsupportedRetrievalSchema

_UPGRADE_LOCK_NAME = ".retrieval-upgrade.lock"
_LOG = logging.getLogger(__name__)


def prepare_retrieval_on_open(project: Any) -> None:
    """Recover disposable retrieval storage under the bundle-open lock."""

    from frisket.search_index import bind_retrieval_sidecar

    project_path = Path(project.path)
    sidecar_exists = (project_path / "project.retrieval.db").is_file()
    if not sidecar_exists:
        # Opening a project remains read-only with respect to derived storage.
        # Persist the repair intent in the authoritative database; the bounded
        # maintenance writer creates and binds the sidecar on its first batch.
        _queue_retrieval_repair(project)
        return
    lock = FileLock(str(project_path / _UPGRADE_LOCK_NAME), timeout=0)
    try:
        with lock:
            sidecar_id = _open_sidecar_identity(project, project_path)
            if sidecar_id is None:
                _queue_retrieval_repair(project)
                return
            if project.get_meta(PROJECT_SIDECAR_META_KEY) != sidecar_id:
                # Project-database failures happen outside sidecar recovery so a
                # healthy derived database is never quarantined for the wrong file.
                bind_retrieval_sidecar(project)
    except Timeout:
        # Another opener owns recovery; do not wait on disposable storage.
        return


def _queue_retrieval_repair(project: Any) -> None:
    with project.transaction_scope():
        if not project.db.execute(
            "SELECT 1 FROM effective_current_cells LIMIT 1"
        ).fetchone():
            return
        global_scope = project.db.execute(
            "SELECT 1 FROM search_dirty_scopes WHERE sheet_id IS NULL "
            "AND scan_cursor='[0,0]' LIMIT 1"
        ).fetchone()
        if global_scope is None:
            enqueue_dirty_scope(project.db)


def _open_sidecar_identity(project: Any, project_path: Path) -> str | None:
    for attempt in range(2):
        try:
            with RetrievalStore(project).open(busy_timeout_ms=0) as retrieval:
                return retrieval.sidecar_id()
        except UnsupportedRetrievalSchema:
            if attempt:
                raise
            _preserve_unusable_sidecar(project_path)
        except sqlite3.DatabaseError as exc:
            primary = _sqlite_primary_error(exc)
            if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return None
            if attempt or primary not in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            }:
                raise
            _preserve_unusable_sidecar(project_path)
    raise AssertionError("sidecar recovery did not return or raise")


def _sqlite_primary_error(exc: sqlite3.DatabaseError) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return None if not isinstance(code, int) else code & 0xFF


def _preserve_unusable_sidecar(project_path: Path) -> None:
    """Keep incompatible cached work intact while a new index is prepared."""

    destination = project_path / f"retrieval-unusable-{uuid.uuid4().hex}"
    destination.mkdir()
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = project_path / f"project.retrieval.db{suffix}"
        if path.exists():
            path.replace(destination / path.name)
    _LOG.warning("Preserved unusable retrieval cache at %s", destination)

"""Workspace-owned plugin package identity catalog.

SINGLE AUTHORITY for plugin PACKAGE identity. Before this catalog, each project
persisted its own copy of a plugin's validated source, manifest/package
digests and ``plugin.load`` manifest evidence in the per-project sqlite table
``workbench_plugin_installs`` while the executable code lived in ONE
process-global registry -- a mismatch that spawned cross-project reconciliation
machinery (workspace-root claims, ``*.frisket/project.db`` globs, restart
re-registration sweeps). This catalog holds exactly ONE durable package
identity per ``plugin_id`` per workspace; projects keep ONLY enablement,
accepted capabilities, settings, secrets and the per-project executable trust
grant.

Storage choice: sqlite at ``<root>/.frisket/plugin_packages.db``. The data here
is exactly the relational identity rows lifted out of the project sqlite
``workbench_plugin_installs`` table -- keyed by ``plugin_id``, looked up by key,
carrying a JSON manifest-evidence blob -- so it mirrors the shape and integrity
guarantees of what it replaces. That is a much closer fit than the workspace's
flat ``runtime_settings.json`` / ``provider_keys.json`` files, which hold a
handful of unkeyed scalar settings; sqlite also gives the many in-process
project handles and the queue worker thread a consistent single-row-per-plugin
read under WAL locking. Connections are opened per operation
(``check_same_thread=False``) to sidestep sqlite's thread-affinity, mirroring
``Project.db``'s WAL + busy_timeout posture.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock

_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_packages (
  plugin_id TEXT PRIMARY KEY,
  install_source TEXT NOT NULL DEFAULT '{}',
  manifest_sha256 TEXT NOT NULL DEFAULT '',
  package_sha256 TEXT NOT NULL DEFAULT '',
  runtime_source TEXT NOT NULL DEFAULT 'plugin.load_receipt',
  receipt_id TEXT,
  manifest_ref TEXT,
  has_executable_backend INTEGER NOT NULL DEFAULT 0,
  executable_activated INTEGER NOT NULL DEFAULT 0,
  install_failure TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS deleted_plugin_packages (
  plugin_id TEXT PRIMARY KEY,
  deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns read back for every catalog row. ``executable_activated`` records
# whether this workspace has ever CONSENTED to run the package's executable
# backend (via a backend/activate that granted executable handlers, or the
# standing grant a bundled package carries). It gates whether restart/rehydration
# REGISTERS the executable runtime bindings into the shared process registry;
# per-project dispatch consent is still enforced separately at
# ``project_runtime_binding``. A merely-installed package (never granted) keeps
# this 0, so its executable code is never registered until a project consents.
_ROW_COLUMNS = (
    "plugin_id, install_source, manifest_sha256, package_sha256, runtime_source, "
    "receipt_id, manifest_ref, has_executable_backend, executable_activated, "
    "install_failure, updated_at"
)

_LIFECYCLE_LOCKS_GUARD = threading.Lock()
_LIFECYCLE_LOCKS: dict[Path, tuple[threading.RLock, FileLock]] = {}


@contextmanager
def plugin_package_lifecycle_lock(workspace_root: str | Path) -> Iterator[None]:
    """One recursive package/project lifecycle lock per canonical workspace.

    ``FileLock`` supplies cross-process exclusion and releases automatically on
    process exit. Reusing the same instance inside one process preserves its
    recursive acquisition semantics for service calls that reach catalog code.
    """
    root = Path(workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _LIFECYCLE_LOCKS_GUARD:
        locks = _LIFECYCLE_LOCKS.get(root)
        if locks is None:
            locks = (
                threading.RLock(),
                FileLock(str(root / ".plugin-package-lifecycle.lock"), timeout=-1),
            )
            _LIFECYCLE_LOCKS[root] = locks
    process_lock, file_lock = locks
    with process_lock:
        with file_lock:
            yield


class PluginPackageCatalog:
    """Durable, workspace-owned package identity keyed by ``plugin_id``."""

    def __init__(self, workspace_root: str | Path) -> None:
        self._root = Path(workspace_root)
        self._db_path = self._root / ".frisket" / "plugin_packages.db"
        self._lock = threading.RLock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000;")
        conn.executescript(_CATALOG_SCHEMA)
        return conn

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT {_ROW_COLUMNS} FROM plugin_packages"
                ).fetchall()
            finally:
                conn.close()
        return {str(row["plugin_id"]): dict(row) for row in rows}

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {_ROW_COLUMNS} FROM plugin_packages WHERE plugin_id=?",
                    (plugin_id,),
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row is not None else None

    def manifest_ref(self, plugin_id: str) -> dict[str, Any] | None:
        """The durable ``plugin.load`` manifest evidence, parsed, or None."""
        entry = self.get(plugin_id)
        if entry is None:
            return None
        return _parse_manifest_ref(entry.get("manifest_ref"))

    def is_deleted(self, plugin_id: str) -> bool:
        """Whether workspace uninstall suppresses automatic bundled seeding."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM deleted_plugin_packages WHERE plugin_id=?",
                    (plugin_id,),
                ).fetchone()
            finally:
                conn.close()
        return row is not None

    def upsert(
        self,
        *,
        plugin_id: str,
        install_source: dict[str, Any] | None,
        manifest_sha256: str,
        package_sha256: str,
        receipt_id: str | None,
        manifest_ref: dict[str, Any] | None,
        has_executable_backend: bool,
        runtime_source: str = "plugin.load_receipt",
        install_failure: dict[str, Any] | None = None,
        clear_tombstone: bool = True,
    ) -> None:
        serialized_source = json.dumps(
            install_source or {}, separators=(",", ":"), sort_keys=True
        )
        serialized_ref = (
            json.dumps(manifest_ref, separators=(",", ":"), sort_keys=True)
            if manifest_ref is not None
            else None
        )
        serialized_failure = (
            json.dumps(install_failure, separators=(",", ":"), sort_keys=True)
            if install_failure is not None
            else None
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO plugin_packages ("
                    "plugin_id, install_source, manifest_sha256, package_sha256, "
                    "runtime_source, receipt_id, manifest_ref, "
                    "has_executable_backend, install_failure, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                    "ON CONFLICT(plugin_id) DO UPDATE SET "
                    "install_source=excluded.install_source, "
                    "manifest_sha256=excluded.manifest_sha256, "
                    "package_sha256=excluded.package_sha256, "
                    "runtime_source=excluded.runtime_source, "
                    "receipt_id=excluded.receipt_id, "
                    "manifest_ref=excluded.manifest_ref, "
                    "has_executable_backend=excluded.has_executable_backend, "
                    "install_failure=excluded.install_failure, "
                    "updated_at=excluded.updated_at",
                    (
                        plugin_id,
                        serialized_source,
                        manifest_sha256,
                        package_sha256,
                        runtime_source,
                        receipt_id,
                        serialized_ref,
                        1 if has_executable_backend else 0,
                        serialized_failure,
                    ),
                )
                if clear_tombstone:
                    conn.execute(
                        "DELETE FROM deleted_plugin_packages WHERE plugin_id=?",
                        (plugin_id,),
                    )
                conn.commit()
            finally:
                conn.close()

    def mark_executable_activated(self, plugin_id: str, value: bool = True) -> None:
        """Record that this workspace has consented to run the package's
        executable backend (a backend/activate granted executable handlers).

        Monotonic on purpose: a later per-project revocation does NOT clear this
        workspace-level fact, because the shared registry may still serve
        another project that granted it, and per-project dispatch consent is
        enforced independently at ``project_runtime_binding``."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE plugin_packages SET executable_activated=?, "
                    "updated_at=datetime('now') WHERE plugin_id=?",
                    (1 if value else 0, plugin_id),
                )
                conn.commit()
            finally:
                conn.close()

    def record_failure(
        self,
        *,
        plugin_id: str,
        install_source: dict[str, Any] | None,
        install_failure: dict[str, Any],
        clear_tombstone: bool = False,
    ) -> None:
        """Record a failed install with no valid package identity evidence."""
        self.upsert(
            plugin_id=plugin_id,
            install_source=install_source,
            manifest_sha256="",
            package_sha256="",
            receipt_id=None,
            manifest_ref=None,
            has_executable_backend=False,
            install_failure=install_failure,
            clear_tombstone=clear_tombstone,
        )

    def delete(self, plugin_id: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO deleted_plugin_packages (plugin_id, deleted_at) "
                    "VALUES (?, datetime('now')) ON CONFLICT(plugin_id) DO UPDATE "
                    "SET deleted_at=excluded.deleted_at",
                    (plugin_id,),
                )
                conn.execute(
                    "DELETE FROM plugin_packages WHERE plugin_id=?", (plugin_id,)
                )
                conn.commit()
            finally:
                conn.close()


def _parse_manifest_ref(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def plugin_package_catalog_for_root(root: str | Path) -> PluginPackageCatalog:
    """Open the catalog for a workspace root.

    Deliberately constructs a fresh handle rather than memoizing: the catalog
    is durable sqlite, so every handle over the same ``<root>/.frisket/
    plugin_packages.db`` observes the one authoritative set of rows. The
    ``Workspace`` still OWNS the catalog in the sense that it triggers seeding
    and registry rehydration once at construction; this accessor lets the deep
    read surfaces (which only carry a ``Project``) reach that same file without
    threading a catalog reference through every signature.
    """
    return PluginPackageCatalog(root)


def plugin_package_catalog_for_project(project: Any) -> PluginPackageCatalog:
    """Catalog for the workspace that owns ``project`` (its bundle's parent)."""
    return plugin_package_catalog_for_root(project.path.parent)

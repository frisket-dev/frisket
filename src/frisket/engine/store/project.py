"""Project store: a ``.frisket`` directory bundle.

Layout:
    myproject.frisket/
      manifest.json
      project.db          (irreplaceable data only)
      project.search.db   (sidecar: FTS + vectors, rebuildable)
      project.cache.db    (sidecar: response cache, rebuildable)
      blobs/ab/abcd1234...

Value resolution for a cell, highest precedence first:
    1. latest applied manual edit (edits joined to applied ops)
    2. the column's exact generated-result head
    3. source cells
Undo/redo move op-log pointers and column run-pointers; nothing is copied.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
import weakref
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Collection


from . import (
    bundle_io,
    bundle_open,
    cells,
    credentials,
    notifications,
    op_log,
    project_blobs,
    project_meta,
    saved_views,
    sheet_lifecycle,
    watches,
)
from .schema import (
    FORMAT_VERSION,
    SCHEMA,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
    BundleSchemaMismatch as BundleSchemaMismatch,
)
from .bundle_io import delete_bundle as delete_bundle
from .cells import (
    COLUMN_FORMATS as COLUMN_FORMATS,
    COLUMN_TYPES as COLUMN_TYPES,
)
from .project_meta import (
    PROJECT_SCHEMA_VERSION as PROJECT_SCHEMA_VERSION,
    RETENTION_POLICY_META_KEY,
    STORAGE_ID_META_KEY,
    SUPPORTED_EVIDENCE_RETENTION_VALUES as SUPPORTED_EVIDENCE_RETENTION_VALUES,
    _normalize_retention_policy,
)
from .blob_backend import (
    FilesystemProjectBlobStore,
    ProjectBlobStore,
)


class _ThreadConnection:
    """A thread-owned connection that closes when its thread state is retired."""

    __slots__ = ("connection", "_closed", "__weakref__")

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Destructors must remain safe during interpreter shutdown.
            pass


class ProjectReadSnapshot:
    """One connection-owned, read-only SQLite snapshot of a project.

    Unlike :attr:`Project.db`, this connection deliberately follows the
    snapshot across threads.  That is required by response iterators, whose
    successive ``next()`` calls may run on different FastAPI worker threads.
    Callers must still use it serially and close it when the read completes.
    """

    def __init__(self, owner: Project):
        self.path = owner.path
        self.db_path = owner.db_path
        self.blob_store = owner.blob_store
        self._close_lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

        connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    @property
    def db(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("project read snapshot is closed")
        return connection

    def columns(self, sheet_id: int, include_hidden: bool = False) -> list[sqlite3.Row]:
        return cells.columns(self, sheet_id, include_hidden)

    def sheets(self, include_hidden: bool = False) -> list[sqlite3.Row]:
        return cells.sheets(self, include_hidden)

    def row_count(self, sheet_id: int) -> int:
        return cells.row_count(self, sheet_id)

    @property
    def op_cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='op_cursor'").fetchone()
        return int(row["value"] if row is not None else "0")

    def get_values_with_refs(
        self,
        sheet_id: int,
        column_id: int,
        row_ids: list[int] | None = None,
        *,
        apply_edits: bool = True,
        tolerate_decode_errors: bool = False,
    ) -> tuple[dict[int, Any], dict[int, dict[str, Any]]]:
        return cells.get_values_with_refs(
            self,
            sheet_id,
            column_id,
            row_ids,
            apply_edits=apply_edits,
            tolerate_decode_errors=tolerate_decode_errors,
        )

    def get_values(
        self,
        sheet_id: int,
        column_id: int,
        row_ids: list[int] | None = None,
        *,
        apply_edits: bool = True,
        tolerate_decode_errors: bool = False,
    ) -> dict[int, Any]:
        return cells.get_values(
            self,
            sheet_id,
            column_id,
            row_ids,
            apply_edits=apply_edits,
            tolerate_decode_errors=tolerate_decode_errors,
        )

    def visible_row_ids(
        self, sheet_id: int, row_ids: list[int] | None = None
    ) -> list[int]:
        return cells.visible_row_ids(self, sheet_id, row_ids)

    def close(self) -> None:
        with self._close_lock:
            connection = self._connection
            self._connection = None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            pass
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> ProjectReadSnapshot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class Project:
    """Synchronous core store. The server wraps this with a write queue;
    SQLite WAL + per-project serialization is the concurrency model."""

    def __init__(
        self,
        bundle_path: str | Path,
        *,
        blob_store: ProjectBlobStore | None = None,
    ):
        self.path = Path(bundle_path)
        self.db_path = self.path / "project.db"
        self.blob_store: ProjectBlobStore = (
            blob_store
            if blob_store is not None
            else FilesystemProjectBlobStore.for_bundle(self.path)
        )
        if not self.db_path.exists():
            raise FileNotFoundError(f"not a frisket bundle: {self.path}")
        self._local = threading.local()
        self._connections: weakref.WeakSet[_ThreadConnection] = weakref.WeakSet()
        self._connections_lock = threading.Lock()
        self._closed = False
        _ = self.db  # open the creating thread's connection eagerly
        bundle_open.open_bundle(self)

    @property
    def db(self) -> sqlite3.Connection:
        """Per-thread connection. FastAPI sync endpoints run in a threadpool;
        a single shared sqlite3 connection races across threads (statement
        cache reuse → InterfaceError / phantom empty fetches). WAL provides
        multi-reader/single-writer across connections; busy_timeout absorbs
        write contention."""
        thread_connection: _ThreadConnection | None = getattr(
            self._local, "connection", None
        )
        if thread_connection is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; "
                "PRAGMA busy_timeout=10000;"
            )
            thread_connection = _ThreadConnection(conn)
            self._local.connection = thread_connection
            with self._connections_lock:
                self._connections.add(thread_connection)
        return thread_connection.connection

    # ---------- lifecycle ----------

    def read_snapshot(self) -> ProjectReadSnapshot:
        """Open a serial-use read facade pinned to one SQLite snapshot."""
        return ProjectReadSnapshot(self)

    @classmethod
    def create(
        cls,
        bundle_path: str | Path,
        name: str | None = None,
        *,
        sensitive: bool = False,
        blob_store: ProjectBlobStore | None = None,
    ) -> "Project":
        path = Path(bundle_path)
        if path.exists() and any(path.iterdir()):
            raise FileExistsError(f"{path} exists and is not empty")
        path.mkdir(parents=True, exist_ok=True)
        retention_policy = _normalize_retention_policy()
        storage_id = f"frisket.bundle.v1:{uuid.uuid4()}"
        db = sqlite3.connect(path / "project.db")
        db.executescript(SCHEMA)
        # The schema stamp is written in the SAME transaction as the DDL
        # replay above, so a bundle can never exist stamped-but-unbuilt or
        # built-but-unstamped: either state would be read by the open fence
        # as somebody else's bundle.
        db.execute(
            "INSERT INTO meta (key, value) VALUES "
            "('format_version', ?), ('name', ?), ('op_cursor', '0'), "
            "('sensitive', ?), (?, ?), (?, ?), (?, ?)",
            (
                str(FORMAT_VERSION),
                name or path.stem,
                # Written here as well as to the manifest, on the same terms as
                # ``set_project_sensitivity``: the manifest is an externalized
                # summary and can be rebuilt from the db, never the reverse. A
                # project created sensitive whose manifest is lost would
                # otherwise come back not-sensitive and resume the telemetry
                # its owner had switched off.
                "true" if sensitive else "false",
                RETENTION_POLICY_META_KEY,
                json.dumps(retention_policy, sort_keys=True),
                STORAGE_ID_META_KEY,
                storage_id,
                SCHEMA_DIGEST_META_KEY,
                SCHEMA_DIGEST,
            ),
        )
        db.commit()
        db.close()
        cls._write_initial_manifest(
            path,
            name or path.stem,
            retention_policy,
            storage_id=storage_id,
            sensitive=sensitive,
        )
        return cls(path, blob_store=blob_store)

    @staticmethod
    def _write_initial_manifest(
        path: Path,
        name: str,
        retention_policy: dict[str, Any] | None = None,
        *,
        storage_id: str,
        sensitive: bool = False,
    ) -> None:
        """Create-time manifest (no Project instance exists yet)."""
        retention = _normalize_retention_policy(retention_policy)
        manifest = {
            "format": "frisket-bundle",
            "schema_version": PROJECT_SCHEMA_VERSION,
            "format_version": FORMAT_VERSION,
            "project_id": path.stem,
            "storage_id": storage_id,
            "name": name,
            "sensitive": sensitive,
            "retention": retention,
        }
        # Same atomic publish as every later write (project_meta), so a crash
        # or a concurrent reader can never see a half-created bundle as a
        # zero-byte manifest. No lock: nothing else can hold a handle on a
        # bundle that is being created.
        project_meta.write_manifest_atomically(path, manifest)

    def close(self) -> None:
        with self._connections_lock:
            # A retired hosted runtime must never publish new canonical bytes
            # through a stale Project reference. Ordinary ``db`` access keeps
            # its legacy reopen behavior, but blob effects are one-way and
            # remain fenced for the lifetime of this object after close.
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()
        self._local = threading.local()  # a later .db access reopens cleanly

    # ---------- meta ----------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        if commit:
            self.db.commit()

    @property
    def storage_identity(self) -> str:
        """Stable bundle identity for external exact-once references.

        Unlike a slug or filesystem path, this value is minted once with the
        bundle and survives reopen, rename, export, and mount relocation.  This
        is a pre-release clean cut: bundles without the identity are rejected,
        not silently assigned an identity during a billing attempt.
        """
        value = self.get_meta(STORAGE_ID_META_KEY)
        if not isinstance(value, str) or not value.startswith("frisket.bundle.v1:"):
            raise ValueError(
                "project bundle has no supported stable storage identity; "
                "recreate it under the current pre-release bundle format"
            )
        return value

    def _write_manifest(self, **updates: Any) -> None:
        project_meta._write_manifest(self, **updates)

    def refresh_pending_review_summary(self) -> int:
        return project_meta.refresh_pending_review_summary(self)

    def project_metadata(self) -> dict[str, Any]:
        return project_meta.project_metadata(self)

    def set_project_metadata(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        starred: bool | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        return project_meta.set_project_metadata(
            self, name=name, description=description, starred=starred, archived=archived
        )

    def set_project_sensitivity(self, sensitive: bool) -> dict[str, Any]:
        return project_meta.set_project_sensitivity(self, sensitive)

    def _ensure_retention_policy(self) -> dict[str, Any]:
        return project_meta._ensure_retention_policy(self)

    def retention_policy(self) -> dict[str, Any]:
        return project_meta.retention_policy(self)

    def set_retention_policy(
        self,
        *,
        default_evidence: str | None = None,
        pin_evidence_by_default: bool | None = None,
        no_compact: bool | None = None,
    ) -> dict[str, Any]:
        return project_meta.set_retention_policy(
            self,
            default_evidence=default_evidence,
            pin_evidence_by_default=pin_evidence_by_default,
            no_compact=no_compact,
        )

    def network_policy(self) -> dict[str, Any]:
        return project_meta.network_policy(self)

    def set_network_policy(
        self,
        *,
        mode: str | None = None,
        org_default: str | None = None,
        clear_org_default: bool = False,
    ) -> dict[str, Any]:
        return project_meta.set_network_policy(
            self,
            mode=mode,
            org_default=org_default,
            clear_org_default=clear_org_default,
        )

    def effective_network_policy(self) -> str:
        return project_meta.effective_network_policy(self)

    # ---------- project settings: provider keys / secrets ----------

    def provider_key_catalog_rows(self) -> dict[str, dict[str, Any]]:
        return credentials.provider_key_catalog_rows(self)

    def set_provider_key(
        self,
        *,
        provider: str,
        encrypted: str,
        hint: str,
        spend_cap_micro: int | None,
    ) -> None:
        credentials.set_provider_key(
            self,
            provider=provider,
            encrypted=encrypted,
            hint=hint,
            spend_cap_micro=spend_cap_micro,
        )

    def delete_provider_key(self, provider: str) -> bool:
        return credentials.delete_provider_key(self, provider)

    def provider_spend_state(
        self, provider: str
    ) -> credentials.ProviderSpendState | None:
        return credentials.provider_spend_state(self, provider)

    def accrue_provider_spend(
        self, deltas: Mapping[str, credentials.ProviderSpendDelta]
    ) -> None:
        """Uncommitted: the caller owns the surrounding transaction."""
        credentials.accrue_provider_spend(self, deltas)

    def provider_model_keys(self) -> dict[str, str]:
        return credentials.provider_model_keys(self)

    def secret_rows(self) -> list[sqlite3.Row]:
        return credentials.secret_rows(self)

    def secret_migration_conflict_rows(self) -> list[sqlite3.Row]:
        return credentials.secret_migration_conflict_rows(self)

    def secret_consumer_rows(self) -> list[sqlite3.Row]:
        return credentials.secret_consumer_rows(self)

    def set_secret(self, *, name: str, encrypted: str, hint: str) -> None:
        credentials.set_secret(self, name=name, encrypted=encrypted, hint=hint)

    def add_secret_consumer(self, *, kind: str, consumer_id: str, name: str) -> None:
        credentials.add_secret_consumer(
            self, kind=kind, consumer_id=consumer_id, name=name
        )

    def secret_consumer_is_declared(
        self, *, kind: str, consumer_id: str, name: str
    ) -> bool:
        return credentials.secret_consumer_is_declared(
            self, kind=kind, consumer_id=consumer_id, name=name
        )

    def secret_plaintext(self, name: str) -> str | None:
        return credentials.secret_plaintext(self, name)

    def delete_secret(self, name: str) -> bool:
        return credentials.delete_secret(self, name)

    # ---------- sheets / columns / rows ----------

    def add_sheet(
        self,
        name: str,
        parent_sheet_id: int | None = None,
        parent_op_id: int | None = None,
        *,
        commit: bool = True,
    ) -> int:
        return cells.add_sheet(
            self,
            name,
            parent_sheet_id,
            parent_op_id,
            commit=commit,
        )

    def sheets(self, include_hidden: bool = False) -> list[sqlite3.Row]:
        return cells.sheets(self, include_hidden)

    def dependent_sheets(self, sheet_id: int) -> list[dict[str, Any]]:
        return sheet_lifecycle.dependent_sheets(self, sheet_id)

    def delete_sheet(self, sheet_id: int) -> dict[str, Any]:
        return sheet_lifecycle.delete_sheet(self, sheet_id)

    def set_sheet_title_column(
        self, sheet_id: int, title_column_id: int | None
    ) -> None:
        cells.set_sheet_title_column(self, sheet_id, title_column_id)

    def add_column(
        self,
        sheet_id: int,
        name: str,
        type: str = "text",
        ai_generated: bool = False,
        format: str | None = None,
        hidden: bool = False,
        default_hidden: bool = False,
        semantic_type: str | None = None,
        *,
        commit: bool = True,
    ) -> int:
        return cells.add_column(
            self,
            sheet_id,
            name,
            type,
            ai_generated,
            format,
            hidden,
            default_hidden,
            semantic_type,
            commit=commit,
        )

    def columns(self, sheet_id: int, include_hidden: bool = False) -> list[sqlite3.Row]:
        return cells.columns(self, sheet_id, include_hidden)

    def get_column(self, column_id: int) -> sqlite3.Row | None:
        return cells.get_column(self, column_id)

    def set_column_default_hidden(
        self, column_id: int, hidden: bool = True, *, commit: bool = True
    ) -> None:
        cells.set_column_default_hidden(self, column_id, hidden, commit=commit)

    def set_column_type(
        self, column_id: int, type: str, *, commit: bool = True
    ) -> None:
        cells.set_column_type(self, column_id, type, commit=commit)

    def set_column_format(
        self,
        column_id: int,
        format: str | None,
        *,
        commit: bool = True,
    ) -> None:
        cells.set_column_format(self, column_id, format, commit=commit)

    def set_column_semantic_type(
        self,
        column_id: int,
        semantic_type: str | None,
        *,
        commit: bool = True,
    ) -> None:
        cells.set_column_semantic_type(
            self,
            column_id,
            semantic_type,
            commit=commit,
        )

    def add_rows(
        self,
        sheet_id: int,
        records: list[dict[str, Any]],
        column_ids: dict[str, int],
        parent_row_ids: list[int] | None = None,
        *,
        producer_id: int | None = None,
        commit: bool = True,
    ) -> list[int]:
        return cells.add_rows(
            self,
            sheet_id,
            records,
            column_ids,
            parent_row_ids,
            producer_id=producer_id,
            commit=commit,
        )

    def add_row_with_undo(
        self, sheet_id: int, record: dict[str, Any], column_ids: dict[str, int]
    ) -> int:
        return cells.add_row_with_undo(self, sheet_id, record, column_ids)

    def row_count(self, sheet_id: int) -> int:
        return cells.row_count(self, sheet_id)

    def visible_row_ids(
        self, sheet_id: int, row_ids: list[int] | None = None
    ) -> list[int]:
        return cells.visible_row_ids(self, sheet_id, row_ids)

    def get_values_with_refs(
        self,
        sheet_id: int,
        column_id: int,
        row_ids: list[int] | None = None,
        *,
        apply_edits: bool = True,
        tolerate_decode_errors: bool = False,
    ) -> tuple[dict[int, Any], dict[int, dict[str, Any]]]:
        return cells.get_values_with_refs(
            self,
            sheet_id,
            column_id,
            row_ids,
            apply_edits=apply_edits,
            tolerate_decode_errors=tolerate_decode_errors,
        )

    def get_values(
        self,
        sheet_id: int,
        column_id: int,
        row_ids: list[int] | None = None,
        *,
        apply_edits: bool = True,
        tolerate_decode_errors: bool = False,
    ) -> dict[int, Any]:
        return cells.get_values(
            self,
            sheet_id,
            column_id,
            row_ids,
            apply_edits=apply_edits,
            tolerate_decode_errors=tolerate_decode_errors,
        )

    def apply_edits(
        self,
        edits: list[dict[str, Any]],
        label: str = "manual edit",
        *,
        spec: dict[str, Any] | None = None,
    ) -> int:
        return cells.apply_edits(self, edits, label, spec=spec)

    def pending_replay_values(
        self, sheet_id: int, column_id: int, row_ids: list[int] | None = None
    ) -> dict[int, dict[str, Any]]:
        return cells.pending_replay_values(self, sheet_id, column_id, row_ids)

    def pending_replay_count(self, sheet_id: int, column_id: int) -> int:
        return cells.pending_replay_count(self, sheet_id, column_id)

    # ---------- op log ----------

    def append_op(
        self,
        kind: str,
        spec: dict[str, Any] | None = None,
        label: str | None = None,
        barrier: bool = False,
        *,
        commit: bool = True,
    ) -> int:
        return op_log.append_op(self, kind, spec, label, barrier, commit=commit)

    def set_undo_info(
        self, op_id: int, undo_info: dict[str, Any], *, commit: bool = True
    ) -> None:
        op_log.set_undo_info(self, op_id, undo_info, commit=commit)

    def history(self) -> list[sqlite3.Row]:
        return op_log.history(self)

    def history_page(self, offset: int, limit: int) -> list[sqlite3.Row]:
        return op_log.history_page(self, offset, limit)

    def history_total(self) -> int:
        return op_log.history_total(self)

    def history_cursor_index(self) -> int:
        return op_log.history_cursor_index(self)

    def undo(self) -> int | None:
        return op_log.undo(self)

    def redo(self) -> int | None:
        return op_log.redo(self)

    def _unapply(self, op: sqlite3.Row) -> None:
        op_log._unapply(self, op)

    def _reapply(self, op: sqlite3.Row) -> None:
        op_log._reapply(self, op)

    def ops_meta(self, op_ids: list[int]) -> dict[int, dict[str, Any]]:
        return op_log.ops_meta(self, op_ids)

    @property
    def op_cursor(self) -> int:
        return int(self.get_meta("op_cursor", "0"))

    # ---------- value resolution ----------

    # ---------- replay preserve+surface ----------

    # ---------- blobs ----------

    def _assert_blob_write_open(self) -> None:
        """Refuse stale-object blob effects before entering the backend port."""
        with self._connections_lock:
            if self._closed:
                raise RuntimeError("closed project cannot accept a blob write")

    def add_blob(
        self,
        data: bytes,
        filename: str | None = None,
        mime: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        commit: bool = True,
    ) -> str:
        self._assert_blob_write_open()
        return project_blobs.add_blob(
            self, data, filename, mime, source_url, metadata, commit=commit
        )

    def add_blob_from_path(
        self,
        path: str | Path,
        filename: str | None = None,
        mime: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        commit: bool = True,
        expected_digest: str | None = None,
    ) -> str:
        self._assert_blob_write_open()
        return project_blobs.add_blob_from_path(
            self,
            path,
            filename,
            mime,
            source_url,
            metadata,
            commit=commit,
            expected_digest=expected_digest,
        )

    def record_blob_derivation(
        self,
        *,
        derived_hash: str,
        source_hash: str,
        op: str,
        params: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> int:
        return project_blobs.record_blob_derivation(
            self,
            derived_hash=derived_hash,
            source_hash=source_hash,
            op=op,
            params=params,
            commit=commit,
        )

    def blob_derivation(self, derived_hash: str) -> sqlite3.Row | None:
        return project_blobs.blob_derivation(self, derived_hash)

    def blob_derivation_chain(self, digest: str) -> list[dict[str, Any]]:
        return project_blobs.blob_derivation_chain(self, digest)

    def _referenced_blob_hashes(self) -> set[str]:
        return project_blobs._referenced_blob_hashes(self)

    def gc_blobs(self, dry_run: bool = False) -> dict[str, Any]:
        return project_blobs.gc_blobs(self, dry_run)

    def materialize_blob(self, digest: str) -> AbstractContextManager[Path]:
        return self.blob_store.materialize(digest)

    def read_blob(self, digest: str) -> bytes:
        with self.materialize_blob(digest) as path:
            return Path(path).read_bytes()

    # ---------- saved views (filter/flag op) ----------

    def views(self, sheet_id: int | None = None) -> list[sqlite3.Row]:
        return saved_views.views(self, sheet_id)

    def get_view(self, view_id: int) -> sqlite3.Row | None:
        return saved_views.get_view(self, view_id)

    def add_view(
        self,
        name: str,
        spec: dict[str, Any] | None = None,
        *,
        sheet_id: int,
        commit: bool = True,
    ) -> int:
        return saved_views.add_view(self, name, spec, sheet_id=sheet_id, commit=commit)

    def update_view(
        self,
        view_id: int,
        name: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> None:
        saved_views.update_view(self, view_id, name, spec)

    def delete_view(self, view_id: int) -> None:
        saved_views.delete_view(self, view_id)

    def lenses(self, sheet_id: int | None = None) -> list[sqlite3.Row]:
        return saved_views.lenses(self, sheet_id)

    def get_lens(self, lens_id: int) -> sqlite3.Row | None:
        return saved_views.get_lens(self, lens_id)

    def add_lens(
        self,
        name: str,
        spec: dict[str, Any],
        sheet_id: int | None = None,
    ) -> int:
        return saved_views.add_lens(self, name, spec, sheet_id)

    def update_lens(
        self,
        lens_id: int,
        name: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> None:
        saved_views.update_lens(self, lens_id, name, spec)

    def delete_lens(self, lens_id: int) -> None:
        saved_views.delete_lens(self, lens_id)

    # ----- saved lenses -----

    # ---------- watchlists ----------

    def watches(self) -> list[sqlite3.Row]:
        return watches.watches(self)

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return watches.get_watch(self, watch_id)

    def add_watch(
        self,
        name: str,
        *,
        scope: str,
        query: dict[str, Any],
        sheet_id: int | None = None,
        detection_policy: dict[str, Any] | None = None,
        enabled: bool = True,
        commit: bool = True,
    ) -> int:
        return watches.add_watch(
            self,
            name,
            scope=scope,
            query=query,
            sheet_id=sheet_id,
            detection_policy=detection_policy,
            enabled=enabled,
            commit=commit,
        )

    def update_watch(
        self,
        watch_id: int,
        *,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> sqlite3.Row | None:
        return watches.update_watch(
            self,
            watch_id,
            name=name,
            enabled=enabled,
        )

    def delete_watch(self, watch_id: int) -> bool:
        return watches.delete_watch(self, watch_id)

    def watch_latest_run(self, watch_id: int) -> sqlite3.Row | None:
        return watches.watch_latest_run(self, watch_id)

    def get_watch_run(self, run_id: int) -> sqlite3.Row | None:
        return watches.get_watch_run(self, run_id)

    def watch_runs_page(
        self,
        watch_id: int,
        offset: int = 0,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return watches.watch_runs_page(self, watch_id, offset, limit)

    def watch_runs_total(self, watch_id: int) -> int:
        return watches.watch_runs_total(self, watch_id)

    def watch_run_hits(
        self,
        run_id: int,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        return watches.watch_run_hits(self, run_id, limit)

    def watch_run_events(
        self,
        run_id: int,
        *,
        offset: int = 0,
        limit: int = 50,
        event_kind: str | None = None,
    ) -> list[sqlite3.Row]:
        return watches.watch_run_events(
            self, run_id, offset=offset, limit=limit, event_kind=event_kind
        )

    def watch_run_events_total(
        self,
        run_id: int,
        *,
        event_kind: str | None = None,
    ) -> int:
        return watches.watch_run_events_total(self, run_id, event_kind=event_kind)

    def watch_events_for_notification(
        self, *, watch_id: int, run_id: int, event_ids: list[int]
    ) -> list[sqlite3.Row]:
        return watches.watch_events_for_notification(
            self, watch_id=watch_id, run_id=run_id, event_ids=event_ids
        )

    def record_watch_run(
        self,
        watch_id: int,
        *,
        hits: list[dict[str, Any]],
        status: str = "ok",
        error: str | None = None,
        op_cursor_before: int,
        op_cursor_after: int,
        resolved_query_hash: str | None = None,
        resolved_query: dict[str, Any] | None = None,
        error_code: str | None = None,
        advance_cursor: bool = True,
    ) -> int:
        return watches.record_watch_run(
            self,
            watch_id,
            hits=hits,
            status=status,
            error=error,
            op_cursor_before=op_cursor_before,
            op_cursor_after=op_cursor_after,
            resolved_query_hash=resolved_query_hash,
            resolved_query=resolved_query,
            error_code=error_code,
            advance_cursor=advance_cursor,
        )

    # ---------- notifications ----------

    def notification_item_by_dedupe_key(self, dedupe_key: str) -> sqlite3.Row | None:
        return notifications.notification_item_by_dedupe_key(self, dedupe_key)

    def notification_item(self, notification_id: int) -> sqlite3.Row | None:
        return notifications.notification_item(self, notification_id)

    def insert_notification_item(
        self,
        *,
        source_kind: str,
        source_ref: str,
        dedupe_key: str,
        source_event_ids: str,
        event_count: int,
        event_kinds: str,
        title: str,
        summary: str,
        severity: str,
        deep_link: str,
        payload: str,
    ) -> int:
        return notifications.insert_notification_item(
            self,
            source_kind=source_kind,
            source_ref=source_ref,
            dedupe_key=dedupe_key,
            source_event_ids=source_event_ids,
            event_count=event_count,
            event_kinds=event_kinds,
            title=title,
            summary=summary,
            severity=severity,
            deep_link=deep_link,
            payload=payload,
        )

    def update_notification_item(
        self,
        notification_id: int | None,
        *,
        source_event_ids: str,
        event_count: int,
        event_kinds: str,
        title: str,
        summary: str,
        severity: str,
        deep_link: str,
        payload: str,
    ) -> None:
        notifications.update_notification_item(
            self,
            notification_id,
            source_event_ids=source_event_ids,
            event_count=event_count,
            event_kinds=event_kinds,
            title=title,
            summary=summary,
            severity=severity,
            deep_link=deep_link,
            payload=payload,
        )

    def list_notification_items(
        self,
        *,
        actor_id: str,
        state: str = "all",
        source_kind: str | None = None,
        source_ref: dict[str, Any] | None = None,
        severity: str | None = None,
        visible_delivery_route_ids: Collection[int] | None = None,
        visible_delivery_channel_ids: Collection[int] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return notifications.list_notification_items(
            self,
            actor_id=actor_id,
            state=state,
            source_kind=source_kind,
            source_ref=source_ref,
            severity=severity,
            visible_delivery_route_ids=visible_delivery_route_ids,
            visible_delivery_channel_ids=visible_delivery_channel_ids,
            offset=offset,
            limit=limit,
        )

    def notification_items_total(
        self,
        *,
        actor_id: str,
        state: str = "all",
        source_kind: str | None = None,
        source_ref: dict[str, Any] | None = None,
        severity: str | None = None,
        visible_delivery_route_ids: Collection[int] | None = None,
        visible_delivery_channel_ids: Collection[int] | None = None,
    ) -> int:
        return notifications.notification_items_total(
            self,
            actor_id=actor_id,
            state=state,
            source_kind=source_kind,
            source_ref=source_ref,
            severity=severity,
            visible_delivery_route_ids=visible_delivery_route_ids,
            visible_delivery_channel_ids=visible_delivery_channel_ids,
        )

    def notification_summary(
        self,
        *,
        actor_id: str,
        visible_delivery_route_ids: Collection[int] | None = None,
        visible_delivery_channel_ids: Collection[int] | None = None,
    ) -> dict[str, Any]:
        return notifications.notification_summary(
            self,
            actor_id=actor_id,
            visible_delivery_route_ids=visible_delivery_route_ids,
            visible_delivery_channel_ids=visible_delivery_channel_ids,
        )

    def ensure_notification_actor_state(
        self, notification_id: int, actor_id: str
    ) -> sqlite3.Row:
        return notifications.ensure_notification_actor_state(
            self, notification_id, actor_id
        )

    def notification_actor_state(
        self, notification_id: int, actor_id: str
    ) -> dict[str, Any]:
        return notifications.notification_actor_state(self, notification_id, actor_id)

    def mark_notifications_seen(
        self,
        *,
        actor_id: str,
        notification_ids: list[int] | None = None,
        source_kind: str | None = None,
        source_ref: dict[str, Any] | None = None,
        before_created_at: str | None = None,
    ) -> int:
        return notifications.mark_notifications_seen(
            self,
            actor_id=actor_id,
            notification_ids=notification_ids,
            source_kind=source_kind,
            source_ref=source_ref,
            before_created_at=before_created_at,
        )

    def mark_notification_read(
        self, notification_id: int, actor_id: str
    ) -> dict[str, Any]:
        return notifications.mark_notification_read(self, notification_id, actor_id)

    def acknowledge_notification(
        self, notification_id: int, actor_id: str
    ) -> dict[str, Any]:
        return notifications.acknowledge_notification(self, notification_id, actor_id)

    def acknowledge_notifications(
        self,
        *,
        actor_id: str,
        notification_ids: list[int] | None = None,
        source_kind: str | None = None,
        source_ref: dict[str, Any] | None = None,
        before_created_at: str | None = None,
    ) -> int:
        return notifications.acknowledge_notifications(
            self,
            actor_id=actor_id,
            notification_ids=notification_ids,
            source_kind=source_kind,
            source_ref=source_ref,
            before_created_at=before_created_at,
        )

    def unacknowledge_notification(
        self, notification_id: int, actor_id: str
    ) -> dict[str, Any]:
        return notifications.unacknowledge_notification(self, notification_id, actor_id)

    def create_notification_channel(
        self,
        *,
        kind: str,
        name: str,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
        secret_ref: str | None = None,
    ) -> dict[str, Any]:
        return notifications.create_notification_channel(
            self,
            kind=kind,
            name=name,
            enabled=enabled,
            config=config,
            secret_ref=secret_ref,
        )

    def ensure_default_in_app_notification_channel(self) -> dict[str, Any]:
        return notifications.ensure_default_in_app_notification_channel(self)

    def notification_channel(self, channel_id: int) -> sqlite3.Row | None:
        return notifications.notification_channel(self, channel_id)

    def notification_channels(self) -> list[sqlite3.Row]:
        return notifications.notification_channels(self)

    def enabled_notification_channels(self) -> list[sqlite3.Row]:
        return notifications.enabled_notification_channels(self)

    def update_notification_channel(
        self,
        channel_id: int,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
        secret_ref: str | None = None,
        set_secret_ref: bool = False,
    ) -> dict[str, Any]:
        return notifications.update_notification_channel(
            self,
            channel_id,
            name=name,
            enabled=enabled,
            config=config,
            secret_ref=secret_ref,
            set_secret_ref=set_secret_ref,
        )

    def public_notification_channel(self, channel_id: int) -> dict[str, Any]:
        return notifications.public_notification_channel(self, channel_id)

    def public_notification_channels(self) -> list[dict[str, Any]]:
        return notifications.public_notification_channels(self)

    def notification_route(self, route_id: int) -> sqlite3.Row | None:
        return notifications.notification_route(self, route_id)

    def notification_routes(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        return notifications.notification_routes(self, enabled_only=enabled_only)

    def create_notification_route(
        self,
        *,
        name: str,
        channel_id: int,
        enabled: bool = True,
        owner_kind: str = "project",
        owner_ref: str | None = None,
        recipient_actor_id: str | None = None,
        source_kind: str | None = None,
        source_ref_match: dict[str, Any] | None = None,
        event_kinds: list[str] | None = None,
        severity_min: str = "info",
        delivery_mode: str = "immediate",
        digest_cadence: str | None = None,
        digest_timezone: str = "UTC",
        digest_anchor_time: str | None = None,
        template_key: str | None = None,
    ) -> dict[str, Any]:
        return notifications.create_notification_route(
            self,
            name=name,
            channel_id=channel_id,
            enabled=enabled,
            owner_kind=owner_kind,
            owner_ref=owner_ref,
            recipient_actor_id=recipient_actor_id,
            source_kind=source_kind,
            source_ref_match=source_ref_match,
            event_kinds=event_kinds,
            severity_min=severity_min,
            delivery_mode=delivery_mode,
            digest_cadence=digest_cadence,
            digest_timezone=digest_timezone,
            digest_anchor_time=digest_anchor_time,
            template_key=template_key,
        )

    def update_notification_route(
        self,
        route_id: int,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        owner_kind: str | None = None,
        owner_ref: str | None = None,
        recipient_actor_id: str | None = None,
        set_recipient_actor_id: bool = False,
        channel_id: int | None = None,
        source_kind: str | None = None,
        set_source_kind: bool = False,
        source_ref_match: dict[str, Any] | None = None,
        event_kinds: list[str] | None = None,
        severity_min: str | None = None,
        delivery_mode: str | None = None,
        digest_cadence: str | None = None,
        set_digest_cadence: bool = False,
        digest_timezone: str | None = None,
        digest_anchor_time: str | None = None,
        set_digest_anchor_time: bool = False,
        template_key: str | None = None,
        set_template_key: bool = False,
    ) -> dict[str, Any]:
        return notifications.update_notification_route(
            self,
            route_id,
            name=name,
            enabled=enabled,
            owner_kind=owner_kind,
            owner_ref=owner_ref,
            recipient_actor_id=recipient_actor_id,
            set_recipient_actor_id=set_recipient_actor_id,
            channel_id=channel_id,
            source_kind=source_kind,
            set_source_kind=set_source_kind,
            source_ref_match=source_ref_match,
            event_kinds=event_kinds,
            severity_min=severity_min,
            delivery_mode=delivery_mode,
            digest_cadence=digest_cadence,
            set_digest_cadence=set_digest_cadence,
            digest_timezone=digest_timezone,
            digest_anchor_time=digest_anchor_time,
            set_digest_anchor_time=set_digest_anchor_time,
            template_key=template_key,
            set_template_key=set_template_key,
        )

    def public_notification_route(self, route_id: int) -> dict[str, Any]:
        return notifications.public_notification_route(self, route_id)

    def public_notification_routes(self) -> list[dict[str, Any]]:
        return notifications.public_notification_routes(self)

    def notification_digest_run(self, digest_run_id: int) -> sqlite3.Row | None:
        return notifications.notification_digest_run(self, digest_run_id)

    def notification_digest_run_by_route_window(
        self,
        *,
        route_id: int,
        window_key: str,
    ) -> sqlite3.Row | None:
        return notifications.notification_digest_run_by_route_window(
            self, route_id=route_id, window_key=window_key
        )

    def create_notification_digest_run(
        self,
        *,
        route_id: int,
        channel_id: int,
        cadence: str,
        window_key: str,
        window_start_at: str,
        window_end_at: str,
        status: str = "composed",
        item_count: int = 0,
    ) -> dict[str, Any]:
        return notifications.create_notification_digest_run(
            self,
            route_id=route_id,
            channel_id=channel_id,
            cadence=cadence,
            window_key=window_key,
            window_start_at=window_start_at,
            window_end_at=window_end_at,
            status=status,
            item_count=item_count,
        )

    def set_notification_digest_run_result(
        self,
        digest_run_id: int,
        *,
        status: str,
        item_count: int,
        delivery_request_id: int | None = None,
    ) -> dict[str, Any]:
        return notifications.set_notification_digest_run_result(
            self,
            digest_run_id,
            status=status,
            item_count=item_count,
            delivery_request_id=delivery_request_id,
        )

    def public_notification_digest_run(self, digest_run_id: int) -> dict[str, Any]:
        return notifications.public_notification_digest_run(self, digest_run_id)

    def add_notification_digest_items(
        self,
        digest_run_id: int,
        notification_ids: list[int],
    ) -> None:
        notifications.add_notification_digest_items(
            self, digest_run_id, notification_ids
        )

    def notification_digest_items(
        self,
        digest_run_id: int,
    ) -> list[sqlite3.Row]:
        return notifications.notification_digest_items(self, digest_run_id)

    def notification_digest_item_ids(self, digest_run_id: int) -> list[int]:
        return notifications.notification_digest_item_ids(self, digest_run_id)

    def create_notification_delivery_request(
        self,
        *,
        route_id: int | None,
        channel_id: int,
        notification_id: int | None = None,
        digest_run_id: int | None = None,
        delivery_kind: str,
        dedupe_key: str,
        status: str = "queued",
        last_error: str | None = None,
    ) -> dict[str, Any]:
        return notifications.create_notification_delivery_request(
            self,
            route_id=route_id,
            channel_id=channel_id,
            notification_id=notification_id,
            digest_run_id=digest_run_id,
            delivery_kind=delivery_kind,
            dedupe_key=dedupe_key,
            status=status,
            last_error=last_error,
        )

    def notification_delivery_request(self, request_id: int) -> sqlite3.Row | None:
        return notifications.notification_delivery_request(self, request_id)

    def list_notification_delivery_requests(
        self,
        *,
        status: str | None = None,
        route_id: int | None = None,
        channel_id: int | None = None,
        notification_id: int | None = None,
        visible_route_ids: Collection[int] | None = None,
        visible_channel_ids: Collection[int] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        return notifications.list_notification_delivery_requests(
            self,
            status=status,
            route_id=route_id,
            channel_id=channel_id,
            notification_id=notification_id,
            visible_route_ids=visible_route_ids,
            visible_channel_ids=visible_channel_ids,
            offset=offset,
            limit=limit,
        )

    def notification_delivery_requests_total(
        self,
        *,
        status: str | None = None,
        route_id: int | None = None,
        channel_id: int | None = None,
        notification_id: int | None = None,
        visible_route_ids: Collection[int] | None = None,
        visible_channel_ids: Collection[int] | None = None,
    ) -> int:
        return notifications.notification_delivery_requests_total(
            self,
            status=status,
            route_id=route_id,
            channel_id=channel_id,
            notification_id=notification_id,
            visible_route_ids=visible_route_ids,
            visible_channel_ids=visible_channel_ids,
        )

    def public_notification_delivery_request(self, request_id: int) -> dict[str, Any]:
        return notifications.public_notification_delivery_request(self, request_id)

    def set_notification_delivery_request_job_id(
        self, request_id: int, job_id: int
    ) -> dict[str, Any]:
        return notifications.set_notification_delivery_request_job_id(
            self, request_id, job_id
        )

    def claim_notification_delivery_request(
        self, request_id: int, *, job_id: int | None = None
    ) -> dict[str, Any] | None:
        return notifications.claim_notification_delivery_request(
            self, request_id, job_id=job_id
        )

    def mark_notification_delivery_request_queued(
        self, request_id: int, *, last_error: str | None = None
    ) -> dict[str, Any]:
        return notifications.mark_notification_delivery_request_queued(
            self, request_id, last_error=last_error
        )

    def record_notification_delivery_request_error(
        self,
        request_id: int,
        *,
        last_error: str | None,
    ) -> dict[str, Any]:
        return notifications.record_notification_delivery_request_error(
            self, request_id, last_error=last_error
        )

    def terminalize_notification_delivery_request(
        self,
        request_id: int,
        *,
        status: str,
        last_error: str | None = None,
        provider_ref: str | None = None,
    ) -> dict[str, Any]:
        return notifications.terminalize_notification_delivery_request(
            self,
            request_id,
            status=status,
            last_error=last_error,
            provider_ref=provider_ref,
        )

    def processing_notification_delivery_requests(self) -> list[sqlite3.Row]:
        return notifications.processing_notification_delivery_requests(self)

    def record_notification_delivery_attempt(
        self,
        *,
        delivery_request_id: int | None = None,
        notification_id: int | None,
        channel_id: int,
        status: str,
        provider_ref: str | None = None,
        error: str | None = None,
        response_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return notifications.record_notification_delivery_attempt(
            self,
            delivery_request_id=delivery_request_id,
            notification_id=notification_id,
            channel_id=channel_id,
            status=status,
            provider_ref=provider_ref,
            error=error,
            response_meta=response_meta,
        )

    def notification_delivery_attempt(self, attempt_id: int) -> dict[str, Any]:
        return notifications.notification_delivery_attempt(self, attempt_id)

    # ---------- export / import ----------

    def export(
        self,
        target_zip: str | Path,
        include_media: bool = True,
        *,
        include_traces: bool = False,
    ) -> Path:
        return bundle_io.export(
            self,
            target_zip,
            include_media,
            include_traces=include_traces,
        )

    def export_database(self, target_db: str | Path) -> Path:
        return bundle_io.export_database(self, target_db)

    def compact(self, vacuum: bool = True, *, force: bool = False) -> dict[str, Any]:
        return bundle_io.compact(self, vacuum, force=force)

    @classmethod
    def import_bundle(cls, source_zip: str | Path, target_dir: str | Path) -> "Project":
        return bundle_io.import_bundle(cls, source_zip, target_dir)

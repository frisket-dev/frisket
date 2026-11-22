"""Project store: a ``.frisket`` directory bundle.

Layout:
    myproject.frisket/
      manifest.json
      project.db          (irreplaceable data only)
      project.retrieval.db (sidecar: lexical + vector retrieval, rebuildable)
      project.cache.db    (sidecar: response cache, rebuildable)
      blobs/ab/abcd1234...

Current cells resolve the latest applied Change at each coordinate. Source
versions, results and edits keep immutable producer references; pending run
scopes mask earlier values. Cached heads are disposable and dirty scopes read
canonical winners until bounded repair completes.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import uuid
import weakref
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator, overload


from . import (
    bundle_io,
    bundle_open,
    cells,
    credentials,
    notifications,
    op_log,
    project_blobs,
    project_meta,
    sheet_lifecycle,
    alerts,
)
from . import views as _views_store
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
    SUPPORTED_RETENTION_VALUES as SUPPORTED_RETENTION_VALUES,
    _normalize_retention_policy,
)
from .blob_backend import (
    FilesystemProjectBlobStore,
    ProjectBlobStore,
)
from .transactions import (
    StatementScopedConnection,
    require_transaction,
    track_for_unit_of_work,
    write_transaction,
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

    is_read_snapshot = True

    def __init__(self, owner: Project):
        self.path = owner.path
        self.db_path = owner.db_path
        self.blob_store = owner.blob_store
        self._close_lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._read_resources = ExitStack()

        # uri=True lets later ATTACH statements use file: URIs (the retrieval
        # reader attaches its index with ?mode=ro); SQLite builds without
        # SQLITE_USE_URI otherwise treat the URI as a literal filename.
        connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
            uri=True,
        )
        try:
            connection.row_factory = sqlite3.Row
            from .current_cells import register_current_cell_functions

            register_current_cell_functions(connection)
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

    def read_snapshot(self) -> AbstractContextManager[ProjectReadSnapshot]:
        """Borrow this same read scope without closing its owner's connection."""
        self.db  # Refuse borrowing an already closed snapshot.
        return nullcontext(self)

    def materialize_blob(self, digest: str) -> AbstractContextManager[Path]:
        """Materialize a content-addressed blob for this read snapshot."""
        self.db  # Refuse blob access after the snapshot lifetime ends.
        return self.blob_store.materialize(digest)

    # ---------- cells: read-only sheet, column and value reads ----------
    pending_replay_values = cells.pending_replay_values
    pending_replay_count = cells.pending_replay_count
    columns = cells.columns
    sheets = cells.sheets
    row_count = cells.row_count
    get_values_with_refs = cells.get_values_with_refs
    get_values = cells.get_values
    visible_row_ids = cells.visible_row_ids

    @property
    def op_cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='op_cursor'").fetchone()
        return int(row["value"] if row is not None else "0")

    def temporary_directory(self, *, prefix: str = "frisket-read-") -> Path:
        """Retain attached temporary databases until this connection is closed."""
        self.db  # Refuse resources after the snapshot lifetime ends.
        return Path(
            self._read_resources.enter_context(
                tempfile.TemporaryDirectory(prefix=prefix)
            )
        )

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
            finally:
                self._read_resources.close()

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
        maintenance: bool = False,
    ):
        self.path = Path(bundle_path)
        self.db_path = self.path / "project.db"
        self.maintenance = bool(maintenance)
        self.blob_store: ProjectBlobStore = (
            blob_store
            if blob_store is not None
            else FilesystemProjectBlobStore.for_bundle(self.path)
        )
        if not self.db_path.exists():
            raise FileNotFoundError(f"not a frisket bundle: {self.path}")
        self._local = threading.local()
        self._purge_result_delete_scope = threading.local()
        self._connections: weakref.WeakSet[_ThreadConnection] = weakref.WeakSet()
        self._connections_lock = threading.Lock()
        self._closed = False
        _ = self.db  # open the creating thread's connection eagerly
        try:
            bundle_open.open_bundle(self, maintenance=maintenance)
        except BaseException:
            self.close()
            raise

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
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                factory=StatementScopedConnection,
            )
            conn.row_factory = sqlite3.Row
            from .current_cells import register_current_cell_functions

            register_current_cell_functions(conn)
            purge_delete_scope = self._purge_result_delete_scope
            maintenance = self.maintenance

            def purge_result_delete_authorized(run_id, row_id, column_id):
                scope = getattr(purge_delete_scope, "keys", None)
                return int(
                    maintenance
                    and scope is not None
                    and (int(run_id), int(row_id), int(column_id)) in scope
                )

            conn.create_function(
                "frisket_purge_result_delete_authorized",
                3,
                purge_result_delete_authorized,
            )
            conn.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; "
                "PRAGMA busy_timeout=10000;"
            )
            track_for_unit_of_work(conn)
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
        # journal_mode cannot change inside a transaction, so the schema's own
        # leading pragmas are applied first; inside the BEGIN they are no-ops.
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        # The DDL replay and the schema stamp below commit as ONE transaction
        # (executescript leaves the BEGIN open), so a bundle can never exist
        # stamped-but-unbuilt or built-but-unstamped: either state would be
        # read by the open fence as somebody else's bundle. Without the
        # explicit BEGIN every CREATE autocommits and syncs on its own.
        db.executescript("BEGIN;\n" + SCHEMA)
        db.execute(
            "INSERT INTO meta (key, value) VALUES "
            "('format_version', ?), ('name', ?), ('op_cursor', '0'), "
            "('sensitive', ?), ('project_kind', 'regular'), (?, ?), (?, ?), (?, ?)",
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

    @overload
    def get_meta(self, key: str, default: str) -> str: ...

    @overload
    def get_meta(self, key: str, default: None = None) -> str | None: ...

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta_in_transaction(self, key: str, value: str) -> None:
        require_transaction(self.db, "project meta")
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def set_meta(self, key: str, value: str) -> None:
        with write_transaction(self.db, what="project meta"):
            self.set_meta_in_transaction(key, value)

    @contextmanager
    def transaction_scope(self) -> Iterator[None]:
        """Serialize one project read-modify-write operation.

        Nested callers keep ownership of their existing transaction.  The
        outermost scope takes SQLite's writer lock before reading, so two
        settings mutations cannot both merge from the same stale document.
        """

        db = self.db
        with write_transaction(db, commit=not db.in_transaction):
            yield

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

    # ---------- project_meta: manifest, metadata, retention, network policy ----------
    _write_manifest = project_meta._write_manifest
    refresh_pending_review_summary = project_meta.refresh_pending_review_summary
    project_metadata = project_meta.project_metadata
    set_project_metadata = project_meta.set_project_metadata
    set_project_sensitivity = project_meta.set_project_sensitivity
    _ensure_retention_policy = project_meta._ensure_retention_policy
    retention_policy = project_meta.retention_policy
    set_retention_policy = project_meta.set_retention_policy
    network_policy = project_meta.network_policy
    set_network_policy = project_meta.set_network_policy
    effective_network_policy = project_meta.effective_network_policy

    # ---------- credentials: provider keys / secrets ----------
    provider_key_catalog_rows = credentials.provider_key_catalog_rows
    set_provider_key = credentials.set_provider_key
    delete_provider_key = credentials.delete_provider_key
    provider_spend_state = credentials.provider_spend_state
    accrue_provider_spend = credentials.accrue_provider_spend
    provider_model_keys = credentials.provider_model_keys
    secret_rows = credentials.secret_rows
    secret_migration_conflict_rows = credentials.secret_migration_conflict_rows
    secret_consumer_rows = credentials.secret_consumer_rows
    set_secret = credentials.set_secret
    add_secret_consumer = credentials.add_secret_consumer
    secret_consumer_is_declared = credentials.secret_consumer_is_declared
    secret_plaintext = credentials.secret_plaintext
    delete_secret = credentials.delete_secret

    # ---------- cells: sheets / columns / rows / values ----------
    add_sheet = cells.add_sheet
    add_sheet_in_transaction = cells.add_sheet_in_transaction
    sheets = cells.sheets
    set_sheet_title_column = cells.set_sheet_title_column
    add_column = cells.add_column
    add_column_in_transaction = cells.add_column_in_transaction
    columns = cells.columns
    get_column = cells.get_column
    set_column_default_hidden = cells.set_column_default_hidden
    set_column_type = cells.set_column_type
    set_column_format = cells.set_column_format
    set_column_semantic_type = cells.set_column_semantic_type
    add_rows = cells.add_rows
    add_rows_in_transaction = cells.add_rows_in_transaction
    add_row_with_undo = cells.add_row_with_undo
    row_count = cells.row_count
    visible_row_ids = cells.visible_row_ids
    get_values_with_refs = cells.get_values_with_refs
    get_values = cells.get_values
    apply_edits = cells.apply_edits
    pending_replay_values = cells.pending_replay_values
    pending_replay_count = cells.pending_replay_count

    # ---------- sheet_lifecycle: sheet deletion ----------
    dependent_sheets = sheet_lifecycle.dependent_sheets
    delete_sheet = sheet_lifecycle.delete_sheet

    # ---------- op_log: operation history, undo / redo ----------

    def append_change(
        self,
        kind: str,
        spec: dict[str, Any] | None = None,
        label: str | None = None,
        barrier: bool = False,
    ) -> int:
        if barrier:
            with write_transaction(self.db, what="change append"):
                return op_log.append_change_in_transaction(
                    self, kind, spec, label, barrier=True
                )
        return op_log.append_change(self, kind, spec, label)

    def append_change_in_transaction(
        self,
        kind: str,
        spec: dict[str, Any] | None = None,
        label: str | None = None,
        barrier: bool = False,
    ) -> int:
        return op_log.append_change_in_transaction(self, kind, spec, label, barrier)

    set_undo_info = op_log.set_undo_info
    history = op_log.history
    history_page = op_log.history_page
    history_total = op_log.history_total
    history_cursor_index = op_log.history_cursor_index
    undo = op_log.undo
    redo = op_log.redo
    _unapply = op_log._unapply
    _reapply = op_log._reapply
    changes_meta = op_log.changes_meta

    @property
    def op_cursor(self) -> int:
        return int(self.get_meta("op_cursor", "0"))

    # ---------- project_blobs: content-addressed blobs ----------

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
    ) -> str:
        self._assert_blob_write_open()
        return project_blobs.add_blob(self, data, filename, mime, source_url, metadata)

    def add_blob_from_path(
        self,
        path: str | Path,
        filename: str | None = None,
        mime: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
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
            expected_digest=expected_digest,
        )

    record_blob_derivation = project_blobs.record_blob_derivation
    blob_derivation = project_blobs.blob_derivation
    blob_derivation_chain = project_blobs.blob_derivation_chain
    _referenced_blob_hashes = project_blobs._referenced_blob_hashes
    gc_blobs = project_blobs.gc_blobs

    def materialize_blob(self, digest: str) -> AbstractContextManager[Path]:
        return self.blob_store.materialize(digest)

    def read_blob(self, digest: str) -> bytes:
        with self.materialize_blob(digest) as path:
            return Path(path).read_bytes()

    # ---------- views: filter/flag views ----------
    views = _views_store.views
    get_view = _views_store.get_view
    add_view = _views_store.add_view
    update_view = _views_store.update_view
    delete_view = _views_store.delete_view

    # ---------- alerts: alerts ----------
    get_alert = alerts.get_alert
    add_alert = alerts.add_alert
    update_alert = alerts.update_alert
    delete_alert = alerts.delete_alert
    alert_latest_run = alerts.alert_latest_run
    get_alert_check = alerts.get_alert_check
    alert_checks_page = alerts.alert_checks_page
    alert_checks_total = alerts.alert_checks_total
    alert_check_matches = alerts.alert_check_matches
    alert_check_events = alerts.alert_check_events
    alert_check_events_total = alerts.alert_check_events_total
    alert_events_for_notification = alerts.alert_events_for_notification
    record_alert_check = alerts.record_alert_check
    # Bound last: this rebinds ``alerts`` inside the class body.
    alerts = alerts.alerts

    # ---------- notifications ----------
    notification_item_by_dedupe_key = notifications.notification_item_by_dedupe_key
    notification_item = notifications.notification_item
    insert_notification_item = notifications.insert_notification_item
    update_notification_item = notifications.update_notification_item
    list_notification_items = notifications.list_notification_items
    notification_items_total = notifications.notification_items_total
    notification_summary = notifications.notification_summary
    ensure_notification_actor_state = notifications.ensure_notification_actor_state
    notification_actor_state = notifications.notification_actor_state
    mark_notifications_seen = notifications.mark_notifications_seen
    mark_notification_read = notifications.mark_notification_read
    acknowledge_notification = notifications.acknowledge_notification
    acknowledge_notifications = notifications.acknowledge_notifications
    unacknowledge_notification = notifications.unacknowledge_notification
    create_notification_channel = notifications.create_notification_channel
    ensure_default_in_app_notification_channel = (
        notifications.ensure_default_in_app_notification_channel
    )
    notification_channel = notifications.notification_channel
    notification_channels = notifications.notification_channels
    update_notification_channel = notifications.update_notification_channel
    public_notification_channel = notifications.public_notification_channel
    notification_route = notifications.notification_route
    notification_routes = notifications.notification_routes
    create_notification_route = notifications.create_notification_route
    update_notification_route = notifications.update_notification_route
    public_notification_route = notifications.public_notification_route
    notification_digest_run = notifications.notification_digest_run
    notification_digest_run_by_route_window = (
        notifications.notification_digest_run_by_route_window
    )
    create_notification_digest_run = notifications.create_notification_digest_run
    set_notification_digest_run_result = (
        notifications.set_notification_digest_run_result
    )
    public_notification_digest_run = notifications.public_notification_digest_run
    add_notification_digest_items = notifications.add_notification_digest_items
    notification_digest_items = notifications.notification_digest_items
    notification_digest_item_ids = notifications.notification_digest_item_ids
    create_notification_delivery_request = (
        notifications.create_notification_delivery_request
    )
    notification_delivery_request = notifications.notification_delivery_request
    list_notification_delivery_requests = (
        notifications.list_notification_delivery_requests
    )
    notification_delivery_requests_total = (
        notifications.notification_delivery_requests_total
    )
    public_notification_delivery_request = (
        notifications.public_notification_delivery_request
    )
    set_notification_delivery_request_job_id = (
        notifications.set_notification_delivery_request_job_id
    )
    claim_notification_delivery_request = (
        notifications.claim_notification_delivery_request
    )
    mark_notification_delivery_request_queued = (
        notifications.mark_notification_delivery_request_queued
    )
    record_notification_delivery_request_error = (
        notifications.record_notification_delivery_request_error
    )
    terminalize_notification_delivery_request = (
        notifications.terminalize_notification_delivery_request
    )
    processing_notification_delivery_requests = (
        notifications.processing_notification_delivery_requests
    )
    record_notification_delivery_attempt = (
        notifications.record_notification_delivery_attempt
    )
    notification_delivery_attempt = notifications.notification_delivery_attempt

    # ---------- bundle_io: export / import ----------
    export = bundle_io.export
    export_database = bundle_io.export_database
    compact = bundle_io.compact

    @classmethod
    def import_bundle(
        cls, source_archive: str | Path, target_dir: str | Path
    ) -> "Project":
        """Restore a tar.gz backup (or an older ZIP) into a new project directory."""
        return bundle_io.import_bundle(cls, source_archive, target_dir)

"""SQLite repository for Frisket's rebuildable retrieval sidecar."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from filelock import FileLock, Timeout as FileLockTimeout

from .contracts import SourceLocator, SourceRevision
from .native import validate_vector_blob
from .sources import canonical_json, input_hash


RETRIEVAL_DB_NAME = "project.retrieval.db"
# Version 3 changes source authority: prepared OCR text supersedes its evidence
# copy, and producer-owned OCR geometry is metadata rather than searchable JSON.
# The sidecar is disposable, so an incompatible derivation is preserved and
# rebuilt through the existing recovery path instead of migrated in place.
SCHEMA_VERSION = 3
SIDECAR_ID_META_KEY = "sidecar_id"
PROJECT_SIDECAR_META_KEY = "retrieval_sidecar_id"
PAGE_SIZE = 16 * 1024
CACHE_KIB = 32 * 1024
MMAP_SIZE = 1024 * 1024 * 1024

PreparationStatus = Literal["pending", "preparing", "ready", "unavailable", "failed"]

# These failures become retryable only when authoritative dirty/evidence/policy
# dispatch registers the same target again. The worker never requeues them
# itself, so a broken input cannot create a hot retry loop.
_EVENT_RETRYABLE_FAILURES = frozenset(
    {
        "blob_integrity_failed",
        "encoding_failed",
        "media_decode_failed",
        "missing_blob",
        "missing_media_tool",
        "pdf_render_failed",
        "stale_source",
    }
)


@dataclass(frozen=True)
class StoredPart:
    part_id: str
    kind: str
    locator: SourceLocator
    group_key: str | None = None


@dataclass(frozen=True)
class BindingDependency:
    row_id: int
    column_id: int
    retrieval_revision: int


@dataclass(frozen=True)
class StoredBinding:
    binding_id: str
    sheet_id: int
    row_id: int
    column_id: int | None = None
    dependencies: tuple[BindingDependency, ...] = ()


@dataclass(frozen=True)
class CachedRepresentation:
    representation_id: str
    space_id: str
    input_hash: str
    vector: bytes
    dimension: int
    precision: str = "float32"


@dataclass(frozen=True)
class PartRepresentation:
    part_id: str
    representation_id: str
    channel: str


@dataclass(frozen=True)
class LexicalInput:
    text: str
    locator: SourceLocator


@dataclass(frozen=True)
class NamedDatasetMember:
    dataset_id: str
    part_id: str
    representation_id: str


@dataclass(frozen=True)
class NamedDatasetVector:
    part_id: str
    channel: str
    representation: CachedRepresentation


@dataclass(frozen=True)
class SourcePublication:
    source_revision: SourceRevision
    recipe_id: str
    channel: str
    parts: tuple[StoredPart, ...]
    bindings: tuple[StoredBinding, ...]
    representations: tuple[CachedRepresentation, ...] = ()
    attachments: tuple[PartRepresentation, ...] = ()
    lexical: LexicalInput | None = None
    named_memberships: tuple[NamedDatasetMember, ...] = ()
    complete: bool = True
    space_id: str | None = None


@dataclass(frozen=True)
class PreparationState:
    source_id: str
    recipe_id: str
    channel: str
    space_id: str | None
    target_version_kind: str
    target_version_value: int | str
    status: PreparationStatus
    prepared_parts: int
    error_code: str | None
    error_message: str | None
    cursor_json: str | None = None


@dataclass(frozen=True)
class PreparationWork:
    source_revision: SourceRevision
    recipe_id: str
    channel: str
    space_id: str | None
    status: Literal["pending", "preparing"]
    prepared_parts: int
    cursor_json: str | None


class StalePublicationError(RuntimeError):
    """Prepared work no longer targets the authority revision being published."""


class UnsupportedRetrievalSchema(RuntimeError):
    """The derived retrieval sidecar cannot be read by this build."""


@contextmanager
def _initialization_lock(path: Path, *, busy_timeout_ms: int) -> Iterator[None]:
    """Serialize cold WAL negotiation and schema setup across openers."""

    lock_path = path.with_name(f"{path.name}.init.lock")
    try:
        with FileLock(str(lock_path), timeout=busy_timeout_ms / 1000):
            yield
    except FileLockTimeout as exc:
        raise sqlite3.OperationalError("database is locked") from exc


class RetrievalStore:
    """Single-writer repository for one project's derived retrieval records."""

    def __init__(self, project_or_path: Any):
        project_path = getattr(project_or_path, "path", project_or_path)
        path = Path(project_path)
        self.path = path if path.name == RETRIEVAL_DB_NAME else path / RETRIEVAL_DB_NAME
        self._connection: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("retrieval store is not open")
        return self._connection

    def open(self, *, busy_timeout_ms: int = 10_000) -> RetrievalStore:
        if self._connection is not None:
            return self
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError("busy_timeout_ms must be a non-negative integer")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(
            self.path, isolation_level=None, timeout=busy_timeout_ms / 1000
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
            db.execute(f"PRAGMA mmap_size={MMAP_SIZE}")
            self._connection = db
            mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal" or self._schema_needs_write():
                with _initialization_lock(self.path, busy_timeout_ms=busy_timeout_ms):
                    mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                    if mode != "wal":
                        creating = (
                            db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
                            is None
                        )
                        if creating:
                            db.execute(f"PRAGMA page_size={PAGE_SIZE}")
                        db.execute("PRAGMA journal_mode=WAL")
                    self.ensure_schema()
        except BaseException:
            self._connection = None
            db.close()
            raise
        return self

    def close(self) -> None:
        db, self._connection = self._connection, None
        if db is not None:
            db.close()

    def __enter__(self) -> RetrievalStore:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        """Create the current sidecar without storing authoritative source text."""

        if not self._schema_needs_write():
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # A concurrent creator may have completed while BEGIN IMMEDIATE waited.
            if not self._schema_needs_write():
                self.db.commit()
                return

            for statement in _schema_statements(_SCHEMA):
                self.db.execute(statement)
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS preparation_space_counts "
                "ON preparation(recipe_id,channel,space_id,status)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS preparation_attention "
                "ON preparation(space_id,channel,status) WHERE status!='ready'"
            )
            row = self.db.execute(
                "SELECT value FROM retrieval_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO retrieval_meta(key,value) VALUES ('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
            sidecar = self.db.execute(
                "SELECT value FROM retrieval_meta WHERE key=?",
                (SIDECAR_ID_META_KEY,),
            ).fetchone()
            try:
                sidecar_id = (
                    None if sidecar is None else str(uuid.UUID(str(sidecar[0])))
                )
            except (TypeError, ValueError, AttributeError):
                sidecar_id = None
            if sidecar_id is None:
                generated = str(uuid.uuid4())
                if sidecar is None:
                    self.db.execute(
                        "INSERT INTO retrieval_meta(key,value) VALUES (?,?)",
                        (SIDECAR_ID_META_KEY, generated),
                    )
                else:
                    self.db.execute(
                        "UPDATE retrieval_meta SET value=? WHERE key=?",
                        (generated, SIDECAR_ID_META_KEY),
                    )
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def _schema_needs_write(self) -> bool:
        tables = {
            str(row[0])
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "retrieval_meta" not in tables:
            if tables:
                raise UnsupportedRetrievalSchema(
                    "unsupported retrieval schema: schema version is missing"
                )
            return True

        row = self.db.execute(
            "SELECT value FROM retrieval_meta WHERE key='schema_version'"
        ).fetchone()
        try:
            version = None if row is None else int(row[0])
        except (TypeError, ValueError):
            version = None
        if version != SCHEMA_VERSION:
            raise UnsupportedRetrievalSchema(
                f"unsupported retrieval schema version "
                f"{None if row is None else row[0]!r}; expected {SCHEMA_VERSION}"
            )

        sidecar = self.db.execute(
            "SELECT value FROM retrieval_meta WHERE key=?", (SIDECAR_ID_META_KEY,)
        ).fetchone()
        try:
            if sidecar is None:
                return True
            uuid.UUID(str(sidecar[0]))
        except (TypeError, ValueError, AttributeError):
            return True

        required_indexes = {"preparation_space_counts", "preparation_attention"}
        present_indexes = {
            str(index[0])
            for index in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('preparation_space_counts','preparation_attention')"
            )
        }
        return present_indexes != required_indexes

    def sidecar_id(self) -> str:
        """Return this disposable sidecar incarnation's stable UUID."""

        row = self.db.execute(
            "SELECT value FROM retrieval_meta WHERE key=?", (SIDECAR_ID_META_KEY,)
        ).fetchone()
        if row is None:
            raise RuntimeError("retrieval sidecar identity is missing")
        try:
            return str(uuid.UUID(str(row[0])))
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError("retrieval sidecar identity is invalid") from exc

    def begin_preparation(
        self,
        source_revision: SourceRevision,
        *,
        recipe_id: str,
        channel: str,
        space_id: str | None = None,
    ) -> None:
        """Durably target one source/channel revision before encoding starts."""

        source_id, _kind, version_kind, version_value, _hash = _revision_fields(
            source_revision
        )
        _require_nonempty(recipe_id, "recipe_id")
        _require_nonempty(channel, "channel")
        _validate_space_id(space_id)
        encoded, value_type = _encode_version(version_value)
        self.db.execute(
            "INSERT INTO preparation("
            "source_id,recipe_id,channel,space_id,target_version_kind,target_version_value,"
            "target_version_type,status,prepared_parts,error_code,error_message,"
            "cursor_json) VALUES (?,?,?,?,?,?,?,'preparing',0,NULL,NULL,NULL) "
            "ON CONFLICT(source_id,recipe_id,channel) DO UPDATE SET "
            "prepared_parts=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type THEN preparation.prepared_parts ELSE 0 END,"
            "cursor_json=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type THEN preparation.cursor_json ELSE NULL END,"
            "space_id=excluded.space_id,target_version_kind=excluded.target_version_kind,"
            "target_version_value=excluded.target_version_value,"
            "target_version_type=excluded.target_version_type,status='preparing',"
            "error_code=NULL,error_message=NULL",
            (
                source_id,
                recipe_id,
                channel,
                space_id,
                version_kind,
                encoded,
                value_type,
            ),
        )

    def queue_preparation(
        self,
        source_revision: SourceRevision,
        *,
        recipe_id: str,
        channel: str,
        space_id: str | None = None,
    ) -> None:
        """Queue a target, preserving same-revision progress and terminal state."""

        source_id, _kind, version_kind, version_value, _hash = _revision_fields(
            source_revision
        )
        _require_nonempty(recipe_id, "recipe_id")
        _require_nonempty(channel, "channel")
        _validate_space_id(space_id)
        encoded, value_type = _encode_version(version_value)
        retryable = ",".join(f"'{code}'" for code in sorted(_EVENT_RETRYABLE_FAILURES))
        preserve_terminal = (
            "preparation.status IN ('ready','unavailable') OR "
            "(preparation.status='failed' AND "
            f"(preparation.error_code IS NULL OR preparation.error_code NOT IN ({retryable})))"
        )
        self.db.execute(
            "INSERT INTO preparation("
            "source_id,recipe_id,channel,space_id,target_version_kind,target_version_value,"
            "target_version_type,status,prepared_parts,error_code,error_message,"
            "cursor_json) VALUES (?,?,?,?,?,?,?,'pending',0,NULL,NULL,NULL) "
            "ON CONFLICT(source_id,recipe_id,channel) DO UPDATE SET "
            "status=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type AND ("
            + preserve_terminal
            + ") THEN preparation.status ELSE 'pending' END,"
            "prepared_parts=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type THEN preparation.prepared_parts ELSE 0 END,"
            "cursor_json=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type THEN preparation.cursor_json ELSE NULL END,"
            "space_id=excluded.space_id,target_version_kind=excluded.target_version_kind,"
            "target_version_value=excluded.target_version_value,"
            "target_version_type=excluded.target_version_type,"
            "error_code=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type AND ("
            + preserve_terminal
            + ") THEN preparation.error_code ELSE NULL END,"
            "error_message=CASE WHEN preparation.space_id IS excluded.space_id "
            "AND preparation.target_version_kind="
            "excluded.target_version_kind AND preparation.target_version_value="
            "excluded.target_version_value AND preparation.target_version_type="
            "excluded.target_version_type AND ("
            + preserve_terminal
            + ") THEN preparation.error_message ELSE NULL END",
            (
                source_id,
                recipe_id,
                channel,
                space_id,
                version_kind,
                encoded,
                value_type,
            ),
        )

    def register_preparation(
        self,
        source_revision: SourceRevision,
        bindings: Sequence[StoredBinding],
        *,
        recipe_id: str,
        channel: str,
        space_id: str | None = None,
        replace_channel_recipes: bool = False,
    ) -> None:
        """Atomically make a queued source visible to scoped coverage.

        Registration precedes optional encoding and lexical publication.  This
        keeps pending, unavailable, and failed work attached to an authoritative
        binding even when a worker stops before publishing its first part.
        """

        source_id, source_kind, version_kind, version_value, content_hash = (
            _revision_fields(source_revision)
        )
        if not bindings:
            raise ValueError("registered preparation requires at least one binding")
        _validate_bindings(bindings)
        _require_nonempty(recipe_id, "recipe_id")
        _require_nonempty(channel, "channel")
        _validate_space_id(space_id)
        encoded_version, version_type = _encode_version(version_value)
        source_key = (source_id, version_kind, encoded_version, version_type)
        with self._write_transaction():
            if replace_channel_recipes:
                old_recipes = tuple(
                    str(row[0])
                    for row in self.db.execute(
                        "SELECT recipe_id FROM preparation "
                        "WHERE source_id=? AND channel=? AND recipe_id<>?",
                        (source_id, channel, recipe_id),
                    )
                )
                if old_recipes:
                    placeholders = ",".join("?" for _ in old_recipes)
                    self.db.execute(
                        "DELETE FROM parts WHERE source_key IN ("
                        "SELECT source_key FROM sources WHERE source_id=?"
                        ") AND publication_channel=? "
                        f"AND recipe_id IN ({placeholders})",
                        (source_id, channel, *old_recipes),
                    )
                    self.db.execute(
                        "DELETE FROM preparation WHERE source_id=? AND channel=? "
                        f"AND recipe_id IN ({placeholders})",
                        (source_id, channel, *old_recipes),
                    )
            self.db.execute(
                "INSERT INTO sources("
                "source_id,source_kind,version_kind,version_value,version_type,"
                "content_hash) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(source_id,version_kind,version_value,version_type) "
                "DO UPDATE SET source_kind=excluded.source_kind,"
                "content_hash=excluded.content_hash",
                (
                    source_id,
                    source_kind,
                    version_kind,
                    encoded_version,
                    version_type,
                    content_hash,
                ),
            )
            self._upsert_binding_records(bindings, self._source_key(source_key))
            self.queue_preparation(
                source_revision,
                recipe_id=recipe_id,
                channel=channel,
                space_id=space_id,
            )

    def next_preparation(
        self, *, channel: str | None = None, space_id: str | None = None
    ) -> PreparationWork | None:
        """Read the next resumable target without claiming a second work queue."""

        _validate_space_id(space_id)
        params: tuple[str, ...] = () if channel is None else (channel,)
        channel_sql = "" if channel is None else "AND work.channel=? "
        space_sql = "" if space_id is None else "AND work.space_id IS ? "
        if space_id is not None:
            params = (*params, space_id)
        row = self.db.execute(
            "SELECT work.*,source.source_kind,source.content_hash "
            "FROM preparation work JOIN sources source "
            "ON source.source_id=work.source_id "
            "AND source.version_kind=work.target_version_kind "
            "AND source.version_value=work.target_version_value "
            "AND source.version_type=work.target_version_type "
            "WHERE work.status IN ('pending','preparing') "
            + channel_sql
            + space_sql
            + "ORDER BY CASE work.status WHEN 'preparing' THEN 0 ELSE 1 END,"
            "work.source_id,work.recipe_id,work.channel LIMIT 1",
            params,
        ).fetchone()
        return None if row is None else _preparation_work(row)

    def advance_preparation(
        self,
        source_revision: SourceRevision,
        *,
        recipe_id: str,
        channel: str,
        space_id: str | None = None,
        cursor_json: str,
    ) -> bool:
        """Persist one producer cursor only for the exact active target."""

        if not isinstance(cursor_json, str) or not cursor_json.strip():
            raise ValueError("cursor_json must be non-empty")
        try:
            json.loads(cursor_json)
        except json.JSONDecodeError as exc:
            raise ValueError("cursor_json must be valid JSON") from exc
        source_id, _kind, version_kind, version_value, _hash = _revision_fields(
            source_revision
        )
        _validate_space_id(space_id)
        encoded, value_type = _encode_version(version_value)
        changed = self.db.execute(
            "UPDATE preparation SET cursor_json=?,status='preparing',"
            "error_code=NULL,error_message=NULL WHERE source_id=? AND recipe_id=? "
            "AND channel=? AND space_id IS ? AND target_version_kind=? "
            "AND target_version_value=? "
            "AND target_version_type=? AND status IN ('pending','preparing')",
            (
                cursor_json,
                source_id,
                recipe_id,
                channel,
                space_id,
                version_kind,
                encoded,
                value_type,
            ),
        )
        return changed.rowcount == 1

    def mark_preparation_unavailable(
        self,
        source_revision: SourceRevision,
        *,
        recipe_id: str,
        channel: str,
        space_id: str | None = None,
        error_code: str = "encoder_unavailable",
        reason: str,
    ) -> bool:
        """Record why an exact target cannot run in the current configuration."""

        _require_nonempty(error_code, "error_code")
        _require_nonempty(reason, "reason")
        source_id, _kind, version_kind, version_value, _hash = _revision_fields(
            source_revision
        )
        _validate_space_id(space_id)
        encoded, value_type = _encode_version(version_value)
        changed = self.db.execute(
            "UPDATE preparation SET status='unavailable',"
            "error_code=?,error_message=? "
            "WHERE source_id=? AND recipe_id=? AND channel=? AND space_id IS ? "
            "AND target_version_kind=? AND target_version_value=? "
            "AND target_version_type=? AND status IN ('pending','preparing')",
            (
                error_code,
                reason,
                source_id,
                recipe_id,
                channel,
                space_id,
                version_kind,
                encoded,
                value_type,
            ),
        )
        return changed.rowcount == 1

    def mark_pending_encoder_unavailable(
        self, *, space_id: str, channels: tuple[str, ...]
    ) -> int:
        """Defer unstarted work for one missing encoder without stealing a pass.

        Preparing targets may belong to another claim. Keep their status and
        cursor, along with ready/failed and other encoder/channel state.
        """
        _validate_space_id(space_id)
        if not channels:
            return 0
        placeholders = ",".join("?" for _ in channels)
        changed = self.db.execute(
            "UPDATE preparation SET status='unavailable',"
            "error_code='encoder_unavailable',"
            "error_message='the configured local retrieval encoder is unavailable' "
            "WHERE status='pending' AND space_id=? "
            f"AND channel IN ({placeholders})",
            (space_id, *channels),
        )
        return changed.rowcount

    def requeue_unavailable(
        self,
        *,
        error_code: str = "encoder_unavailable",
        channel: str | None = None,
        space_id: str | None = None,
    ) -> int:
        """Retry matching unavailable work after its prerequisite becomes ready."""

        _require_nonempty(error_code, "error_code")
        clauses = ["status='unavailable'", "error_code=?"]
        values: list[object] = [error_code]
        if channel is not None:
            _require_nonempty(channel, "channel")
            clauses.append("channel=?")
            values.append(channel)
        if space_id is not None:
            _validate_space_id(space_id)
            clauses.append("space_id=?")
            values.append(space_id)
        changed = self.db.execute(
            "UPDATE preparation SET status='pending',error_code=NULL,"
            "error_message=NULL WHERE " + " AND ".join(clauses),
            values,
        )
        return changed.rowcount

    def fail_preparation(
        self,
        source_revision: SourceRevision,
        *,
        recipe_id: str,
        channel: str,
        space_id: str | None = None,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Mark failure only when the worker still owns the targeted revision."""

        source_id, _kind, version_kind, version_value, _hash = _revision_fields(
            source_revision
        )
        _validate_space_id(space_id)
        encoded, value_type = _encode_version(version_value)
        cursor = self.db.execute(
            "UPDATE preparation SET status='failed',error_code=?,error_message=? "
            "WHERE source_id=? AND recipe_id=? AND channel=? AND space_id IS ? "
            "AND target_version_kind=? AND target_version_value=? "
            "AND target_version_type=?",
            (
                error_code,
                error_message,
                source_id,
                recipe_id,
                channel,
                space_id,
                version_kind,
                encoded,
                value_type,
            ),
        )
        return cursor.rowcount == 1

    def publish_source(self, publication: SourcePublication) -> bool:
        """Append one bounded source/recipe/channel batch conditionally.

        The method returns ``False`` when newer preparation superseded this worker.
        No part of a stale batch is written.  Vector encoding and lexical text
        production happen before entering this transaction.
        """

        _validate_publication(publication)
        source_id, source_kind, version_kind, version_value, content_hash = (
            _revision_fields(publication.source_revision)
        )
        encoded_version, version_type = _encode_version(version_value)
        with self._write_transaction():
            owned = self.db.execute(
                "SELECT 1 FROM preparation WHERE source_id=? AND recipe_id=? "
                "AND channel=? AND space_id IS ? AND target_version_kind=? "
                "AND target_version_value=? AND target_version_type=? "
                "AND status IN ('pending','preparing')",
                (
                    source_id,
                    publication.recipe_id,
                    publication.channel,
                    publication.space_id,
                    version_kind,
                    encoded_version,
                    version_type,
                ),
            ).fetchone()
            if owned is None:
                return False

            self.db.execute(
                "INSERT INTO sources("
                "source_id,source_kind,version_kind,version_value,version_type,"
                "content_hash) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(source_id,version_kind,version_value,version_type) "
                "DO UPDATE SET source_kind=excluded.source_kind,"
                "content_hash=excluded.content_hash",
                (
                    source_id,
                    source_kind,
                    version_kind,
                    encoded_version,
                    version_type,
                    content_hash,
                ),
            )
            source_identity = (
                source_id,
                version_kind,
                encoded_version,
                version_type,
            )
            source_key = self._source_key(source_identity)
            self._upsert_parts(publication, source_key)
            self._upsert_bindings(publication, source_key)
            self._upsert_representations(publication.representations)
            self._attach_representations(publication.attachments)
            self._replace_lexical(publication, source_key)
            self._upsert_memberships(publication)
            prepared_parts = int(
                self.db.execute(
                    "SELECT count(*) FROM parts WHERE source_key=? AND recipe_id=? "
                    "AND publication_channel=?",
                    (source_key, publication.recipe_id, publication.channel),
                ).fetchone()[0]
            )
            changed = self.db.execute(
                "UPDATE preparation SET status=?,prepared_parts=?,"
                "error_code=NULL,error_message=NULL "
                "WHERE source_id=? AND recipe_id=? AND channel=? "
                "AND space_id IS ? AND target_version_kind=? AND target_version_value=? "
                "AND target_version_type=? AND status IN ('pending','preparing')",
                (
                    "ready" if publication.complete else "preparing",
                    prepared_parts,
                    source_id,
                    publication.recipe_id,
                    publication.channel,
                    publication.space_id,
                    version_kind,
                    encoded_version,
                    version_type,
                ),
            )
            if changed.rowcount != 1:
                raise StalePublicationError("preparation target changed during publish")
        return True

    def remove_bindings(self, binding_ids: Sequence[str]) -> int:
        """Remove explicitly retired contextual bindings without touching parts."""

        unique = tuple(dict.fromkeys(binding_ids))
        if not unique:
            return 0
        with self._write_transaction():
            placeholders = ",".join("?" for _ in unique)
            cursor = self.db.execute(
                f"DELETE FROM bindings WHERE binding_id IN ({placeholders})", unique
            )
        return cursor.rowcount

    def remove_cell_bindings(
        self, *, sheet_id: int, row_id: int, column_id: int
    ) -> int:
        """Retire every source projection attached to one cell scope."""

        for value, name in (
            (sheet_id, "sheet_id"),
            (row_id, "row_id"),
            (column_id, "column_id"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        with self._write_transaction():
            cursor = self.db.execute(
                "DELETE FROM bindings WHERE sheet_id=? AND row_id=? AND column_id=?",
                (sheet_id, row_id, column_id),
            )
        return cursor.rowcount

    def remove_cell_bindings_except(
        self,
        *,
        sheet_id: int,
        row_id: int,
        column_id: int,
        keep_binding_ids: Sequence[str],
    ) -> int:
        """Retire stale projections after all current cell sources publish."""

        for value, name in (
            (sheet_id, "sheet_id"),
            (row_id, "row_id"),
            (column_id, "column_id"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        unique = tuple(dict.fromkeys(keep_binding_ids))
        if not unique:
            return self.remove_cell_bindings(
                sheet_id=sheet_id, row_id=row_id, column_id=column_id
            )
        for binding_id in unique:
            _require_nonempty(binding_id, "binding_id")
        placeholders = ",".join("?" for _ in unique)
        with self._write_transaction():
            cursor = self.db.execute(
                "DELETE FROM bindings WHERE sheet_id=? AND row_id=? AND column_id=? "
                f"AND binding_id NOT IN ({placeholders})",
                (sheet_id, row_id, column_id, *unique),
            )
        return cursor.rowcount

    def upsert_bindings(
        self, source_revision: SourceRevision, bindings: Sequence[StoredBinding]
    ) -> None:
        """Publish contextual bindings independently of representation batches."""

        source_id, _source_kind, version_kind, version_value, _content_hash = (
            _revision_fields(source_revision)
        )
        encoded_version, version_type = _encode_version(version_value)
        _validate_bindings(bindings)
        source_identity = (source_id, version_kind, encoded_version, version_type)
        with self._write_transaction():
            self._upsert_binding_records(bindings, self._source_key(source_identity))

    def replace_named_dataset_members(
        self, dataset_id: str, members: Iterable[NamedDatasetMember]
    ) -> None:
        """Replace only one named dataset's declared membership atomically."""

        _require_nonempty(dataset_id, "dataset_id")

        def rows() -> Iterable[tuple[str, str, str]]:
            for member in members:
                if member.dataset_id != dataset_id:
                    raise ValueError("all members must belong to the replaced dataset")
                yield member.dataset_id, member.part_id, member.representation_id

        with self._write_transaction():
            self.db.execute(
                "DELETE FROM named_dataset_membership WHERE dataset_id=?",
                (dataset_id,),
            )
            self.db.executemany(
                "INSERT INTO named_dataset_membership("
                "dataset_id,part_key,representation_key) VALUES (?,"
                "(SELECT part_key FROM parts WHERE part_id=?),"
                "(SELECT representation_key FROM representations "
                "WHERE representation_id=?))",
                rows(),
            )

    def preparation_state(
        self, source_id: str, *, recipe_id: str, channel: str
    ) -> PreparationState | None:
        row = self.db.execute(
            "SELECT * FROM preparation WHERE source_id=? AND recipe_id=? AND channel=?",
            (source_id, recipe_id, channel),
        ).fetchone()
        if row is None:
            return None
        return _preparation_state(row)

    def readiness_counts(
        self, *, recipe_id: str, channel: str, space_id: str | None = None
    ) -> dict[PreparationStatus, int]:
        """Read maintained status counts without scanning source or part tables."""

        _validate_space_id(space_id)
        counts: dict[PreparationStatus, int] = {
            "pending": 0,
            "preparing": 0,
            "ready": 0,
            "unavailable": 0,
            "failed": 0,
        }
        space_sql = "" if space_id is None else " AND space_id IS ?"
        params = (
            (recipe_id, channel)
            if space_id is None
            else (
                recipe_id,
                channel,
                space_id,
            )
        )
        for row in self.db.execute(
            "SELECT status,count(*) AS amount FROM preparation "
            "WHERE recipe_id=? AND channel=?" + space_sql + " GROUP BY status",
            params,
        ):
            counts[row["status"]] = int(row["amount"])
        return counts

    def get_representations(
        self, space_id: str, input_hashes: Sequence[str]
    ) -> dict[str, CachedRepresentation]:
        """Return a bounded content-addressed cache lookup keyed by input hash."""

        _require_nonempty(space_id, "space_id")
        unique = tuple(dict.fromkeys(input_hashes))
        if len(unique) > 1000:
            raise ValueError("representation cache lookup is bounded to 1000 hashes")
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        rows = self.db.execute(
            "SELECT * FROM representations WHERE space_id=? "
            f"AND input_hash IN ({placeholders})",
            (space_id, *unique),
        ).fetchall()
        return {str(row["input_hash"]): _cached_representation(row) for row in rows}

    def put_representations(
        self, representations: Sequence[CachedRepresentation]
    ) -> None:
        """Populate the shared cache without inventing located parts."""

        for representation in representations:
            _validate_cached_representation(representation)
        with self._write_transaction():
            self._upsert_representations(representations)

    def read_named_dataset_vectors(
        self,
        dataset_id: str,
        *,
        space_id: str,
        limit: int,
        after: tuple[str, str] | None = None,
    ) -> tuple[NamedDatasetVector, ...]:
        """Read one bounded ordered page for export or analytical computation."""

        _require_nonempty(dataset_id, "dataset_id")
        _require_nonempty(space_id, "space_id")
        if after is not None:
            _require_nonempty(after[0], "after part_id")
            _require_nonempty(after[1], "after representation_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("named dataset vector limit must be in [1, 1000]")
        rows = self.db.execute(
            "SELECT part.part_id,attachment.channel,representation.* "
            "FROM named_dataset_membership AS member "
            "JOIN part_representations AS attachment "
            "ON attachment.part_key=member.part_key "
            "AND attachment.representation_key=member.representation_key "
            "JOIN parts AS part ON part.part_key=member.part_key "
            "JOIN representations AS representation "
            "ON representation.representation_key=member.representation_key "
            "WHERE member.dataset_id=? AND representation.space_id=? "
            "AND (part.part_id>? OR "
            "(part.part_id=? AND representation.representation_id>?)) "
            "ORDER BY part.part_id,representation.representation_id LIMIT ?",
            (
                dataset_id,
                space_id,
                "" if after is None else after[0],
                "" if after is None else after[0],
                "" if after is None else after[1],
                limit,
            ),
        ).fetchall()
        return tuple(
            NamedDatasetVector(
                part_id=str(row["part_id"]),
                channel=str(row["channel"]),
                representation=_cached_representation(row),
            )
            for row in rows
        )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        if self.db.in_transaction:
            raise RuntimeError("retrieval publication owns its write transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _source_key(self, identity: tuple[str, str, str, str]) -> int:
        row = self.db.execute(
            "SELECT source_key FROM sources WHERE source_id=? AND version_kind=? "
            "AND version_value=? AND version_type=?",
            identity,
        ).fetchone()
        if row is None:
            raise ValueError("source revision has not been published")
        return int(row[0])

    def _upsert_parts(self, publication: SourcePublication, source_key: int) -> None:
        self.db.executemany(
            "INSERT INTO parts("
            "part_id,source_key,recipe_id,publication_channel,part_kind,"
            "locator_json,group_key) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(part_id) DO UPDATE SET "
            "source_key=excluded.source_key,recipe_id=excluded.recipe_id,"
            "publication_channel=excluded.publication_channel,"
            "part_kind=excluded.part_kind,locator_json=excluded.locator_json,"
            "group_key=excluded.group_key",
            [
                (
                    part.part_id,
                    source_key,
                    publication.recipe_id,
                    publication.channel,
                    part.kind,
                    canonical_json(part.locator),
                    part.group_key,
                )
                for part in publication.parts
            ],
        )

    def _upsert_bindings(self, publication: SourcePublication, source_key: int) -> None:
        self._upsert_binding_records(publication.bindings, source_key)

    def _upsert_binding_records(
        self,
        bindings: Sequence[StoredBinding],
        source_key: int,
    ) -> None:
        self.db.executemany(
            "INSERT INTO bindings("
            "binding_id,source_key,sheet_id,row_id,column_id) VALUES (?,?,?,?,?) "
            "ON CONFLICT(binding_id) DO UPDATE SET "
            "source_key=excluded.source_key,"
            "sheet_id=excluded.sheet_id,row_id=excluded.row_id,"
            "column_id=excluded.column_id",
            [
                (
                    binding.binding_id,
                    source_key,
                    binding.sheet_id,
                    binding.row_id,
                    binding.column_id,
                )
                for binding in bindings
            ],
        )
        binding_ids = [binding.binding_id for binding in bindings]
        if binding_ids:
            placeholders = ",".join("?" for _ in binding_ids)
            self.db.execute(
                "DELETE FROM binding_dependencies WHERE binding_key IN ("
                "SELECT binding_key FROM bindings "
                f"WHERE binding_id IN ({placeholders}))",
                binding_ids,
            )
        self.db.executemany(
            "INSERT INTO binding_dependencies("
            "binding_key,row_id,column_id,retrieval_revision) "
            "VALUES ((SELECT binding_key FROM bindings WHERE binding_id=?),?,?,?)",
            [
                (
                    binding.binding_id,
                    dependency.row_id,
                    dependency.column_id,
                    dependency.retrieval_revision,
                )
                for binding in bindings
                for dependency in binding.dependencies
            ],
        )

    def _upsert_representations(
        self, representations: Sequence[CachedRepresentation]
    ) -> None:
        for representation in representations:
            self.db.execute(
                "INSERT OR IGNORE INTO representations("
                "representation_id,space_id,input_hash,vector,dimension,precision"
                ") VALUES (?,?,?,?,?,?)",
                (
                    representation.representation_id,
                    representation.space_id,
                    representation.input_hash,
                    sqlite3.Binary(representation.vector),
                    representation.dimension,
                    representation.precision,
                ),
            )
            stored = self.db.execute(
                "SELECT * FROM representations WHERE space_id=? AND input_hash=?",
                (representation.space_id, representation.input_hash),
            ).fetchone()
            if stored is None or _cached_representation(stored) != representation:
                raise ValueError(
                    "representation identity conflicts with immutable cached content"
                )

    def _attach_representations(
        self, attachments: Sequence[PartRepresentation]
    ) -> None:
        for attachment in attachments:
            self.db.execute(
                "INSERT INTO part_representations("
                "part_key,representation_key,channel) VALUES ("
                "(SELECT part_key FROM parts WHERE part_id=?),"
                "(SELECT representation_key FROM representations "
                "WHERE representation_id=?),?) "
                "ON CONFLICT(part_key,representation_key,channel) DO NOTHING",
                (
                    attachment.part_id,
                    attachment.representation_id,
                    attachment.channel,
                ),
            )

    def _replace_lexical(self, publication: SourcePublication, source_key: int) -> None:
        if publication.lexical is None:
            return
        old = self.db.execute(
            "SELECT rowid FROM lexical_sources WHERE source_key=?", (source_key,)
        ).fetchone()
        if old is not None:
            self.db.execute("DELETE FROM lexical_fts WHERE rowid=?", (int(old[0]),))
            self.db.execute("DELETE FROM lexical_sources WHERE rowid=?", (int(old[0]),))
        cursor = self.db.execute(
            "INSERT INTO lexical_sources("
            "source_key,content_hash,locator_json) VALUES (?,?,?)",
            (
                source_key,
                "pending",
                canonical_json(publication.lexical.locator),
            ),
        )
        lexical_rowid = int(cursor.lastrowid)
        if not publication.lexical.text.strip():
            self.db.execute(
                "DELETE FROM lexical_sources WHERE rowid=?", (lexical_rowid,)
            )
            return
        self.db.execute(
            "UPDATE lexical_sources SET content_hash=? WHERE rowid=?",
            (input_hash(publication.lexical.text), lexical_rowid),
        )
        self.db.execute(
            "INSERT INTO lexical_fts(rowid,content) VALUES (?,?)",
            (lexical_rowid, publication.lexical.text),
        )

    def _upsert_memberships(self, publication: SourcePublication) -> None:
        self.db.executemany(
            "INSERT INTO named_dataset_membership("
            "dataset_id,part_key,representation_key) VALUES (?,"
            "(SELECT part_key FROM parts WHERE part_id=?),"
            "(SELECT representation_key FROM representations "
            "WHERE representation_id=?)) "
            "ON CONFLICT(dataset_id,part_key,representation_key) DO NOTHING",
            [
                (member.dataset_id, member.part_id, member.representation_id)
                for member in publication.named_memberships
            ],
        )


def read_sidecar_id(project_or_path: Any) -> str | None:
    """Read a sidecar incarnation without creating or repairing storage."""

    project_path = getattr(project_or_path, "path", project_or_path)
    path = Path(project_path)
    path = path if path.name == RETRIEVAL_DB_NAME else path / RETRIEVAL_DB_NAME
    if not path.is_file():
        return None
    db: sqlite3.Connection | None = None
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
        row = db.execute(
            "SELECT value FROM retrieval_meta WHERE key=?", (SIDECAR_ID_META_KEY,)
        ).fetchone()
        return None if row is None else str(uuid.UUID(str(row[0])))
    except (sqlite3.Error, TypeError, ValueError, AttributeError):
        return None
    finally:
        if db is not None:
            db.close()


def _revision_fields(
    revision: SourceRevision,
) -> tuple[str, str, str, int | str, str | None]:
    if not isinstance(revision, SourceRevision):
        raise TypeError("source_revision must be SourceRevision")
    fields = (
        revision.source_id,
        revision.source_kind,
        revision.version_kind,
        revision.version_value,
        revision.content_hash,
    )
    source_id, source_kind, version_kind, version_value, content_hash = fields
    _require_nonempty(source_id, "source_id")
    _require_nonempty(source_kind, "source_kind")
    _require_nonempty(version_kind, "version_kind")
    _encode_version(version_value)
    if content_hash is not None:
        _require_nonempty(str(content_hash), "content_hash")
    return source_id, source_kind, version_kind, version_value, content_hash


def _encode_version(value: int | str) -> tuple[str, str]:
    if isinstance(value, bool):
        raise ValueError("source version must not be boolean")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("integer source version must be positive")
        return str(value), "integer"
    if isinstance(value, str) and value.strip():
        return value, "text"
    raise ValueError("source version must be a positive integer or non-empty string")


def _decode_version(value: str, value_type: str) -> int | str:
    return int(value) if value_type == "integer" else value


def _preparation_state(row: sqlite3.Row) -> PreparationState:
    return PreparationState(
        source_id=str(row["source_id"]),
        recipe_id=str(row["recipe_id"]),
        channel=str(row["channel"]),
        space_id=None if row["space_id"] is None else str(row["space_id"]),
        target_version_kind=str(row["target_version_kind"]),
        target_version_value=_decode_version(
            str(row["target_version_value"]), str(row["target_version_type"])
        ),
        status=row["status"],
        prepared_parts=int(row["prepared_parts"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        cursor_json=row["cursor_json"],
    )


def _preparation_work(row: sqlite3.Row) -> PreparationWork:
    state = _preparation_state(row)
    return PreparationWork(
        source_revision=SourceRevision(
            source_id=state.source_id,
            source_kind=str(row["source_kind"]),
            version_kind=state.target_version_kind,
            version_value=state.target_version_value,
            content_hash=row["content_hash"],
        ),
        recipe_id=state.recipe_id,
        channel=state.channel,
        space_id=state.space_id,
        status=state.status,
        prepared_parts=state.prepared_parts,
        cursor_json=state.cursor_json,
    )


def _require_nonempty(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


def _validate_space_id(value: str | None) -> None:
    if value is not None:
        _require_nonempty(value, "space_id")


def _cached_representation(row: sqlite3.Row) -> CachedRepresentation:
    return CachedRepresentation(
        representation_id=str(row["representation_id"]),
        space_id=str(row["space_id"]),
        input_hash=str(row["input_hash"]),
        vector=bytes(row["vector"]),
        dimension=int(row["dimension"]),
        precision=str(row["precision"]),
    )


def _validate_cached_representation(representation: CachedRepresentation) -> None:
    _require_nonempty(representation.representation_id, "representation_id")
    _require_nonempty(representation.space_id, "space_id")
    _require_nonempty(representation.input_hash, "input_hash")
    if representation.precision != "float32":
        raise ValueError("only float32 retrieval representations are supported")
    if isinstance(representation.dimension, bool) or representation.dimension < 1:
        raise ValueError("representation dimension must be positive")
    validate_vector_blob(
        representation.vector,
        dimensions=representation.dimension,
        require_unit_norm=False,
    )


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    return {value for value in values if value in seen or seen.add(value)}


def _validate_publication(publication: SourcePublication) -> None:
    _revision_fields(publication.source_revision)
    _require_nonempty(publication.recipe_id, "recipe_id")
    _require_nonempty(publication.channel, "channel")
    _validate_space_id(publication.space_id)
    part_ids = {part.part_id for part in publication.parts}
    if duplicates := _duplicates(part.part_id for part in publication.parts):
        raise ValueError(f"duplicate part ids: {sorted(duplicates)!r}")
    _validate_bindings(publication.bindings)
    representation_ids: set[str] = set()
    for representation in publication.representations:
        if representation.representation_id in representation_ids:
            raise ValueError(
                f"duplicate representation id {representation.representation_id!r}"
            )
        representation_ids.add(representation.representation_id)
        _validate_cached_representation(representation)
        if (
            publication.space_id is not None
            and publication.space_id != representation.space_id
        ):
            raise ValueError(
                "publication representations must belong to its exact space_id"
            )
    attachment_keys: set[tuple[str, str, str]] = set()
    for attachment in publication.attachments:
        _require_nonempty(attachment.part_id, "attachment part_id")
        _require_nonempty(attachment.representation_id, "attachment representation_id")
        _require_nonempty(attachment.channel, "attachment channel")
        key = (attachment.part_id, attachment.representation_id, attachment.channel)
        if key in attachment_keys:
            raise ValueError(f"duplicate representation attachment {key!r}")
        attachment_keys.add(key)
        if attachment.part_id not in part_ids:
            raise ValueError("representation attachment references an unlisted part")
    for member in publication.named_memberships:
        if member.part_id not in part_ids:
            raise ValueError(
                "named membership references a part outside its publication"
            )
        if not any(
            attachment.part_id == member.part_id
            and attachment.representation_id == member.representation_id
            for attachment in publication.attachments
        ):
            raise ValueError(
                "named membership requires a matching representation attachment"
            )


def _validate_bindings(bindings: Sequence[StoredBinding]) -> None:
    if duplicates := _duplicates(binding.binding_id for binding in bindings):
        raise ValueError(f"duplicate binding ids: {sorted(duplicates)!r}")
    for binding in bindings:
        if not binding.dependencies:
            raise ValueError("every binding requires an authoritative cell dependency")
        dependency_keys = [
            (dependency.row_id, dependency.column_id)
            for dependency in binding.dependencies
        ]
        if len(set(dependency_keys)) != len(dependency_keys):
            raise ValueError("binding dependencies must not contain duplicate cells")
        for dependency in binding.dependencies:
            if (
                isinstance(dependency.row_id, bool)
                or dependency.row_id <= 0
                or isinstance(dependency.column_id, bool)
                or dependency.column_id <= 0
                or isinstance(dependency.retrieval_revision, bool)
                or dependency.retrieval_revision < 0
            ):
                raise ValueError(
                    "binding dependency IDs must be positive and revision non-negative"
                )


def _schema_statements(script: str) -> Iterable[str]:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise RuntimeError("retrieval schema contains an incomplete SQL statement")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS retrieval_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sources (
  source_key INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  version_kind TEXT NOT NULL,
  version_value TEXT NOT NULL,
  version_type TEXT NOT NULL CHECK(version_type IN ('integer','text')),
  content_hash TEXT,
  UNIQUE(source_id,version_kind,version_value,version_type)
);

CREATE TABLE IF NOT EXISTS parts (
  part_key INTEGER PRIMARY KEY,
  part_id TEXT NOT NULL UNIQUE,
  source_key INTEGER NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
  recipe_id TEXT NOT NULL,
  publication_channel TEXT NOT NULL,
  part_kind TEXT NOT NULL,
  locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
  group_key TEXT
);
CREATE INDEX IF NOT EXISTS parts_source_recipe
  ON parts(source_key,recipe_id,publication_channel,part_key);

CREATE TABLE IF NOT EXISTS bindings (
  binding_key INTEGER PRIMARY KEY,
  binding_id TEXT NOT NULL UNIQUE,
  source_key INTEGER NOT NULL REFERENCES sources(source_key) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  column_id INTEGER
);
CREATE INDEX IF NOT EXISTS bindings_scope
  ON bindings(sheet_id,row_id,column_id,source_key,binding_key);
CREATE INDEX IF NOT EXISTS bindings_source
  ON bindings(source_key,binding_key);

CREATE TABLE IF NOT EXISTS binding_dependencies (
  binding_key INTEGER NOT NULL REFERENCES bindings(binding_key) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  retrieval_revision INTEGER NOT NULL CHECK(retrieval_revision>=0),
  PRIMARY KEY(binding_key,column_id,row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS binding_dependencies_cell
  ON binding_dependencies(column_id,row_id,retrieval_revision,binding_key);

CREATE TABLE IF NOT EXISTS representations (
  representation_key INTEGER PRIMARY KEY,
  representation_id TEXT NOT NULL UNIQUE,
  space_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  vector BLOB NOT NULL,
  dimension INTEGER NOT NULL CHECK(dimension>0),
  precision TEXT NOT NULL CHECK(precision='float32'),
  UNIQUE(space_id,input_hash)
);

CREATE TABLE IF NOT EXISTS part_representations (
  part_key INTEGER NOT NULL REFERENCES parts(part_key) ON DELETE CASCADE,
  representation_key INTEGER NOT NULL REFERENCES representations(representation_key)
    ON DELETE RESTRICT,
  channel TEXT NOT NULL,
  PRIMARY KEY(part_key,representation_key,channel)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS part_representations_space
  ON part_representations(representation_key,part_key,channel);

CREATE TABLE IF NOT EXISTS lexical_sources (
  rowid INTEGER PRIMARY KEY,
  source_key INTEGER NOT NULL UNIQUE REFERENCES sources(source_key) ON DELETE CASCADE,
  content_hash TEXT NOT NULL,
  locator_json TEXT NOT NULL CHECK(json_valid(locator_json))
);
CREATE VIRTUAL TABLE IF NOT EXISTS lexical_fts USING fts5(
  content,content='',contentless_delete=1
);

CREATE TABLE IF NOT EXISTS preparation (
  source_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  space_id TEXT,
  target_version_kind TEXT NOT NULL,
  target_version_value TEXT NOT NULL,
  target_version_type TEXT NOT NULL CHECK(target_version_type IN ('integer','text')),
  status TEXT NOT NULL CHECK(status IN (
    'pending','preparing','ready','unavailable','failed'
  )),
  prepared_parts INTEGER NOT NULL DEFAULT 0 CHECK(prepared_parts>=0),
  error_code TEXT,
  error_message TEXT,
  cursor_json TEXT CHECK(cursor_json IS NULL OR json_valid(cursor_json)),
  PRIMARY KEY(source_id,recipe_id,channel)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS preparation_counts
  ON preparation(recipe_id,channel,status);
CREATE INDEX IF NOT EXISTS preparation_work_order
  ON preparation(
    CASE status WHEN 'preparing' THEN 0 ELSE 1 END,
    source_id,recipe_id,channel
  ) WHERE status IN ('pending','preparing');
CREATE INDEX IF NOT EXISTS preparation_channel_work_order
  ON preparation(
    space_id,channel,
    CASE status WHEN 'preparing' THEN 0 ELSE 1 END,
    source_id,recipe_id
  ) WHERE status IN ('pending','preparing');

CREATE TABLE IF NOT EXISTS named_dataset_membership (
  dataset_id TEXT NOT NULL,
  part_key INTEGER NOT NULL REFERENCES parts(part_key) ON DELETE CASCADE,
  representation_key INTEGER NOT NULL REFERENCES representations(representation_key)
    ON DELETE RESTRICT,
  PRIMARY KEY(dataset_id,part_key,representation_key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS named_dataset_part
  ON named_dataset_membership(part_key,dataset_id,representation_key);
"""

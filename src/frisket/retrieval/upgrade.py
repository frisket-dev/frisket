"""One-shot admission of the retired embedding sidecars into retrieval storage.

The host calls this while project upgrade/open is serialized, after old embedding
workers are stopped and before any unified retrieval preparation starts.  A
completed marker makes the expensive proof scan one-shot; interrupted work is
safe to repeat because every underlying migration write is content-addressed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from filelock import FileLock, Timeout

from frisket.ai.embeddings.analytics import ensure_legacy_analytical_space
from frisket.ai.embeddings.backends import (
    LOCAL_MODEL,
    PROVIDERLESS_CLASSIFY_MODEL,
    REMOTE_EMBED_MODEL,
)
from frisket.engine.store.value_codec import decode_stored_value
from frisket.engine.store.search_index_work import enqueue_dirty_scope
from frisket.search_hydration import searchable_text

from .migration import (
    LegacyCacheMigrationReport,
    LegacyCellVectorProof,
    MigrationMiss,
    legacy_cell_vector_key,
    migrate_legacy_embedding_caches,
)
from .store import RetrievalStore


LEGACY_EMBEDDING_UPGRADE_META_KEY = "retrieval_legacy_embedding_upgrade_v1"
_MARKER_VERSION = 3
_UPGRADE_LOCK_NAME = ".retrieval-upgrade.lock"
_LEGACY_FILENAMES = ("project.search.db", "project.embeddings.db")
_SEMANTIC_PREFIX_CHARACTERS = 50_000
_CLASSIFIER_CHUNK_WORDS = 384
_CLASSIFIER_CHUNK_CHARACTERS = 1_600
_CLASSIFIER_MAX_CHUNKS = 8
_CELL_VECTOR_COLUMNS = frozenset({"key", "vec"})
_SEMANTIC_ACTION_KINDS = ("cluster.values", "join.semantic")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LegacyEmbeddingUpgradeReport:
    """Durable result of one finite legacy-cache upgrade."""

    migration: LegacyCacheMigrationReport
    analytical_candidate_keys: int
    analytical_matches: int
    analytical_proofs: int
    analytical_space_rejections: int
    analytical_vector_shape_rejections: int
    reused: bool = False

    @property
    def complete(self) -> bool:
        return self.migration.complete


def prepare_retrieval_on_open(project: Any) -> None:
    """Recover disposable storage and import old caches under the bundle-open lock."""

    from frisket.search_index import bind_retrieval_sidecar

    project_path = Path(project.path)
    sidecar_exists = (project_path / "project.retrieval.db").is_file()
    legacy_exists = any((project_path / name).is_file() for name in _LEGACY_FILENAMES)
    if sidecar_exists or legacy_exists:
        # Existing derived state must be bound before readers can consider it
        # current. A legacy import also needs a sidecar to receive its rows.
        bind_retrieval_sidecar(project)
    else:
        # Opening a project remains read-only with respect to derived storage.
        # Persist the repair intent in the authoritative database; the bounded
        # maintenance writer creates and binds the sidecar on its first batch.
        with project.transaction_scope():
            global_scope = project.db.execute(
                "SELECT 1 FROM search_dirty_scopes WHERE sheet_id IS NULL "
                "AND scan_cursor='[0,0]' LIMIT 1"
            ).fetchone()
            if global_scope is None:
                enqueue_dirty_scope(project.db)
        return
    if not legacy_exists:
        return
    lock = FileLock(str(project_path / _UPGRADE_LOCK_NAME), timeout=0)
    try:
        with lock:
            try:
                _upgrade_legacy_embedding_caches(project, batch_size=250)
            except Exception:
                _LOG.exception(
                    "legacy retrieval cache upgrade failed during project open"
                )
    except Timeout:
        # Another opener owns the same idempotent attempt. It will persist the
        # report; this opener must not wait on derived-cache recovery.
        return


def upgrade_legacy_embedding_caches(
    project: Any,
    *,
    batch_size: int = 250,
) -> LegacyEmbeddingUpgradeReport:
    """Import every exactly provable legacy vector and mark completion once.

    No provider is called and neither legacy sidecar is removed.  The caller
    must serialize this boundary and ensure legacy workers cannot append after
    it; raw manual restoration of an older project database requires deleting
    derived retrieval state before reopening the bundle.
    """

    lock = FileLock(str(Path(project.path) / _UPGRADE_LOCK_NAME), timeout=-1)
    with lock:
        return _upgrade_legacy_embedding_caches(project, batch_size=batch_size)


def _upgrade_legacy_embedding_caches(
    project: Any,
    *,
    batch_size: int,
) -> LegacyEmbeddingUpgradeReport:
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be in [1, 1000]")
    if project.db.in_transaction:
        raise ValueError("legacy embedding upgrade requires no active transaction")
    fingerprints = _legacy_fingerprints(Path(project.path))
    with RetrievalStore(project).open() as retrieval_store:
        sidecar_id = retrieval_store.sidecar_id()
        marked = _read_marker(project, sidecar_id=sidecar_id, fingerprints=fingerprints)
        if marked is not None:
            return replace(marked, reused=True)

        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as matches:
            discovery = _discover_analytical_matches(
                project, matches=matches, batch_size=batch_size
            )
            spaces, space_rejections = _ensure_matched_spaces(project, matches)
            proof_count = _count_proofs(matches, spaces)
            migration = migrate_legacy_embedding_caches(
                project,
                retrieval_store,
                cell_vector_proofs=_iter_proofs(matches, spaces),
                batch_size=batch_size,
            )

        report = LegacyEmbeddingUpgradeReport(
            migration=migration,
            analytical_candidate_keys=discovery.candidate_keys,
            analytical_matches=discovery.matches,
            analytical_proofs=proof_count,
            analytical_space_rejections=space_rejections,
            analytical_vector_shape_rejections=discovery.vector_shape_rejections,
        )
        _write_marker(
            project,
            sidecar_id=sidecar_id,
            fingerprints=fingerprints,
            report=report,
        )
    return report


@dataclass(frozen=True, slots=True)
class _Discovery:
    candidate_keys: int = 0
    matches: int = 0
    vector_shape_rejections: int = 0


def _discover_analytical_matches(
    project: Any,
    *,
    matches: TextIO,
    batch_size: int,
) -> _Discovery:
    path = Path(project.path) / "project.search.db"
    if not path.is_file():
        return _Discovery()
    legacy: sqlite3.Connection | None = None
    try:
        legacy = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
        legacy.row_factory = sqlite3.Row
        if not _has_cell_vector_schema(legacy):
            return _Discovery()
        legacy.execute("BEGIN")
        if legacy.execute("SELECT 1 FROM cell_vec LIMIT 1").fetchone() is None:
            return _Discovery()
        legacy.execute(
            "CREATE TEMP TABLE seen_upgrade_keys(key TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        candidate_count = match_count = shape_rejections = 0
        pending: dict[str, tuple[str, str]] = {}
        with project.read_snapshot() as snapshot:
            model_ids = _legacy_model_ids(snapshot)
            for model_id, text in _candidate_pairs(snapshot, model_ids, batch_size):
                key = legacy_cell_vector_key(model_id, text)
                if not legacy.execute(
                    "INSERT OR IGNORE INTO seen_upgrade_keys(key) VALUES (?)", (key,)
                ).rowcount:
                    continue
                candidate_count += 1
                pending[key] = (model_id, text)
                if len(pending) < batch_size:
                    continue
                found, rejected = _write_matches(legacy, pending, matches)
                match_count += found
                shape_rejections += rejected
                pending.clear()
        if pending:
            found, rejected = _write_matches(legacy, pending, matches)
            match_count += found
            shape_rejections += rejected
        matches.flush()
        matches.seek(0)
        return _Discovery(candidate_count, match_count, shape_rejections)
    except (sqlite3.Error, OSError):
        return _Discovery()
    finally:
        if legacy is not None:
            legacy.close()


def _write_matches(
    legacy: sqlite3.Connection,
    pending: dict[str, tuple[str, str]],
    matches: TextIO,
) -> tuple[int, int]:
    placeholders = ",".join("?" for _ in pending)
    rows = legacy.execute(
        f"SELECT key,vec FROM cell_vec WHERE key IN ({placeholders})",
        tuple(pending),
    )
    found = rejected = 0
    for row in rows:
        key = str(row["key"])
        try:
            vector = bytes(row["vec"])
        except (TypeError, ValueError):
            rejected += 1
            continue
        if not vector or len(vector) % 4:
            rejected += 1
            continue
        model_id, text = pending[key]
        matches.write(
            json.dumps(
                [model_id, text, len(vector) // 4],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        found += 1
    return found, rejected


def _legacy_model_ids(snapshot: Any) -> tuple[str, ...]:
    values = {
        f"fastembed/{LOCAL_MODEL}",
        f"fastembed/{PROVIDERLESS_CLASSIFY_MODEL}",
        REMOTE_EMBED_MODEL,
    }
    values.update(
        str(row["model"])
        for row in snapshot.db.execute(
            "SELECT DISTINCT model FROM runs WHERE model IS NOT NULL "
            "AND action_kind IN (?,?)",
            _SEMANTIC_ACTION_KINDS,
        )
        if str(row["model"]).strip()
    )
    for row in snapshot.db.execute(
        "SELECT engine,model_ids FROM model_calls WHERE capability='llm.embed'"
    ):
        engine = str(row["engine"])
        if engine.strip():
            values.add(engine)
        try:
            recorded = json.loads(row["model_ids"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(recorded, list):
            values.update(
                item for item in recorded if isinstance(item, str) and item.strip()
            )
    values.update(
        str(row["actual_model_id"])
        for row in snapshot.db.execute(
            "SELECT DISTINCT actual_model_id FROM embedding_spaces "
            "WHERE modality IN ('text','row')"
        )
        if str(row["actual_model_id"]).strip()
    )
    return tuple(sorted(values))


def _candidate_pairs(
    snapshot: Any,
    model_ids: Sequence[str],
    batch_size: int,
) -> Iterator[tuple[str, str]]:
    classifier_model = f"fastembed/{PROVIDERLESS_CLASSIFY_MODEL}"
    for text in _legacy_texts(snapshot, batch_size):
        variants = tuple(dict.fromkeys(_text_variants(text)))
        for model_id in model_ids:
            for variant in variants:
                yield model_id, variant
        for chunk in _legacy_classifier_chunks(text):
            yield classifier_model, chunk


def _legacy_classifier_chunks(text: str) -> Iterator[str]:
    """Replay the retired local-semantic cache input contract exactly."""

    chunk_count = 0
    current: list[str] = []
    current_characters = 0
    for word in re.findall(r"\S+", text):
        pieces = [
            word[offset : offset + _CLASSIFIER_CHUNK_CHARACTERS]
            for offset in range(0, len(word), _CLASSIFIER_CHUNK_CHARACTERS)
        ] or [word]
        for piece in pieces:
            added = len(piece) + (1 if current else 0)
            if current and (
                len(current) >= _CLASSIFIER_CHUNK_WORDS
                or current_characters + added > _CLASSIFIER_CHUNK_CHARACTERS
            ):
                yield " ".join(current)
                chunk_count += 1
                if chunk_count >= _CLASSIFIER_MAX_CHUNKS:
                    return
                current = []
                current_characters = 0
            current.append(piece)
            current_characters += len(piece) + (1 if len(current) > 1 else 0)
    if current and chunk_count < _CLASSIFIER_MAX_CHUNKS:
        yield " ".join(current)


def _legacy_texts(snapshot: Any, batch_size: int) -> Iterator[str]:
    current = snapshot.db.execute(
        "SELECT logical.value_kind,logical.value "
        "FROM current_cell_values logical "
        "JOIN columns column_record ON column_record.id=logical.column_id "
        "JOIN sheets sheet ON sheet.id=column_record.sheet_id "
        "JOIN rows row_record ON row_record.id=logical.row_id "
        "WHERE column_record.hidden=0 AND sheet.hidden=0 AND row_record.hidden=0 "
        "AND column_record.type IN ('text','category','json','link') "
        "ORDER BY logical.column_id,logical.row_id"
    )
    while rows := current.fetchmany(batch_size):
        for row in rows:
            value = decode_stored_value(
                row["value_kind"], row["value"], tolerate_errors=True
            )
            text = searchable_text(value)
            if text is not None:
                yield text
    for sql in (
        "SELECT prepared_text AS text FROM prepared_page_versions "
        "WHERE length(trim(prepared_text))>0 ORDER BY id",
        "SELECT quote AS text FROM source_spans WHERE quote IS NOT NULL "
        "AND length(trim(quote))>0 ORDER BY id",
    ):
        cursor = snapshot.db.execute(sql)
        while rows := cursor.fetchmany(batch_size):
            yield from (str(row["text"]) for row in rows)


def _text_variants(text: str) -> Iterator[str]:
    if not text.strip():
        return
    yield text
    stripped = text.strip()
    if stripped != text:
        yield stripped
    prefix = text[:_SEMANTIC_PREFIX_CHARACTERS]
    if prefix not in {text, stripped}:
        yield prefix


def _ensure_matched_spaces(
    project: Any, matches: TextIO
) -> tuple[dict[tuple[str, int], str], int]:
    spaces: dict[tuple[str, int], str] = {}
    rejected: set[tuple[str, int]] = set()
    matches.seek(0)
    for line in matches:
        model_id, _text, dimension = json.loads(line)
        key = (str(model_id), int(dimension))
        if key in spaces or key in rejected:
            continue
        try:
            spaces[key] = ensure_legacy_analytical_space(
                project,
                key[0],
                dimension=key[1],
                normalization="none",
            ).identity
        except (TypeError, ValueError):
            rejected.add(key)
    matches.seek(0)
    return spaces, len(rejected)


def _count_proofs(matches: TextIO, spaces: dict[tuple[str, int], str]) -> int:
    matches.seek(0)
    count = sum(
        (str(model_id), int(dimension)) in spaces
        for model_id, _text, dimension in map(json.loads, matches)
    )
    matches.seek(0)
    return count


def _iter_proofs(
    matches: TextIO, spaces: dict[tuple[str, int], str]
) -> Iterator[LegacyCellVectorProof]:
    matches.seek(0)
    for line in matches:
        model_id, text, dimension = json.loads(line)
        space_id = spaces.get((str(model_id), int(dimension)))
        if space_id is not None:
            yield LegacyCellVectorProof(space_id, str(model_id), str(text))


def _has_cell_vector_schema(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cell_vec'"
    ).fetchone()
    if row is None:
        return False
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(cell_vec)")}
    return _CELL_VECTOR_COLUMNS <= columns


def _legacy_fingerprints(project_path: Path) -> dict[str, list[int]]:
    fingerprints: dict[str, list[int]] = {}
    for name in _LEGACY_FILENAMES:
        path = project_path / name
        if not path.is_file():
            continue
        stat = path.stat()
        fingerprints[name] = [int(stat.st_size), int(stat.st_mtime_ns)]
        wal_name = f"{name}-wal"
        try:
            wal_stat = (project_path / wal_name).stat()
        except FileNotFoundError:
            fingerprints[wal_name] = [0, 0, 0]
        else:
            fingerprints[wal_name] = [
                1,
                int(wal_stat.st_size),
                int(wal_stat.st_mtime_ns),
            ]
    return fingerprints


def _write_marker(
    project: Any,
    *,
    sidecar_id: str,
    fingerprints: dict[str, list[int]],
    report: LegacyEmbeddingUpgradeReport,
) -> None:
    project.db.execute(
        "INSERT INTO meta(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            LEGACY_EMBEDDING_UPGRADE_META_KEY,
            _encode_marker(
                sidecar_id=sidecar_id,
                fingerprints=fingerprints,
                report=report,
            ),
        ),
    )
    project.db.commit()


def _read_marker(
    project: Any,
    *,
    sidecar_id: str,
    fingerprints: dict[str, list[int]],
) -> LegacyEmbeddingUpgradeReport | None:
    row = project.db.execute(
        "SELECT value FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["value"])
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _MARKER_VERSION
            or payload.get("sidecar_id") != sidecar_id
            or payload.get("legacy") != fingerprints
        ):
            return None
        return _decode_report(payload["report"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _report_payload(report: LegacyEmbeddingUpgradeReport) -> dict[str, Any]:
    migration = report.migration
    return {
        "migration": {
            "named_sidecar_found": migration.named_sidecar_found,
            "analytical_sidecar_found": migration.analytical_sidecar_found,
            "named_scan_complete": migration.named_scan_complete,
            "analytical_scan_complete": migration.analytical_scan_complete,
            "named_indexes": migration.named_indexes,
            "named_items": migration.named_items,
            "named_imported": migration.named_imported,
            "analytical_items": migration.analytical_items,
            "analytical_imported": migration.analytical_imported,
            "misses": [
                {"reason": miss.reason, "count": miss.count}
                for miss in migration.misses
            ],
        },
        "analytical_candidate_keys": report.analytical_candidate_keys,
        "analytical_matches": report.analytical_matches,
        "analytical_proofs": report.analytical_proofs,
        "analytical_space_rejections": report.analytical_space_rejections,
        "analytical_vector_shape_rejections": (
            report.analytical_vector_shape_rejections
        ),
    }


def _decode_report(payload: Any) -> LegacyEmbeddingUpgradeReport:
    if not isinstance(payload, dict):
        raise ValueError("upgrade report must be an object")
    migration_payload = payload["migration"]
    migration = LegacyCacheMigrationReport(
        named_sidecar_found=bool(migration_payload["named_sidecar_found"]),
        analytical_sidecar_found=bool(migration_payload["analytical_sidecar_found"]),
        named_scan_complete=bool(migration_payload["named_scan_complete"]),
        analytical_scan_complete=bool(migration_payload["analytical_scan_complete"]),
        named_indexes=int(migration_payload["named_indexes"]),
        named_items=int(migration_payload["named_items"]),
        named_imported=int(migration_payload["named_imported"]),
        analytical_items=int(migration_payload["analytical_items"]),
        analytical_imported=int(migration_payload["analytical_imported"]),
        misses=tuple(
            MigrationMiss(str(item["reason"]), int(item["count"]))
            for item in migration_payload["misses"]
        ),
    )
    return LegacyEmbeddingUpgradeReport(
        migration=migration,
        analytical_candidate_keys=int(payload["analytical_candidate_keys"]),
        analytical_matches=int(payload["analytical_matches"]),
        analytical_proofs=int(payload["analytical_proofs"]),
        analytical_space_rejections=int(payload["analytical_space_rejections"]),
        analytical_vector_shape_rejections=int(
            payload["analytical_vector_shape_rejections"]
        ),
    )


def _encode_marker(
    *,
    sidecar_id: str,
    fingerprints: dict[str, list[int]],
    report: LegacyEmbeddingUpgradeReport,
) -> str:
    return json.dumps(
        {
            "version": _MARKER_VERSION,
            "sidecar_id": sidecar_id,
            "legacy": fingerprints,
            "report": _report_payload(report),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "LEGACY_EMBEDDING_UPGRADE_META_KEY",
    "LegacyEmbeddingUpgradeReport",
    "upgrade_legacy_embedding_caches",
]

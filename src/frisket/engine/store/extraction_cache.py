"""Project-scoped content-addressed extraction cache in ``project.cache.db``.

Stores immutable extraction payloads for ``media.ocr`` and ``media.transcribe``
keyed by ``(cache_key_version, blob_hash, page_selector, capability, engine,
engine_version, options_hash)`` strictly inside ``<project>/project.cache.db``.

This store is disposable and rebuildable: deleting or corrupting
``project.cache.db`` never harms ``project.db`` or blocks Purge. Cache hits
supply raw extraction facts so each target Sheet still mints its own
``source_artifacts``, ``prepared_content_refs``, ``citations``, and independent
unreviewed ``results``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.ai.vision.coordinates import (
    GENERIC_COORDINATE_PROFILE,
    coordinate_profile_for_model,
)
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    execution_alias_map,
    is_generic_vlm_ocr_engine,
)
from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTRACT_VERSION
from frisket.engine._workers.indra_ocr_model import (
    MODEL_REVISION as INDRA_MODEL_REVISION,
    PACKAGE_VERSION as INDRA_PACKAGE_VERSION,
)
from frisket.engine._workers.parakeet_model import (
    MODEL as PARAKEET_MODEL,
    MODEL_REVISION as PARAKEET_MODEL_REVISION,
    VAD_REVISION as PARAKEET_VAD_REVISION,
)
from frisket.engine._workers.rapidocr_models import RAPIDOCR_DEFAULT_RECOGNIZER_SHA256
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.targets import CAPABILITY_OCR, CAPABILITY_TRANSCRIBE

logger = logging.getLogger("frisket.extraction_cache")

EXTRACTION_CACHE_KEY_VERSION = 1
EXTRACTION_CACHE_SOURCE = "project_extraction_cache"
CACHE_DB_FILENAME = "project.cache.db"
OCR_ADAPTER_VERSION = "1"

_OCR_ALIASES = execution_alias_map(OCR_ENGINE_TABLE)
_TRANSCRIBE_ALIASES = execution_alias_map(TRANSCRIBE_ENGINE_TABLE)
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
class ExtractionTarget:
    """Capability, engine, transport, and resolved engine version for cache keying."""

    capability: str
    engine: str
    transport: str | None = None
    engine_version: str | None = None

    def resolved_engine(self) -> str:
        if self.capability == CAPABILITY_OCR:
            return _OCR_ALIASES.get(self.engine, self.engine)
        if self.capability == CAPABILITY_TRANSCRIBE:
            return _TRANSCRIBE_ALIASES.get(self.engine, self.engine)
        raise ValueError(
            f"unsupported extraction cache capability: {self.capability!r}"
        )

    def resolved_version(self) -> str:
        if self.engine_version is not None:
            return self.engine_version
        canonical = self.resolved_engine()
        if self.capability == CAPABILITY_OCR:
            return ocr_engine_version(canonical, transport=self.transport)
        if self.capability == CAPABILITY_TRANSCRIBE:
            return transcribe_engine_version(canonical, transport=self.transport)
        raise ValueError(
            f"unsupported extraction cache capability: {self.capability!r}"
        )


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


def effective_cache_mode(ctx_or_router: Any) -> str:
    """Resolve the active cache mode from an ActionContext, router, or mode string."""
    if isinstance(ctx_or_router, str):
        return ctx_or_router
    extras = getattr(ctx_or_router, "extras", None)
    router = extras.get("router") if isinstance(extras, Mapping) else ctx_or_router
    mode = getattr(router, "cache_mode", None)
    return str(mode) if isinstance(mode, str) and mode else "replay"


def cache_reads_enabled(cache_mode: str | None) -> bool:
    """Whether extraction cache reads are allowed under ``cache_mode``."""
    return (cache_mode or "replay") in {"replay", "replay_strict"}


def cache_writes_enabled(cache_mode: str | None) -> bool:
    """Whether extraction cache writes are allowed under ``cache_mode``."""
    return (cache_mode or "replay") in {"replay", "fresh"}


def page_selector_for(selected_page: int | None) -> str:
    """Canonical page selector string for a whole document or single page."""
    if selected_page is None:
        return "all"
    if type(selected_page) is not int or selected_page < 1:
        raise ValueError(f"invalid page selector: {selected_page!r}")
    return f"page:{selected_page}"


def normalized_options_hash(options: Mapping[str, Any]) -> str:
    """SHA-256 digest over canonical JSON of normalized action options."""
    canonical = json.dumps(
        dict(options),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ocr_engine_version(engine: str, *, transport: str | None = None) -> str:
    """Deterministic engine/model version token for OCR cache keying."""
    canonical = _OCR_ALIASES.get(engine, engine)
    if canonical == "rapidocr":
        base = (
            f"rapidocr:v{OCR_ADAPTER_VERSION}:{RAPIDOCR_DEFAULT_RECOGNIZER_SHA256[:16]}"
        )
    elif canonical == "indra-ocr":
        base = (
            f"indra-ocr:v{OCR_ADAPTER_VERSION}:"
            f"pkg-{INDRA_PACKAGE_VERSION}:{INDRA_MODEL_REVISION[:16]}"
        )
    elif canonical == "tesseract":
        base = f"tesseract:v{OCR_ADAPTER_VERSION}"
    elif canonical == "datalab":
        base = f"datalab:v{OCR_ADAPTER_VERSION}"
    elif canonical.startswith("opendocrouter/"):
        base = f"opendocrouter:v{OCR_ADAPTER_VERSION}:{canonical}"
    elif is_generic_vlm_ocr_engine(canonical):
        profile = coordinate_profile_for_model(canonical) or GENERIC_COORDINATE_PROFILE
        base = (
            f"vlm:v{OCR_ADAPTER_VERSION}:{canonical}:"
            f"{profile.profile_id}:{profile.prompt_version}"
        )
    else:
        base = f"ocr:v{OCR_ADAPTER_VERSION}:{canonical}"
    return f"{base}@transport:{transport}" if transport else base


def transcribe_engine_version(engine: str, *, transport: str | None = None) -> str:
    """Deterministic engine/model version token for transcription cache keying."""
    canonical = _TRANSCRIBE_ALIASES.get(engine, engine)
    if canonical == "parakeet-tdt":
        base = (
            f"parakeet:v{TRANSCRIPTION_CONTRACT_VERSION}:{PARAKEET_MODEL}:"
            f"{PARAKEET_MODEL_REVISION[:16]}:{PARAKEET_VAD_REVISION[:16]}"
        )
    elif canonical == "faster_whisper":
        base = f"faster_whisper:v{TRANSCRIPTION_CONTRACT_VERSION}"
    else:
        base = f"transcribe:v{TRANSCRIPTION_CONTRACT_VERSION}:{canonical}"
    return f"{base}@transport:{transport}" if transport else base


def build_extraction_cache_key(
    target: ExtractionTarget,
    *,
    blob_hash: str,
    normalized_options: Mapping[str, Any],
    page_selector: str = "all",
    cache_key_version: int = EXTRACTION_CACHE_KEY_VERSION,
) -> ExtractionCacheKey:
    """Build a strict, versioned extraction cache key."""
    if not isinstance(blob_hash, str) or not blob_hash:
        raise ValueError("blob_hash must be a non-empty string")
    engine = target.resolved_engine()
    engine_version = target.resolved_version()
    if not isinstance(engine, str) or not engine:
        raise ValueError("engine must be a non-empty string")
    if not isinstance(engine_version, str) or not engine_version:
        raise ValueError("engine_version must be a non-empty string")
    options_hash = normalized_options_hash(normalized_options)
    canonical = json.dumps(
        {
            "cache_key_version": int(cache_key_version),
            "blob_hash": blob_hash,
            "page_selector": page_selector,
            "capability": target.capability,
            "engine": engine,
            "engine_version": engine_version,
            "options_hash": options_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ExtractionCacheKey(
        cache_key_version=int(cache_key_version),
        blob_hash=blob_hash,
        page_selector=page_selector,
        capability=target.capability,
        engine=engine,
        engine_version=engine_version,
        options_hash=options_hash,
        digest=digest,
    )


def validate_ocr_cache_payload(
    payload: Any, *, selected_page: int | None = None
) -> dict[str, Any] | None:
    """Validate a cached OCR payload; return normalized dict or ``None`` if invalid."""
    if not isinstance(payload, dict):
        return None
    pages = payload.get("pages")
    page_images = payload.get("page_images")
    warnings = payload.get("warnings")
    page_count = payload.get("page_count")
    if (
        not isinstance(pages, list)
        or not pages
        or not all(isinstance(page, dict) for page in pages)
        or any(page.get("error") for page in pages)
    ):
        return None
    if selected_page is not None and len(pages) != 1:
        return None
    if not isinstance(page_images, dict):
        return None
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        return None
    if type(page_count) is not int or page_count <= 0:
        return None
    return {
        "pages": pages,
        "page_images": page_images,
        "warnings": warnings,
        "page_count": page_count,
    }


def validate_transcribe_cache_payload(payload: Any) -> dict[str, Any] | None:
    """Validate a cached transcription payload; return dict or ``None`` if invalid."""
    if not isinstance(payload, dict):
        return None
    text = payload.get("text")
    segments = payload.get("segments")
    language = payload.get("language")
    duration = payload.get("duration")
    if not isinstance(text, str):
        return None
    if not isinstance(segments, list) or not all(
        isinstance(segment, dict) for segment in segments
    ):
        return None
    if language is not None and not isinstance(language, str):
        return None
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
    ):
        return None
    return {
        "text": text,
        "segments": segments,
        "language": language,
        "duration": duration,
    }


def _sidecar_has_symlink(path: Path) -> bool:
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
        if _sidecar_has_symlink(self.path):
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


def admitted_cached_row_ids_from_promises(promises: Any) -> frozenset[int]:
    """Read cache-discounted row ids bound inside a promise set's ``cost`` promise."""
    if not promises:
        return frozenset()
    for promise in promises:
        row = promise.to_row() if hasattr(promise, "to_row") else promise
        if not isinstance(row, Mapping) or str(row.get("field")) != "cost":
            continue
        basis = row.get("basis")
        if not isinstance(basis, Mapping):
            continue
        raw_ids = basis.get("cached_row_ids")
        if not isinstance(raw_ids, list):
            continue
        parsed: set[int] = set()
        for item in raw_ids:
            if type(item) is int and item > 0:
                parsed.add(item)
        return frozenset(parsed)
    return frozenset()


def admitted_cached_row_ids(admission: Any) -> frozenset[int]:
    """Read cache-discounted row ids from a ``RoutedAdmission``."""
    promise_set = getattr(admission, "promise_set", None)
    promises = getattr(promise_set, "promises", None)
    return admitted_cached_row_ids_from_promises(promises)


def admitted_has_paid_cost_promise(admission: Any) -> bool:
    """Whether an admitted route carries any ``cost`` promise (non-free route)."""
    promise_set = getattr(admission, "promise_set", None)
    promises = getattr(promise_set, "promises", None)
    if not promises:
        return False
    for promise in promises:
        row = promise.to_row() if hasattr(promise, "to_row") else promise
        if isinstance(row, Mapping) and str(row.get("field")) == "cost":
            return True
    return False


def _normalized_options_for_target(
    target: ExtractionTarget, spec: Mapping[str, Any]
) -> dict[str, Any] | None:
    canonical = target.resolved_engine()
    options_type = (
        OcrOptions if target.capability == CAPABILITY_OCR else TranscriptionOptions
    )
    raw = {key: spec[key] for key in options_type.model_fields if key in spec}
    try:
        return options_type.model_validate(raw).normalize(canonical)
    except (TypeError, ValueError):
        return None


def _row_media_is_cache_hit(
    cache: BlobExtractionCache,
    blobs: MediaBlobStore,
    target: ExtractionTarget,
    options: Mapping[str, Any],
    media: Any,
) -> bool:
    digest = media.get("blob") if isinstance(media, dict) else None
    if not isinstance(digest, str) or not digest:
        return False
    selected_page: int | None = None
    if target.capability == CAPABILITY_OCR:
        raw_page = media.get("page")
        if raw_page is not None:
            if type(raw_page) is not int or raw_page < 1:
                return False
            selected_page = raw_page
    elif media.get("start_ms") is not None or media.get("end_ms") is not None:
        return False
    if blobs.blob_row(digest) is None:
        return False
    key = build_extraction_cache_key(
        target,
        blob_hash=digest,
        normalized_options=options,
        page_selector=page_selector_for(selected_page),
    )
    payload = cache.get(key)
    if target.capability == CAPABILITY_OCR:
        return (
            validate_ocr_cache_payload(payload, selected_page=selected_page) is not None
        )
    return validate_transcribe_cache_payload(payload) is not None


def _non_empty_row_candidates(
    source_names: Sequence[str],
    values_by_col: Mapping[str, Mapping[int, Any]],
    row_id: int,
) -> list[Any]:
    candidates: list[Any] = []
    for name in source_names:
        col_values = values_by_col.get(name)
        if col_values is None:
            continue
        value = col_values.get(row_id)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, (dict, list, tuple)) and not value:
            continue
        candidates.append(value)
    return candidates


def _source_columns_for_spec(spec: Mapping[str, Any], program: Any) -> list[str]:
    if program is not None:
        return [str(name) for name in program.source_columns(dict(spec))]
    raw = spec.get("input_columns")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [str(name) for name in raw if isinstance(name, str) and name]
    for key in ("source", "input_column", "source_column"):
        val = spec.get(key)
        if isinstance(val, str) and val:
            return [val]
    return []


def probe_extraction_cache_rows(
    project: Any,
    target: ExtractionTarget,
    *,
    spec: Mapping[str, Any],
    program: Any,
    row_ids: Sequence[int],
) -> tuple[frozenset[int], frozenset[int]]:
    """Partition admitted rows into exact-key ``(cached_row_ids, live_row_ids)``.

    Empty source cells appear in neither set. Any row whose exact key cannot be
    proved fails closed into ``live_row_ids``.
    """
    options = _normalized_options_for_target(target, spec)
    source_names = _source_columns_for_spec(spec, program)
    if options is None or not source_names:
        return frozenset(), frozenset(int(r) for r in row_ids)

    sheet_id = int(spec["sheet_id"])
    col_map = {str(c["name"]): int(c["id"]) for c in project.columns(sheet_id)}
    int_row_ids = [int(r) for r in row_ids]
    values_by_col = {
        name: project.get_values(sheet_id, col_map[name], row_ids=int_row_ids)
        for name in source_names
        if name in col_map
    }
    sidecar = cache_db_path(project)
    readable = (
        sidecar is not None and sidecar.exists() and not _sidecar_has_symlink(sidecar)
    )
    blobs = MediaBlobStore(project) if readable else None
    cached: set[int] = set()
    live: set[int] = set()

    cache = BlobExtractionCache(sidecar) if readable and sidecar is not None else None
    try:
        for row_id in int_row_ids:
            candidates = _non_empty_row_candidates(source_names, values_by_col, row_id)
            if not candidates:
                continue
            if cache is None or blobs is None or len(candidates) != 1:
                live.add(row_id)
                continue
            if _row_media_is_cache_hit(cache, blobs, target, options, candidates[0]):
                cached.add(row_id)
            else:
                live.add(row_id)
    finally:
        if cache is not None:
            cache.close()
    return frozenset(cached), frozenset(live)


def verify_consented_extraction_cache(
    project: Any,
    target: ExtractionTarget,
    *,
    spec: Mapping[str, Any],
    program: Any,
    cached_row_ids: Sequence[int],
) -> bool:
    """Return ``True`` iff every cache-discounted row still hits cache."""
    if not cached_row_ids or target.capability not in {
        CAPABILITY_OCR,
        CAPABILITY_TRANSCRIBE,
    }:
        return True
    still_cached, _still_live = probe_extraction_cache_rows(
        project,
        target,
        spec=spec,
        program=program,
        row_ids=cached_row_ids,
    )
    return set(cached_row_ids).issubset(still_cached)

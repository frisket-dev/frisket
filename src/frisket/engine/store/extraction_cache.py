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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from frisket.engine.store.extraction_cache_store import (
    CACHE_DB_FILENAME as CACHE_DB_FILENAME,
    EXTRACTION_CACHE_KEY_VERSION as EXTRACTION_CACHE_KEY_VERSION,
    EXTRACTION_CACHE_SCHEMA as EXTRACTION_CACHE_SCHEMA,
    EXTRACTION_CACHE_SOURCE as EXTRACTION_CACHE_SOURCE,
    BlobExtractionCache as BlobExtractionCache,
    ExtractionCacheKey as ExtractionCacheKey,
    cache_db_path as cache_db_path,
    delete_extraction_cache_for_blobs as delete_extraction_cache_for_blobs,
    sidecar_has_symlink,
)
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.execution.targets import CAPABILITY_OCR, CAPABILITY_TRANSCRIBE

OCR_ADAPTER_VERSION = "1"

_OCR_ALIASES = execution_alias_map(OCR_ENGINE_TABLE)
_TRANSCRIBE_ALIASES = execution_alias_map(TRANSCRIBE_ENGINE_TABLE)


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
        sidecar is not None and sidecar.exists() and not sidecar_has_symlink(sidecar)
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

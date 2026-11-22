"""Row-bound transcription over the shared local/gateway/provider adapters."""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

from frisket.actions.media_options import TranscriptionOptions
from frisket.actions.media_types import AudioColumn, TranscribedMedia
from frisket.actions.types import ColumnRef, Row, RowError
from frisket.ai.llm.types import provider_from_model_id
from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.transcription_language import transcribe_language_declaration
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.extraction_cache import (
    EXTRACTION_CACHE_SOURCE,
    BlobExtractionCache,
    ExtractionCacheKey,
    ExtractionTarget,
    admitted_cached_row_ids,
    admitted_has_paid_cost_promise,
    build_extraction_cache_key,
    cache_reads_enabled,
    cache_writes_enabled,
    effective_cache_mode,
    validate_transcribe_cache_payload,
)
from frisket.engine.store.media_blobs import MediaBlobStore, update_blob_metadata
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.execution.attempt import cache_hit_units, routed_admission_in_scope
from frisket.execution.provider import enforce_media_duration_limit
from frisket.execution.resolve_for_action import authored_options
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.execution.targets import CAPABILITY_TRANSCRIBE
from frisket.ops.base import ActionProgramInvocationHalt, materialize_media_path
from frisket.sdk.ops import transcribe_engines


def _transcribe_cache_hit_model_call(
    engine: str, *, transport: str | None, admission: Any, out: dict[str, Any]
) -> dict[str, Any]:
    if transport == "local":
        provider = "local-onnx" if engine == "parakeet-tdt" else "local"
        provider_kind = "local_process"
        model_ids = [engine]
    elif transport == "frisket.transcription.v1":
        provider = "frisket-sidecar"
        provider_kind = "local_http"
        model_ids = [engine]
    else:
        provider = provider_from_model_id(engine)
        _, _, model = engine.partition("/")
        provider_kind = "platform_api"
        model_ids = [model or engine]
    call = ModelCallMeta.cache_hit(
        capability=CAPABILITY_TRANSCRIBE,
        engine=engine,
        provider=provider,
        provider_kind=provider_kind,
        model_ids=model_ids,
        units=cache_hit_units({"audio_seconds": 0.0}, admission),
        cache={"hit": True, "source": EXTRACTION_CACHE_SOURCE},
    ).as_dict()
    if admission is not None:
        call = bind_fact_to_route(admission.route, call, out)
    return call


class AdmittedTranscriber:
    def __init__(self, ctx, *, engine: str | None, options: dict) -> None:
        self._ctx = ctx
        self._engine = transcribe_engines.canonical_transcription_engine(
            engine or transcribe_engines.DEFAULT_ENGINE
        )
        self._options = copy.deepcopy(options)
        self._spec = {**self._options, "engine": self._engine}
        self._resources = AsyncExitStack()
        self._started = False
        self._closed = False
        self._called_rows: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self.calls_by_row: dict[int, list[dict[str, Any]]] = {}
        self.accounting_by_row: dict[int, dict[str, Any]] = {}

    async def start(self, *, expected_rows: int) -> None:
        if self._started or self._closed:
            raise RuntimeError("transcriber invocation is already started or closed")
        self._selected_transport(self._ctx)
        await self._resources.enter_async_context(
            transcribe_engines.transcription_execution_scope(
                self._spec, self._ctx, expected_rows=expected_rows
            )
        )
        self._started = True

    async def aclose(self) -> None:
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            await _settle(task)
        await self._resources.aclose()

    def _check_active(self, ctx) -> None:
        if self._closed or not self._started:
            raise RuntimeError("transcriber requires an active invocation")
        cancelled = ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            raise asyncio.CancelledError

    def _selected_transport(self, ctx) -> str | None:
        admission = routed_admission_in_scope(ctx.extras)
        preview = preview_resolution_in_scope(ctx.extras)
        if admission is not None and (
            admission.route.engine != self._engine
            or admission.route.target_snapshot.get("capability")
            != CAPABILITY_TRANSCRIBE
            or admission.route.options
            != authored_options(self._spec, CAPABILITY_TRANSCRIBE)
        ):
            raise ActionProgramInvocationHalt(
                "promise_violation", "Transcriber requires its admitted engine route."
            )
        if preview is not None and (
            preview.facts.engine != self._engine
            or preview.support.capability != CAPABILITY_TRANSCRIBE
        ):
            raise ActionProgramInvocationHalt(
                "promise_violation", "Transcriber requires its resolved preview engine."
            )
        transport = transcribe_engines.transcription_transport(
            self._engine, self._spec, ctx
        )
        if admission is None and preview is None and transport != "local":
            raise ActionProgramInvocationHalt(
                "promise_violation",
                "Transcription provider requires its admitted route.",
            )
        return transport

    def bind_row(self, row: Row, *, sheet_id, row_id, sources, ctx):
        self._check_active(ctx)
        if type(sheet_id) is not int or type(row_id) is not int or row_id <= 0:
            raise RowError(
                "invalid_input_ref", "Transcriber requires its admitted row."
            )
        return _BoundTranscriber(
            self, row, sheet_id, row_id, copy.deepcopy(dict(sources or {})), ctx
        )


class _BoundTranscriber:
    def __init__(self, owner, row, sheet_id, row_id, sources, ctx):
        self._owner = owner
        self._row = row
        self._sheet_id = sheet_id
        self._row_id = row_id
        self._sources = sources
        self._ctx = ctx

    def _source(self, source):
        captured = self._sources.get(source.name)
        if (
            not isinstance(captured, dict)
            or source.name not in self._row.values
            or "value" not in captured
            or canonical_json_hash(source.read(self._row))
            != canonical_json_hash(captured["value"])
        ):
            raise RowError(
                "stale_input", "Audio source differs from its admitted cell."
            )
        column = self._ctx.project.db.execute(
            "SELECT sheet_id,type,hidden FROM columns WHERE id=?",
            (captured.get("column_id"),),
        ).fetchone()
        if (
            column is None
            or column["sheet_id"] != self._sheet_id
            or column["hidden"]
            or column["type"] not in AudioColumn.accepted_column_types
            or column["type"] != captured.get("column_type")
        ):
            raise RowError("invalid_input_ref", "Audio source column is incompatible.")
        return copy.deepcopy(captured)

    async def transcribe(
        self, row: Row, source: ColumnRef[Any], *, options: TranscriptionOptions
    ) -> TranscribedMedia:
        self._owner._check_active(self._ctx)
        task = asyncio.create_task(self._transcribe(row, source, options=options))
        self._owner._tasks.add(task)
        try:
            return await task
        finally:
            await _settle(task)
            self._owner._tasks.discard(task)

    def _cache_lookup(
        self, media: Any, digest: str | None, transport: str | None, cache_mode: str
    ) -> tuple[
        BlobExtractionCache | None, ExtractionCacheKey | None, dict[str, Any] | None
    ]:
        if not (
            isinstance(media, dict)
            and isinstance(digest, str)
            and bool(digest)
            and media.get("start_ms") is None
            and media.get("end_ms") is None
        ):
            return None, None, None
        extraction_cache = BlobExtractionCache.for_project(self._ctx.project)
        cache_key = build_extraction_cache_key(
            ExtractionTarget(
                capability=CAPABILITY_TRANSCRIBE,
                engine=self._owner._engine,
                transport=transport,
            ),
            blob_hash=digest,
            normalized_options=self._owner._options,
        )
        if extraction_cache is None or not cache_reads_enabled(cache_mode):
            return extraction_cache, cache_key, None
        with extraction_cache:
            cached = validate_transcribe_cache_payload(extraction_cache.get(cache_key))
        return extraction_cache, cache_key, cached

    def _store_cache(
        self,
        extraction_cache: BlobExtractionCache | None,
        cache_key: ExtractionCacheKey | None,
        cache_mode: str,
        out: dict[str, Any],
    ) -> None:
        if (
            extraction_cache is None
            or cache_key is None
            or not cache_writes_enabled(cache_mode)
            or self._ctx.extras.get("preview") is True
            or not isinstance(out.get("text"), str)
            or not isinstance(out.get("segments"), list)
        ):
            return
        with extraction_cache:
            extraction_cache.put(
                cache_key,
                {
                    "text": out["text"],
                    "segments": transcribe_engines.segments_with_index(out["segments"]),
                    "language": out.get("language"),
                    "duration": out.get("duration"),
                },
            )

    def _require_live_transcription_allowed(self, admission: Any) -> None:
        if (
            admission is not None
            and admitted_has_paid_cost_promise(admission)
            and self._row_id in admitted_cached_row_ids(admission)
        ):
            raise ActionProgramInvocationHalt(
                "promise_violation",
                "cached transcription entry is no longer available for a "
                "row authorized at $0 from cache; re-estimate and confirm "
                "before calling a paid transcription engine",
            )

    async def _transcribe(
        self, row: Row, source: ColumnRef[Any], *, options: TranscriptionOptions
    ) -> TranscribedMedia:
        owner, ctx = self._owner, self._ctx
        owner._check_active(ctx)
        if row is not self._row or not isinstance(source, ColumnRef):
            raise RowError(
                "invalid_input_ref", "Transcriber requires its admitted source."
            )
        if type(options) is not TranscriptionOptions:
            raise TypeError("Transcriber requires TranscriptionOptions")
        if options.normalize(owner._engine) != owner._options:
            raise RowError(
                "invalid_params",
                "Transcription options differ from the admitted options.",
            )
        if self._row_id in owner._called_rows:
            raise RuntimeError("transcriber permits one transcription per row")
        captured = self._source(source)
        media = captured["value"]
        if not media:
            raise RowError("invalid_input_ref", "No media value in the audio source.")
        transport = owner._selected_transport(ctx)
        digest = media.get("blob") if isinstance(media, dict) else None
        probe = (
            MediaBlobStore(ctx.project).probe_metadata(str(digest)) if digest else {}
        )
        duration = probe.get("duration_seconds")
        if duration is None and digest:
            probe = update_blob_metadata(ctx.project, str(digest))
            duration = probe.get("duration_seconds")
        limits = ctx.execution_limits
        if limits is not None and limits.max_media_seconds is not None:
            enforce_media_duration_limit(limits.max_media_seconds, duration)
        owner._called_rows.add(self._row_id)
        admission = routed_admission_in_scope(ctx.extras)
        cache_mode = effective_cache_mode(ctx)
        extraction_cache, cache_key, cached_payload = self._cache_lookup(
            media, digest if isinstance(digest, str) else None, transport, cache_mode
        )
        if cached_payload is not None:
            out = copy.deepcopy(cached_payload)
            if duration is None and out.get("duration") is not None:
                duration = out["duration"]
            cache_call = _transcribe_cache_hit_model_call(
                owner._engine,
                transport=transport,
                admission=admission,
                out=out,
            )
            owner.accounting_by_row[self._row_id] = {
                "cost": 0.0,
                "model_calls": [cache_call],
            }
        else:
            self._require_live_transcription_allowed(admission)
            cancelled = ctx.extras.get("cancelled")
            should_cancel: Callable[[], bool] | None = (
                (lambda: bool(cancelled())) if callable(cancelled) else None
            )
            with materialize_media_path(media, ctx, action="transcribe") as path:
                owner._check_active(ctx)
                result = await transcribe_engines.run_transcription_engine(
                    owner._engine,
                    str(path),
                    copy.deepcopy(owner._spec),
                    ctx,
                    should_cancel=should_cancel,
                    transport=transport,
                    media=copy.deepcopy(media),
                )
            # Preserve returned provider facts even if validation or the author fails.
            out = result.output
            owner.accounting_by_row[self._row_id] = {
                "cost": out.get("cost", 0.0),
                "model_calls": copy.deepcopy(list(result.model_calls)),
            }
            self._store_cache(extraction_cache, cache_key, cache_mode, out)
        text = out["text"]
        segments = transcribe_engines.segments_with_index(out["segments"])
        duration_ms = (
            round(duration * 1000)
            if (type(duration) is int or type(duration) is float)
            and math.isfinite(duration)
            and duration >= 0
            else None
        )
        owner.calls_by_row[self._row_id] = [
            copy.deepcopy(
                {
                    "kind": "transcribe_read",
                    "engine": owner._engine,
                    "text": text,
                    "segments": segments,
                    "language": out.get("language") or owner._options.get("language"),
                    "source": {
                        **captured,
                        "sheet_id": self._sheet_id,
                        "row_id": self._row_id,
                        "source_column": source.name,
                        "duration_ms": duration_ms,
                    },
                }
            )
        ]
        return TranscribedMedia.model_validate(
            {
                "text": text,
                "segments": copy.deepcopy(segments),
                "detected_language": out.get("language")
                if transcribe_language_declaration(owner._engine).detects
                else None,
            }
        )

"""Row-bound transcription over the shared local/gateway/provider adapters."""

from __future__ import annotations

import asyncio
import copy
import math
from contextlib import AsyncExitStack
from typing import Any

from frisket.actions.media_options import TranscriptionOptions
from frisket.actions.media_types import AudioColumn, TranscribedMedia
from frisket.actions.types import ColumnRef, Row, RowError
from frisket.contracts.transcription_language import transcribe_language_declaration
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.executor.visual_cuts_read import _settle
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.provider import enforce_media_duration_limit
from frisket.execution.resolve_for_action import authored_options
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.execution.targets import CAPABILITY_TRANSCRIBE
from frisket.ops.base import RecipeInvocationHalt, materialize_media_path
from frisket.sdk.ops import transcribe_engines


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
            raise RecipeInvocationHalt(
                "promise_violation", "Transcriber requires its admitted engine route."
            )
        if preview is not None and (
            preview.facts.engine != self._engine
            or preview.support.capability != CAPABILITY_TRANSCRIBE
        ):
            raise RecipeInvocationHalt(
                "promise_violation", "Transcriber requires its resolved preview engine."
            )
        transport = transcribe_engines.transcription_transport(
            self._engine, self._spec, ctx
        )
        if admission is None and preview is None and transport != "local":
            raise RecipeInvocationHalt(
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
        limits = ctx.execution_limits
        if limits is not None and limits.max_media_seconds is not None:
            enforce_media_duration_limit(limits.max_media_seconds, duration)
        owner._called_rows.add(self._row_id)
        cancelled = ctx.extras.get("cancelled")
        with materialize_media_path(media, ctx, op="transcribe") as path:
            owner._check_active(ctx)
            result = await transcribe_engines.run_transcription_engine(
                owner._engine,
                str(path),
                copy.deepcopy(owner._spec),
                ctx,
                should_cancel=cancelled if callable(cancelled) else None,
                transport=transport,
                media=copy.deepcopy(media),
            )
        # Preserve returned provider facts even if validation or the author fails.
        out = result.output
        owner.accounting_by_row[self._row_id] = {
            "cost": out.get("cost", 0.0),
            "model_calls": copy.deepcopy(list(result.model_calls)),
        }
        text = out["text"]
        segments = transcribe_engines.segments_with_index(out["segments"])
        duration_ms = (
            round(duration * 1000)
            if type(duration) in (int, float)
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
        return TranscribedMedia(
            text=text,
            segments=copy.deepcopy(segments),
            detected_language=out.get("language")
            if transcribe_language_declaration(owner._engine).detects
            else None,
        )

"""Accounted transcription previews over transient dropped media."""

from __future__ import annotations

import hashlib
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from frisket.actions.media import TranscribeParams, transcribe_options
from frisket.engine.executor.table_preview import TablePreviewResult
from frisket.engine.runner.preview import PreviewColumn
from frisket.engine.store import Project
from frisket.execution.provider import enforce_media_duration_limit
from frisket.execution.resolve_for_action import BoundedScratchInput, resolve_for_action
from frisket.execution.resolver import Refusal, ResolvedExecution
from frisket.ops.base import RecipeInvocationHalt, media_suffix
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.media_probe import probe_for_ingest
from frisket.preview.common import ComparePreviewError, str_or_none
from frisket.sdk.ops import transcribe_engines
from frisket.server.services.scratch_action_preview import (
    ScratchActionPreviewPlan,
    ScratchActionRecipe,
    scratch_estimate,
)

DEFAULT_TRANSCRIBE_COMPARE_LIMIT_SECONDS = 600.0


class TranscribeComparePreviewError(ComparePreviewError):
    """Validation/runtime error returned as a preview 400."""


def _positive_limit(value: Any) -> float:
    if value is None:
        return DEFAULT_TRANSCRIBE_COMPARE_LIMIT_SECONDS
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise TranscribeComparePreviewError(
            "invalid_params",
            "time_limit_seconds must be a positive finite number",
            field="time_limit_seconds",
        )
    return float(value)


def paid_transcribe_scratch_plan(
    project: Project,
    *,
    media_bytes: bytes,
    payload: Mapping[str, Any],
    composition: Any,
) -> tuple[ScratchActionPreviewPlan, dict[str, Any]]:
    """Prepare one native transcription candidate over transient bytes."""

    engine = payload.get("engine")
    if not isinstance(engine, str) or not engine.strip():
        raise TranscribeComparePreviewError(
            "invalid_params", "transcribe compare requires one engine", field="engine"
        )
    params_payload = {
        key: payload[key]
        for key in TranscribeParams.model_fields
        if key not in {"source", "engine"} and key in payload
    }
    language = params_payload.get("language")
    if isinstance(language, str):
        params_payload["language"] = [language]
    params_payload.update(source="scratch", engine=engine.strip())
    try:
        params = TranscribeParams.model_validate(params_payload)
        options = transcribe_options(params)
        normalized_options = options.normalize(params.engine.root)
    except (TypeError, ValueError) as exc:
        raise TranscribeComparePreviewError(
            "invalid_params", str(exc), field="payload"
        ) from exc

    limit_seconds = _positive_limit(payload.get("time_limit_seconds"))
    filename = str_or_none(payload.get("filename"))
    mime = str_or_none(payload.get("mime"))
    digest = hashlib.sha256(media_bytes).hexdigest()
    with tempfile.TemporaryDirectory(prefix="frisket-transcribe-probe-") as tmp:
        path = Path(tmp) / f"scratch{media_suffix(filename, mime)}"
        path.write_bytes(media_bytes)
        probe = probe_for_ingest(path, filename=filename, mime=mime, digest=digest)
    duration = probe.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration <= 0
    ):
        raise TranscribeComparePreviewError(
            "invalid_input_ref",
            "Transcribe compare could not measure a positive media duration.",
            field="file",
        )
    duration_seconds = float(duration)
    effective_seconds = min(duration_seconds, limit_seconds)
    selection = {
        "start_seconds": "0",
        "end_seconds": format(effective_seconds, ".15g"),
    }
    source_identity = {"sha256": digest, "selection": selection}
    work_scope = {
        "schema_version": "frisket.scratch-work-scope.v1",
        "identity": digest,
        "selection": selection,
        "source_count": 1,
    }
    spec = {
        "action_kind": "media.transcribe",
        "sheet_id": 0,
        "params": params.model_dump(mode="json", exclude_unset=True),
        "engine": params.engine.root,
        **normalized_options,
    }
    recipe = ScratchActionRecipe("media.transcribe", "transcribe")
    resolved = resolve_for_action(
        project,
        spec,
        recipe,
        composition=composition,
        bounded_scratch_input=BoundedScratchInput(effective_seconds, work_scope),
    )
    if isinstance(resolved, Refusal):
        raise TranscribeComparePreviewError(
            "no_live_target", resolved.remedy, field="engine"
        )
    if not isinstance(resolved, ResolvedExecution):
        raise RuntimeError("transcription scratch resolution returned no route")
    estimate = scratch_estimate(
        resolved, quantity_name="audio_seconds", quantity=effective_seconds
    )

    async def run(context) -> TablePreviewResult:
        if context.cancelled:
            raise RecipeInvocationHalt("local_session_failed", "Preview was cancelled.")
        limits = context.execution_limits
        if limits is not None:
            enforce_media_duration_limit(limits.max_media_seconds, effective_seconds)
        with tempfile.TemporaryDirectory(prefix="frisket-transcribe-scratch-") as tmp:
            source = Path(tmp) / f"source{media_suffix(filename, mime)}"
            source.write_bytes(media_bytes)
            selected = source
            if effective_seconds < duration_seconds:
                from frisket.engine.store.media_clip import cut_clip

                selected = Path(tmp) / f"prefix{media_suffix(filename, mime)}"
                await cut_clip(
                    None,  # type: ignore[arg-type] - scratch has no durable ClipSource
                    source_path=source,
                    start_ms=0,
                    end_ms=round(effective_seconds * 1000),
                    out_path=selected,
                )
            ctx = context.op_context()
            engine_id = transcribe_engines.canonical_transcription_engine(
                params.engine.root
            )
            engine_spec = {**normalized_options, "engine": params.engine.root}
            try:
                async with transcribe_engines.transcription_execution_scope(
                    engine_spec, ctx, expected_rows=1
                ):
                    started = time.perf_counter()
                    result = await transcribe_engines.run_transcription_engine(
                        engine_id,
                        str(selected),
                        engine_spec,
                        ctx,
                        should_cancel=context.cancel_event.is_set,
                        transport=transcribe_engines.transcription_transport(
                            engine_id, engine_spec, ctx
                        ),
                        media={"filename": filename, "mime": mime},
                    )
                    runtime_ms = max(0, round((time.perf_counter() - started) * 1000))
            except HostedEngineError as exc:
                context.write_model_calls(
                    list((exc.accounting or {}).get("model_calls") or [])
                )
                raise
            context.write_model_calls(list(result.model_calls))
            out = result.output
            context.progress(1, 1)
            return TablePreviewResult(
                columns=[
                    PreviewColumn(name="text", column_type="text"),
                    PreviewColumn(name="segments", column_type="json"),
                    PreviewColumn(name="detected_language", column_type="text"),
                    PreviewColumn(name="runtime_ms", column_type="number"),
                ],
                rows=[
                    {
                        "text": {"value": str(out.get("text") or "")},
                        "segments": {
                            "value": transcribe_engines.segments_with_index(
                                out.get("segments") or []
                            )
                        },
                        "detected_language": {"value": out.get("language")},
                        "runtime_ms": {"value": runtime_ms},
                    }
                ],
                total=1,
                warnings=tuple(str(item) for item in (out.get("warnings") or [])),
            )

    return (
        ScratchActionPreviewPlan(
            action_kind="media.transcribe",
            recipe=recipe,
            spec=spec,
            resolved_execution=resolved,
            estimate=estimate,
            work_scope=work_scope,
            source_identity=source_identity,
            run=run,
        ),
        {
            "filename": filename,
            "mime": mime,
            "size": len(media_bytes),
            "duration_seconds": duration_seconds,
            "effective_duration_seconds": effective_seconds,
            "time_limit_seconds": limit_seconds,
        },
    )

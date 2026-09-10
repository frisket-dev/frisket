"""Public transcription adapter facade and route-aware dispatch.

Adapter implementations live in :mod:`frisket.sdk.ops.transcription`; this
module keeps the established SDK imports and selects a declared transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from frisket.contracts.action import (
    project_transcription_engine_options,
    transcribe_engine_capabilities,
)
from frisket.contracts.actions.schemas._engines import (
    TRANSCRIBE_ENGINE_TABLE,
    engine_ids,
    execution_alias_map,
)
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.definitions import build_static_targets
from frisket.execution.resolve_for_action import authored_options
from frisket.execution.resolver import (
    Refusal,
    preferred_static_choice,
    preview_resolution_in_scope,
    target_family,
)
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
from frisket.execution.targets import validated_target_snapshot
from frisket.ops.base import OpContext
from frisket.sdk.ops.transcription.common import (
    LocalTranscribeUnsupportedError as LocalTranscribeUnsupportedError,
    TranscribeCancelled as TranscribeCancelled,
    TranscriptionEngineResult,
    segments_with_index as segments_with_index,
    total_audio_seconds as total_audio_seconds,
)
from frisket.sdk.ops.transcription.faster_whisper import (
    FasterWhisperAdapter,
    faster_whisper_env as faster_whisper_env,
    faster_whisper_policy as faster_whisper_policy,
)
from frisket.sdk.ops.transcription.hosted import OpenAITranscriptionAdapter
from frisket.sdk.ops.transcription.parakeet import ParakeetAdapter, execution_scope
from frisket.sdk.ops.transcription.sidecar import TranscriptionV1Adapter

DEFAULT_ENGINE = "faster_whisper"
LOCAL_ENGINES = engine_ids(TRANSCRIBE_ENGINE_TABLE, tier="local")
ENGINE_CHOICES = list(engine_ids(TRANSCRIBE_ENGINE_TABLE))
ENGINE_ALIASES = execution_alias_map(TRANSCRIBE_ENGINE_TABLE)
_PREFERENCE_TARGETS = build_static_targets()

TranscriptionAdapter = (
    FasterWhisperAdapter
    | ParakeetAdapter
    | OpenAITranscriptionAdapter
    | TranscriptionV1Adapter
)


def _preferred_static_support(spec: dict):
    raw = str(spec.get("engine") or DEFAULT_ENGINE)
    return preferred_static_choice(raw, authored_options(spec), _PREFERENCE_TARGETS)


def preferred_transcription_target_family(spec: dict) -> str | None:
    choice = _preferred_static_support(spec)
    return target_family(choice[0].id) if isinstance(choice, tuple) else None


def canonical_transcription_engine(engine: str) -> str:
    return ENGINE_ALIASES.get(engine, engine)


def transcription_transport(
    engine: str, spec: dict[str, Any], ctx: OpContext | None
) -> str | None:
    """Resolve dispatch from a bound route or declared static preference."""
    admission = routed_admission_in_scope((ctx.extras or {}) if ctx is not None else {})
    preview = preview_resolution_in_scope(ctx.extras) if ctx is not None else None
    if admission is not None:
        snapshot = validated_target_snapshot(admission.route.target_snapshot)
        return str(snapshot["transport"])
    if preview is not None:
        return preview.support.transport
    if "/" in engine:
        return "remote"
    raw = str(spec.get("engine") or engine)
    choice = preferred_static_choice(raw, authored_options(spec), _PREFERENCE_TARGETS)
    if isinstance(choice, Refusal):
        raise ValueError(f"no capable execution target: {choice.remedy}")
    if choice is not None:
        return choice[1].transport
    try:
        return transcribe_engine_capabilities(engine).transport
    except ValueError:
        return None


def validate_transcription_engine_options(
    engine: str, spec: dict[str, Any]
) -> dict[str, Any]:
    return project_transcription_engine_options(engine, spec)


def transcription_max_row_concurrency(spec: dict[str, Any]) -> int | None:
    choice = _preferred_static_support(spec)
    return 1 if isinstance(choice, tuple) and choice[1].run_scoped else None


def transcription_uses_run_scope(spec: dict[str, Any], ctx: OpContext) -> bool:
    admission = routed_admission_in_scope(ctx.extras or {})
    preview = preview_resolution_in_scope(ctx.extras)
    if admission is not None:
        snapshot = validated_target_snapshot(admission.route.target_snapshot)
        return bool(snapshot["run_scoped"])
    if preview is not None:
        return preview.support.run_scoped
    choice = _preferred_static_support(spec)
    return isinstance(choice, tuple) and choice[1].run_scoped


@asynccontextmanager
async def transcription_execution_scope(
    spec: dict[str, Any], ctx: OpContext, *, expected_rows: int
) -> AsyncIterator[None]:
    engine = canonical_transcription_engine(str(spec.get("engine") or DEFAULT_ENGINE))
    enabled = transcription_uses_run_scope(spec, ctx)
    if enabled and engine != "parakeet-tdt":
        raise RuntimeError(f"run-scoped transcription engine {engine!r} has no adapter")
    cancel = (ctx.extras or {}).get("cancelled")
    should_cancel = cancel if callable(cancel) else None
    async with execution_scope(
        enabled=enabled,
        spec=spec,
        expected_rows=expected_rows,
        should_cancel=should_cancel,
    ):
        yield


def _adapter_for(engine: str, transport: str | None) -> TranscriptionAdapter:
    if transport == "local":
        if engine == "faster_whisper":
            return FasterWhisperAdapter()
        if engine == "parakeet-tdt":
            return ParakeetAdapter()
    if transport == "frisket.transcription.v1":
        return TranscriptionV1Adapter()
    if "/" in engine:
        return OpenAITranscriptionAdapter()
    raise ValueError(
        f"unknown transcription engine '{engine}' (use "
        f"{', '.join(repr(item) for item in ENGINE_CHOICES)}, or a provider/model id)"
    )


def transcription_model_calls(
    engine: str,
    path: str,
    spec: dict[str, Any],
    out: dict[str, Any],
    *,
    transport: str | None = None,
) -> list[dict[str, Any]]:
    canonical = canonical_transcription_engine(engine)
    selected_transport = transport or transcription_transport(canonical, spec, None)
    return _adapter_for(canonical, selected_transport).model_calls(
        canonical, path, spec, out
    )


async def run_transcription_engine(
    engine: str,
    path: str,
    spec: dict[str, Any],
    ctx: OpContext,
    *,
    should_cancel: Callable[[], bool] | None = None,
    transport: str | None = None,
    media: Any = None,
) -> TranscriptionEngineResult:
    """Run one materialized file through the shared preview/durable path."""
    canonical = canonical_transcription_engine(engine)
    selected_transport = transport or transcription_transport(canonical, spec, ctx)
    adapter = _adapter_for(canonical, selected_transport)
    if isinstance(adapter, FasterWhisperAdapter | ParakeetAdapter):
        out = await adapter.transcribe(path, spec, should_cancel=should_cancel)
    elif isinstance(adapter, TranscriptionV1Adapter):
        out = await adapter.transcribe(
            canonical, path, spec, ctx, light_engine=DEFAULT_ENGINE
        )
    else:
        out = await adapter.transcribe(canonical, path, spec, ctx, media)
    calls = adapter.model_calls(canonical, path, spec, out)
    admission = routed_admission_in_scope(ctx.extras or {})
    if admission is not None:
        calls = [bind_fact_to_route(admission.route, call, out) for call in calls]
        if any(not call.get(ROUTE_OBSERVATION_KEY) for call in calls):
            raise RuntimeError(
                "transcription fact built under a route carries no binding observation "
                "required by the route-epoch invariant"
            )
    return TranscriptionEngineResult(output=out, model_calls=tuple(calls))

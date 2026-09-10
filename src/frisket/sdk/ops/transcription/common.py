"""Shared transcription result normalization and model-call accounting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.ai.llm.types import provider_from_model_id
from frisket.ai.models.metadata import ModelCallMeta, PROVIDER_KIND
from frisket.contracts.action import project_transcription_engine_options
from frisket.contracts.transcription_sidecar import (
    TranscriptionOptions,
    parse_transcription_response,
)
from frisket.local_model_ids import bare_model_name


class TranscribeCancelled(RuntimeError):
    """A cooperative cancellation stopped transcription before a result."""


class LocalTranscribeUnsupportedError(RuntimeError):
    """An edge-authenticated local endpoint cannot serve audio transcription."""


def total_audio_seconds(rows: list[dict[str, Any]], project: Any) -> float | None:
    """Measure admitted audio from stored metadata, or return unknown."""
    from frisket.engine.runner.row_inputs import is_empty_cell_value

    total = 0.0
    for row in rows:
        candidates = [value for value in row.values() if not is_empty_cell_value(value)]
        if not candidates:
            continue
        if len(candidates) != 1:
            return None
        duration = _duration_from_media(candidates[0], project)
        if duration is None:
            return None
        total += duration
        if not math.isfinite(total):
            return None
    return total


def _duration_from_media(media: Any, project: Any) -> float | None:
    from frisket.engine.store.media_blobs import MediaBlobStore

    if not isinstance(media, dict) or not isinstance(media.get("blob"), str):
        return None
    try:
        duration = (
            MediaBlobStore(project)
            .display_metadata(media["blob"])
            .get("duration_seconds")
        )
        if duration is None or isinstance(duration, bool):
            return None
        seconds = float(duration)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except Exception:  # noqa: BLE001 - an unmeasured input has no quote quantity
        return None


def _segment_with_speaker(segment: dict[str, Any]) -> dict[str, Any]:
    out = {
        "start": round(float(segment["start"]), 2),
        "end": round(float(segment["end"]), 2),
        "text": str(segment["text"]).strip(),
    }
    speaker = segment.get("speaker")
    if speaker is not None and speaker != "":
        out["speaker"] = str(speaker)
        confidence = segment.get("speaker_confidence")
        if confidence:
            out["speaker_confidence"] = str(confidence)
    if "words" in segment and segment["words"] is not None:
        out["words"] = [dict(word) for word in segment["words"]]
    return out


def _spec_language(spec: dict[str, Any]) -> str | None:
    value = spec.get("language")
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def transcription_v1_options(engine: str, spec: dict[str, Any]) -> TranscriptionOptions:
    declared = project_transcription_engine_options(engine, spec)
    option_values = {
        key: value
        for key, value in declared.items()
        if key
        in {
            "language",
            "model_size",
            "vad",
            "diarize",
            "num_speakers",
            "min_speakers",
            "max_speakers",
            "context",
        }
    }
    if "language" in option_values:
        option_values["language"] = _spec_language(
            {"language": option_values["language"]}
        )
    return TranscriptionOptions.model_validate(option_values)


def segments_with_index(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        next_segment = dict(segment)
        next_segment["segment_index"] = index
        normalized.append(next_segment)
    return normalized


def transcription_v1_result(
    body: Any,
    *,
    wire_engine: str,
    requested_options: TranscriptionOptions,
    boundary: str,
) -> dict[str, Any]:
    try:
        response = parse_transcription_response(body)
    except RuntimeError as error:
        if boundary == "local":
            raise RuntimeError(
                str(error).replace("sidecar transcription", "local transcription")
            ) from None
        raise
    if len(response.results) != 1:
        raise RuntimeError(
            f"malformed {boundary} transcription v1 response: expected one result"
        )
    result = response.results[0]
    if result.engine != wire_engine:
        raise RuntimeError(
            f"malformed {boundary} transcription v1 response: "
            f"requested engine '{wire_engine}', received '{result.engine}'"
        )
    accepted_options = requested_options.model_dump(mode="python", exclude_none=True)
    if result.accepted_options != accepted_options:
        raise RuntimeError(
            f"malformed {boundary} transcription v1 response: accepted_options "
            "do not match the request"
        )
    segments = [
        _segment_with_speaker(segment.model_dump(exclude_none=True, mode="json"))
        for segment in result.segments
    ]
    return {
        "text": result.text.strip(),
        "segments": segments_with_index(segments),
        "language": result.language,
        "duration": result.duration,
        "model_ids": list(result.model_ids),
        "revision": result.revision,
        "device": result.device,
        "dtype": result.dtype,
        "timings": dict(result.timings),
        "warnings": list(result.warnings),
        "accepted_options": dict(result.accepted_options),
        "cost": 0.0,
    }


def v1_provenance_units(out: dict[str, Any], units: dict[str, Any]) -> dict[str, Any]:
    for field, unit_name in (
        ("revision", "model_revision"),
        ("device", "device"),
        ("dtype", "dtype"),
    ):
        value = out.get(field)
        if isinstance(value, str) and value:
            units[unit_name] = value
    for name, value in (out.get("timings") or {}).items():
        if isinstance(value, (int, float, str, bool)):
            units[f"timing_{name}"] = value
    for name, value in (out.get("accepted_options") or {}).items():
        if isinstance(value, (int, float, str, bool)):
            units[f"option_{name}"] = value
    return units


def input_units(path: str, out: dict[str, Any]) -> dict[str, Any]:
    units: dict[str, Any] = {}
    try:
        units["input_bytes"] = Path(path).stat().st_size
    except OSError:
        pass
    duration = out.get("duration")
    if duration is not None:
        units["audio_seconds"] = float(duration)
    return units


def provider_model_call(engine: str, path: str, out: dict[str, Any]) -> ModelCallMeta:
    provider = provider_from_model_id(engine)
    model = bare_model_name(engine)
    units = input_units(path, out)
    usage = out.get("usage") or {}
    units.update(
        {
            key: value
            for key, value in usage.items()
            if isinstance(value, (int, float, str, bool))
        }
    )
    units["requests"] = 1
    return ModelCallMeta.provider_call(
        capability="transcribe",
        engine=engine,
        provider=provider,
        provider_kind=PROVIDER_KIND.get(provider, "platform_api"),
        model_ids=[model or engine],
        credential_source=out.get("credential_source", "none"),
        provider_reported_cost_usd=out.get("cost"),
        provider_cost_usd=out.get("cost"),
        units=units,
        cost_source=str(out.get("cost_source") or "unknown"),
        warnings=[]
        if out.get("cost") is not None
        else ["remote provider cost is unknown"],
        duration_ms=None,
    )


@dataclass(frozen=True)
class TranscriptionEngineResult:
    output: dict[str, Any]
    model_calls: tuple[dict[str, Any], ...]

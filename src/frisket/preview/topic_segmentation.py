"""Ephemeral transcript topic-segmentation comparison.

One uploaded TXT/SRT/VTT sample is parsed into immutable dialogue units and
run through requested variants from the same engine dispatch used by the
durable Find topic changes action.  This module has no Project parameter and
imports no stores: it cannot create rows, blobs, runs, receipts, or evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.preview.common import (
    ComparePreviewError,
    str_or_none,
)
from frisket.redaction import safe_error
from frisket.features.temporal.transcript_boundaries import lock_transcript_boundaries
from frisket.features.topic_segmentation.contracts import (
    BetweenUnits,
    BoundaryCandidate,
    DialogueUnit,
    ExactTime,
    SegmentationContext,
    SegmentationResult,
    SegmentationSnapshot,
    WithinUnit,
)
from frisket.features.topic_segmentation.engines import (
    engine_catalog,
    get_segmenter,
)


SCHEMA_VERSION = "frisket.topic_segmentation_compare.v1"
SUPPORTED_SUFFIXES = frozenset({".txt", ".srt", ".vtt"})

_TIMING_RE = re.compile(r"^(?P<start>\S+)\s+-->\s+(?P<end>\S+)(?:\s+.*)?$")
_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>[0-5]?\d):"
    r"(?P<seconds>[0-5]?\d)(?:[,.](?P<millis>\d{1,3}))?$"
)
_TAG_RE = re.compile(r"<[^>]+>")
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]+)>", re.IGNORECASE)


class TopicSegmentationCompareError(ComparePreviewError):
    """Invalid scratch input returned as a compare-preview 400."""


@dataclass(frozen=True, slots=True)
class TopicSegmentationVariantRequest:
    id: str
    engine: str
    settings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TopicSegmentationScratchRequest:
    variants: tuple[TopicSegmentationVariantRequest, ...]
    filename: str
    mime: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedScratchTranscript:
    filename: str
    mime: str | None
    size: int
    snapshot: SegmentationSnapshot


def _text_or_error(value: Any, *, field: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TopicSegmentationCompareError(
            "invalid_params",
            message,
            field=field,
        )
    return value.strip()


def _coerce_request(
    request: TopicSegmentationScratchRequest | Mapping[str, Any],
) -> TopicSegmentationScratchRequest:
    if isinstance(request, TopicSegmentationScratchRequest):
        req = request
    else:
        filename = _text_or_error(
            request.get("filename"),
            field="file",
            message="Topic Compare requires a .txt, .srt, or .vtt filename.",
        )
        variants_raw = request.get("variants")
        if not isinstance(variants_raw, list) or not variants_raw:
            raise TopicSegmentationCompareError(
                "invalid_params",
                "Topic Compare requires at least one engine variant.",
                field="variants",
            )
        variants: list[TopicSegmentationVariantRequest] = []
        for index, raw in enumerate(variants_raw):
            if not isinstance(raw, Mapping):
                raise TopicSegmentationCompareError(
                    "invalid_params",
                    "Each Topic Compare variant must be an object.",
                    field=f"variants[{index}]",
                )
            variant_id = _text_or_error(
                raw.get("id"),
                field=f"variants[{index}].id",
                message="Each Topic Compare variant needs a stable id.",
            )
            engine = _text_or_error(
                raw.get("engine"),
                field=f"variants[{index}].engine",
                message="Each Topic Compare variant needs an engine id.",
            )
            settings = raw.get("settings", {})
            if not isinstance(settings, dict):
                raise TopicSegmentationCompareError(
                    "invalid_params",
                    "Topic Compare variant settings must be an object.",
                    field=f"variants[{index}].settings",
                )
            variants.append(
                TopicSegmentationVariantRequest(
                    id=variant_id,
                    engine=engine,
                    settings=dict(settings),
                )
            )
        req = TopicSegmentationScratchRequest(
            variants=tuple(variants),
            filename=filename,
            mime=str_or_none(request.get("mime")),
            language=str_or_none(request.get("language")),
        )

    if Path(req.filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            "Topic Compare accepts .txt, .srt, and .vtt uploads only.",
            field="file",
            details={"filename": req.filename},
        )
    if not req.variants:
        raise TopicSegmentationCompareError(
            "invalid_params",
            "Topic Compare requires at least one engine variant.",
            field="variants",
        )
    ids = [variant.id for variant in req.variants]
    if len(ids) != len(set(ids)):
        raise TopicSegmentationCompareError(
            "invalid_params",
            "Topic Compare variant ids must be unique.",
            field="variants",
        )
    return req


def _decode(upload: bytes) -> str:
    if not upload:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            "Topic Compare cannot analyze an empty transcript.",
            field="file",
        )
    try:
        return upload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            "Topic Compare transcript files must be UTF-8 text.",
            field="file",
        ) from exc


def _timestamp_ms(value: str, *, block: int) -> int:
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            f"Transcript cue {block} has an invalid timestamp.",
            field="file",
        )
    millis_text = match.group("millis") or "0"
    return (
        (int(match.group("hours") or 0) * 3_600_000)
        + (int(match.group("minutes")) * 60_000)
        + (int(match.group("seconds")) * 1_000)
        + int(millis_text.ljust(3, "0"))
    )


def _cue_timing(line: str, *, block: int) -> tuple[int, int]:
    match = _TIMING_RE.fullmatch(line.strip())
    if match is None:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            f"Transcript cue {block} has no valid timestamp line.",
            field="file",
        )
    start_ms = _timestamp_ms(match.group("start"), block=block)
    end_ms = _timestamp_ms(match.group("end"), block=block)
    if start_ms >= end_ms:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            f"Transcript cue {block} must end after it starts.",
            field="file",
        )
    return start_ms, end_ms


def _cue_content(lines: list[str], *, block: int) -> tuple[str, str | None]:
    raw = "\n".join(line.strip() for line in lines).strip()
    if not raw:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            f"Transcript cue {block} has no text.",
            field="file",
        )
    voice = _VOICE_RE.search(raw)
    speaker = html.unescape(voice.group(1)).strip() if voice is not None else None
    text = html.unescape(_TAG_RE.sub("", raw)).strip()
    if not text:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            f"Transcript cue {block} has no readable text.",
            field="file",
        )
    return text, speaker or None


def _blocks(text: str) -> list[list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [
        block.split("\n")
        for block in re.split(r"\n[ \t]*\n+", normalized.strip())
        if block.strip()
    ]


def _parse_txt(text: str) -> tuple[DialogueUnit, ...]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return tuple(
        DialogueUnit(id=f"unit-{index:06d}", ordinal=index, text=line)
        for index, line in enumerate(lines)
    )


def _parse_srt(text: str) -> tuple[DialogueUnit, ...]:
    units: list[DialogueUnit] = []
    for block_number, raw_lines in enumerate(_blocks(text), start=1):
        lines = [line.strip("\ufeff") for line in raw_lines]
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) < 2:
            raise TopicSegmentationCompareError(
                "invalid_input_ref",
                f"Transcript cue {block_number} is incomplete.",
                field="file",
            )
        start_ms, end_ms = _cue_timing(lines[0], block=block_number)
        cue_text, speaker = _cue_content(lines[1:], block=block_number)
        ordinal = len(units)
        units.append(
            DialogueUnit(
                id=f"cue-{ordinal:06d}",
                ordinal=ordinal,
                text=cue_text,
                speaker=speaker,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return tuple(units)


def _vtt_body(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or not lines[0].lstrip("\ufeff").startswith("WEBVTT"):
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            "A .vtt upload must begin with WEBVTT.",
            field="file",
        )
    if len(lines) == 1:
        return ""
    # WEBVTT permits header metadata before the first blank line. If the next
    # line is already a timestamp, accept the common missing-blank variant.
    if "-->" in lines[1]:
        return "\n".join(lines[1:])
    index = 1
    while index < len(lines) and lines[index].strip():
        index += 1
    return "\n".join(lines[index + 1 :])


def _parse_vtt(text: str) -> tuple[DialogueUnit, ...]:
    units: list[DialogueUnit] = []
    for block_number, raw_lines in enumerate(_blocks(_vtt_body(text)), start=1):
        lines = [line.strip("\ufeff") for line in raw_lines]
        if not lines:
            continue
        if re.match(r"^(NOTE|STYLE|REGION)(?:\s|$)", lines[0].lstrip().upper()):
            continue
        timing_index = 0 if "-->" in lines[0] else 1
        if timing_index >= len(lines) or len(lines) <= timing_index + 1:
            raise TopicSegmentationCompareError(
                "invalid_input_ref",
                f"Transcript cue {block_number} is incomplete.",
                field="file",
            )
        start_ms, end_ms = _cue_timing(lines[timing_index], block=block_number)
        cue_text, speaker = _cue_content(lines[timing_index + 1 :], block=block_number)
        ordinal = len(units)
        units.append(
            DialogueUnit(
                id=f"cue-{ordinal:06d}",
                ordinal=ordinal,
                text=cue_text,
                speaker=speaker,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return tuple(units)


def _snapshot_hash(
    source_kind: str,
    language: str | None,
    units: tuple[DialogueUnit, ...],
) -> str:
    canonical = json.dumps(
        {
            "schema_version": "frisket.topic_compare.snapshot.v1",
            "source_kind": source_kind,
            "language": language,
            "units": [
                {
                    "id": unit.id,
                    "ordinal": unit.ordinal,
                    "text": unit.text,
                    "speaker": unit.speaker,
                    "start_ms": unit.start_ms,
                    "end_ms": unit.end_ms,
                }
                for unit in units
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def parse_scratch_transcript(
    transcript_bytes: bytes,
    *,
    filename: str,
    mime: str | None = None,
    language: str | None = None,
) -> ParsedScratchTranscript:
    """Parse one upload without resolving or persisting any project state."""

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            "Topic Compare accepts .txt, .srt, and .vtt uploads only.",
            field="file",
            details={"filename": filename},
        )
    text = _decode(transcript_bytes)
    if suffix == ".txt":
        units = _parse_txt(text)
        source_kind = "untimed_transcript"
    elif suffix == ".srt":
        units = _parse_srt(text)
        source_kind = "timestamped_transcript"
    else:
        units = _parse_vtt(text)
        source_kind = "timestamped_transcript"

    if not units:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            "Topic Compare found no transcript units in this file.",
            field="file",
        )
    try:
        snapshot = SegmentationSnapshot(
            snapshot_hash=_snapshot_hash(source_kind, language, units),
            source_kind=source_kind,  # type: ignore[arg-type]
            language=language,
            units=units,
        )
    except ValueError as exc:
        raise TopicSegmentationCompareError(
            "invalid_input_ref",
            f"Transcript units are not in valid source order: {exc}",
            field="file",
        ) from exc
    return ParsedScratchTranscript(
        filename=filename,
        mime=mime,
        size=len(transcript_bytes),
        snapshot=snapshot,
    )


def _locator_payload(candidate: BoundaryCandidate) -> dict[str, Any]:
    locator = candidate.locator
    if isinstance(locator, BetweenUnits):
        return {
            "kind": locator.kind,
            "left_unit_id": locator.left_unit_id,
            "right_unit_id": locator.right_unit_id,
        }
    if isinstance(locator, WithinUnit):
        return {"kind": locator.kind, "unit_id": locator.unit_id}
    assert isinstance(locator, ExactTime)
    return {"kind": locator.kind, "at_ms": locator.at_ms}


def _untimed_layout(
    snapshot: SegmentationSnapshot,
    candidates: tuple[BoundaryCandidate, ...],
) -> tuple[
    dict[str, tuple[int, str]],
    list[dict[str, Any]],
    tuple[tuple[str, ...], ...],
]:
    """Project untimed Compare results on source order only.

    Durable topic ranges cannot exist without timestamps.  This fallback is
    consequently limited to unit-addressed locators; all timed inputs use the
    production locking path below.
    """

    by_id = {unit.id: (position, unit) for position, unit in enumerate(snapshot.units)}
    observations: dict[str, tuple[int, str]] = {}
    for candidate in candidates:
        locator = candidate.locator
        if isinstance(locator, BetweenUnits):
            left = by_id.get(locator.left_unit_id)
            right = by_id.get(locator.right_unit_id)
            if left is None or right is None:
                raise ValueError("between_units references an unknown transcript unit")
            if right[0] != left[0] + 1:
                raise ValueError(
                    "between_units references non-adjacent transcript units"
                )
            observations[candidate.id] = (right[1].ordinal, "point")
        elif isinstance(locator, WithinUnit):
            resolved = by_id.get(locator.unit_id)
            if resolved is None:
                raise ValueError("within_unit references an unknown transcript unit")
            observations[candidate.id] = (resolved[1].ordinal, "span")
        else:
            assert isinstance(locator, ExactTime)
            raise ValueError("exact_time cannot address an untimed transcript")

    position_by_ordinal = {
        unit.ordinal: position for position, unit in enumerate(snapshot.units)
    }
    grouped: dict[int, list[tuple[str, str]]] = {}
    for candidate_id, (key, kind) in observations.items():
        grouped.setdefault(key, []).append((candidate_id, kind))

    canonical: list[dict[str, Any]] = []
    for key in sorted(grouped, key=position_by_ordinal.__getitem__):
        values = grouped[key]
        kind = "span" if any(value[1] == "span" for value in values) else "point"
        position = position_by_ordinal[key]
        if kind == "point" and position == 0:
            raise ValueError(
                "a point boundary cannot precede the first transcript unit"
            )
        canonical.append(
            {
                "key": key,
                "kind": kind,
                "candidate_ids": sorted(value[0] for value in values),
            }
        )

    sections: list[tuple[str, ...]] = []

    def append_section(start: int, end: int) -> None:
        if start < 0 or end < start or end >= len(snapshot.units):
            raise ValueError("canonical boundaries produced an empty topic section")
        sections.append(tuple(unit.id for unit in snapshot.units[start : end + 1]))

    start_position = 0
    for boundary in canonical:
        position = position_by_ordinal[int(boundary["key"])]
        end_position = position if boundary["kind"] == "span" else position - 1
        append_section(start_position, end_position)
        start_position = position
    append_section(start_position, len(snapshot.units) - 1)
    return observations, canonical, tuple(sections)


def _membership_payload(
    snapshot: SegmentationSnapshot,
    section_unit_ids: tuple[tuple[str, ...], ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    membership_by_unit = {unit.id: [] for unit in snapshot.units}
    sections = []
    for index, unit_ids in enumerate(section_unit_ids):
        sections.append({"index": index, "unit_ids": list(unit_ids)})
        for unit_id in unit_ids:
            membership_by_unit[unit_id].append(index)
    membership = [
        {"unit_id": unit.id, "section_indexes": membership_by_unit[unit.id]}
        for unit in snapshot.units
    ]
    return sections, membership


def _successful_variant_payload(
    variant: TopicSegmentationVariantRequest,
    result: SegmentationResult,
    snapshot: SegmentationSnapshot,
    *,
    runtime_ms: int,
) -> dict[str, Any]:
    if snapshot.source_kind == "timestamped_transcript":
        duration_ms = max(unit.end_ms or 0 for unit in snapshot.units)
        locked = lock_transcript_boundaries(
            snapshot,
            result.boundaries,
            timeline={
                "artifact_stable_id": (
                    "source_artifact:topic-compare-"
                    + snapshot.snapshot_hash.removeprefix("sha256:")
                ),
                "fingerprint": snapshot.snapshot_hash,
                "duration_ms": duration_ms,
            },
        )
        observation_by_id = {
            item.candidate_id: (item.boundary_key, item.kind)
            for item in locked.candidate_locks
        }
        canonical = [
            {
                "key": item.boundary_key,
                "kind": item.kind,
                "candidate_ids": list(item.candidate_ids),
            }
            for item in locked.locked_boundaries
        ]
        section_unit_ids = locked.section_unit_ids
    else:
        observation_by_id, canonical, section_unit_ids = _untimed_layout(
            snapshot,
            result.boundaries,
        )
    sections, membership = _membership_payload(snapshot, section_unit_ids)
    return {
        "variant_id": variant.id,
        "engine": result.engine_id,
        "engine_version": result.engine_version,
        "settings": dict(result.resolved_settings),
        "status": "completed",
        "runtime_ms": runtime_ms,
        "boundaries": [
            {
                "id": candidate.id,
                "locator": _locator_payload(candidate),
                "canonical_key": observation_by_id[candidate.id][0],
                "locking_kind": observation_by_id[candidate.id][1],
                "strength": candidate.strength,
                "label": candidate.label,
                "diagnostics": dict(candidate.diagnostics),
            }
            for candidate in result.boundaries
        ],
        "canonical_boundaries": canonical,
        "sections": sections,
        "unit_membership": membership,
        "diagnostics": dict(result.diagnostics),
        "warnings": [],
        "errors": [],
    }


def _failed_variant_payload(
    variant: TopicSegmentationVariantRequest,
    *,
    runtime_ms: int,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "variant_id": variant.id,
        "engine": variant.engine,
        "engine_version": None,
        "settings": dict(variant.settings),
        "status": "failed",
        "runtime_ms": runtime_ms,
        "boundaries": [],
        "canonical_boundaries": [],
        "sections": [],
        "unit_membership": [],
        "diagnostics": {},
        "warnings": [],
        "errors": [{"code": code, "message": message}],
    }


def _unit_payload(unit: DialogueUnit) -> dict[str, Any]:
    return {
        "id": unit.id,
        "ordinal": unit.ordinal,
        "text": unit.text,
        "speaker": unit.speaker,
        "start_ms": unit.start_ms,
        "end_ms": unit.end_ms,
    }


async def compare_topic_segmentation_scratch(
    transcript_bytes: bytes,
    request: TopicSegmentationScratchRequest | Mapping[str, Any],
) -> dict[str, Any]:
    """Run request-local variants with no project/store object in reach."""

    req = _coerce_request(request)
    parsed = parse_scratch_transcript(
        transcript_bytes,
        filename=req.filename,
        mime=req.mime,
        language=req.language,
    )
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for variant in req.variants:
        started = time.perf_counter()
        try:
            segmenter = get_segmenter(variant.engine)
            resolved_settings = segmenter.validate_settings(variant.settings)
            preflight = segmenter.preflight(parsed.snapshot, resolved_settings)
            if not preflight.ok:
                runtime_ms = int((time.perf_counter() - started) * 1_000)
                code = preflight.error_code or "preflight_failed"
                message = preflight.message or "This engine cannot analyze the sample."
                errors.append(
                    {
                        "variant_id": variant.id,
                        "engine": variant.engine,
                        "code": code,
                        "message": message,
                    }
                )
                results.append(
                    _failed_variant_payload(
                        variant,
                        runtime_ms=runtime_ms,
                        code=code,
                        message=message,
                    )
                )
                continue
            result = await asyncio.to_thread(
                segmenter.segment,
                parsed.snapshot,
                resolved_settings,
                SegmentationContext(),
            )
            if result.engine_id != segmenter.definition.id:
                raise ValueError("topic engine returned a mismatched engine id")
            runtime_ms = int((time.perf_counter() - started) * 1_000)
            results.append(
                _successful_variant_payload(
                    variant,
                    result,
                    parsed.snapshot,
                    runtime_ms=runtime_ms,
                )
            )
        except Exception as exc:  # noqa: BLE001 - each compare lane fails alone
            runtime_ms = int((time.perf_counter() - started) * 1_000)
            safe = safe_error("topic_compare_engine_failed", exc, max_chars=300)
            errors.append(
                {
                    "variant_id": variant.id,
                    "engine": variant.engine,
                    "code": safe.code,
                    "message": safe.detail,
                }
            )
            results.append(
                _failed_variant_payload(
                    variant,
                    runtime_ms=runtime_ms,
                    code=safe.code,
                    message=safe.detail,
                )
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "scratch": True,
            "filename": parsed.filename,
            "mime": parsed.mime,
            "size": parsed.size,
            "source_kind": parsed.snapshot.source_kind,
            "snapshot_hash": parsed.snapshot.snapshot_hash,
            "language": parsed.snapshot.language,
        },
        # Same action-local immutable roster as Find topic changes. This is
        # informational; the workbench engine picker still reads the action
        # catalog rather than maintaining a Compare-owned list.
        "engines": list(engine_catalog()),
        "units": [_unit_payload(unit) for unit in parsed.snapshot.units],
        "results": results,
        "warnings": [],
        "errors": errors,
    }


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SUFFIXES",
    "ParsedScratchTranscript",
    "TopicSegmentationCompareError",
    "TopicSegmentationScratchRequest",
    "TopicSegmentationVariantRequest",
    "compare_topic_segmentation_scratch",
    "parse_scratch_transcript",
]

#!/usr/bin/env python3
"""Score one pinned multi-speaker fixture without emitting transcript text."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from frisket_models.transcription.contract import (
    GatewayTranscriptionResponse,
    TranscribeResult,
    TranscribeSegment,
    WorkerTranscriptionResponse,
)

SCHEMA = "frisket.transcription.fixture.v1"
MAX_SPEAKERS = 8
MIN_OVERLAP_SECONDS = 0.01
MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
REVISION = "4a1af868018e7974197f4f018730758012b28c27"
_EVENT = re.compile(r"\[[^\]]*\]")


class EvaluationError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reference(data: bytes) -> tuple[str, float, list[TranscribeSegment]]:
    try:
        value = json.loads(data)
        clip = value["clip"]
        turns = value["reference"]["turns"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EvaluationError("invalid fixture manifest") from exc
    if value.get("schema") != SCHEMA:
        raise EvaluationError(f"fixture schema must be {SCHEMA}")
    expected_hash = clip.get("sha256")
    duration = clip.get("duration_seconds")
    if not isinstance(expected_hash, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_hash
    ):
        raise EvaluationError("fixture clip SHA-256 is invalid")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration <= 0
    ):
        raise EvaluationError("fixture duration is invalid")
    try:
        segments = [
            TranscribeSegment.model_validate(
                {key: item[key] for key in ("start", "end", "text", "speaker")},
                strict=True,
            )
            for item in turns
            if item.get("kind") != "gap"
        ]
    except (AttributeError, KeyError, TypeError, ValidationError) as exc:
        raise EvaluationError("fixture turns violate contract v1") from exc
    if (
        not segments
        or len({item.speaker for item in segments}) < 2
        or any(not item.text.strip() or item.end > duration for item in segments)
        or any(right.start < left.start for left, right in zip(segments, segments[1:]))
    ):
        raise EvaluationError("fixture turns are not a valid multi-speaker reference")
    return expected_hash.lower(), float(duration), segments


def _result(data: bytes) -> TranscribeResult:
    try:
        value = json.loads(data)
        if "result" in value and "results" not in value:
            return WorkerTranscriptionResponse.model_validate(value, strict=True).result
        envelope = GatewayTranscriptionResponse.model_validate(value, strict=True)
        if len(envelope.results) != 1:
            raise EvaluationError("gateway response must contain one result")
        return envelope.results[0]
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
    ) as exc:
        raise EvaluationError("response violates transcription contract v1") from exc


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", _EVENT.sub(" ", text)).casefold()
    return " ".join(
        "".join(
            char if unicodedata.category(char)[0] in {"L", "N"} else " "
            for char in text
        ).split()
    )


def _edit(left: Sequence[Any], right: Sequence[Any]) -> int:
    if len(left) > len(right):
        left, right = right, left
    row = list(range(len(left) + 1))
    for index, right_item in enumerate(right, 1):
        next_row = [index]
        for column, left_item in enumerate(left, 1):
            next_row.append(
                min(
                    next_row[-1] + 1,
                    row[column] + 1,
                    row[column - 1] + (left_item != right_item),
                )
            )
        row = next_row
    return row[-1]


def _streams(segments: Sequence[TranscribeSegment]) -> dict[str, str]:
    output: dict[str, list[str]] = {}
    for segment in segments:
        if segment.speaker:
            output.setdefault(segment.speaker, []).append(segment.text)
    return {speaker: _normalize(" ".join(parts)) for speaker, parts in output.items()}


def _mapping(
    reference: Sequence[str],
    hypothesis: Sequence[str],
    score,
) -> tuple[dict[str, str | None], float]:
    size = max(len(reference), len(hypothesis))
    if size > MAX_SPEAKERS:
        raise EvaluationError(f"at most {MAX_SPEAKERS} speakers are supported")
    refs: list[str | None] = list(reference) + [None] * (size - len(reference))
    hyps: list[str | None] = list(hypothesis) + [None] * (size - len(hypothesis))
    best = max(
        itertools.permutations(refs),
        key=lambda order: sum(score(hyp, ref) for hyp, ref in zip(hyps, order)),
    )
    return (
        {hyp: ref for hyp, ref in zip(hyps, best) if hyp is not None},
        sum(score(hyp, ref) for hyp, ref in zip(hyps, best)),
    )


def _intervals(
    segments: Sequence[TranscribeSegment], duration: float
) -> dict[str, list[tuple[float, float]]]:
    output: dict[str, list[tuple[float, float]]] = {}
    for segment in segments:
        if segment.speaker and segment.end > segment.start:
            start, end = max(0.0, segment.start), min(duration, segment.end)
            if end > start:
                output.setdefault(segment.speaker, []).append((start, end))
    for speaker, spans in output.items():
        merged: list[tuple[float, float]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        output[speaker] = merged
    return output


def _intersection(
    left: Sequence[tuple[float, float]], right: Sequence[tuple[float, float]]
) -> float:
    return sum(
        max(0.0, min(a_end, b_end) - max(a_start, b_start))
        for a_start, a_end in left
        for b_start, b_end in right
    )


def _diarization(
    reference: Sequence[TranscribeSegment],
    hypothesis: Sequence[TranscribeSegment],
    duration: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    ref = _intervals(reference, duration)
    hyp = _intervals(hypothesis, duration)
    mapping, _ = _mapping(
        sorted(ref),
        sorted(hyp),
        lambda h, r: (
            _intersection(hyp.get(h, ()), ref.get(r, ()))
            if h is not None and r is not None
            else 0.0
        ),
    )
    boundaries = sorted(
        {0.0, duration}
        | {
            point
            for spans in (*ref.values(), *hyp.values())
            for span in spans
            for point in span
        }
    )
    miss = false_alarm = confusion = denominator = 0.0
    overlaps = {"reference": 0.0, "hypothesis": 0.0, "coincident": 0.0}
    for start, end in zip(boundaries, boundaries[1:]):
        middle, span = (start + end) / 2, end - start
        active_ref = {
            speaker
            for speaker, spans in ref.items()
            if any(a <= middle < b for a, b in spans)
        }
        active_hyp = {
            speaker
            for speaker, spans in hyp.items()
            if any(a <= middle < b for a, b in spans)
        }
        mapped = {mapping[speaker] for speaker in active_hyp if mapping[speaker]}
        denominator += len(active_ref) * span
        miss += max(0, len(active_ref) - len(active_hyp)) * span
        false_alarm += max(0, len(active_hyp) - len(active_ref)) * span
        confusion += (
            min(len(active_ref), len(active_hyp)) - len(active_ref & mapped)
        ) * span
        ref_overlap, hyp_overlap = len(active_ref) >= 2, len(active_hyp) >= 2
        overlaps["reference"] += span if ref_overlap else 0
        overlaps["hypothesis"] += span if hyp_overlap else 0
        overlaps["coincident"] += span if ref_overlap and hyp_overlap else 0
    errors = miss + false_alarm + confusion
    return (
        {
            "collar_seconds": 0.0,
            "scores_overlap": True,
            "missed_speaker_seconds": round(miss, 6),
            "false_alarm_speaker_seconds": round(false_alarm, 6),
            "confusion_speaker_seconds": round(confusion, 6),
            "reference_speaker_seconds": round(denominator, 6),
            "rate": round(errors / denominator, 6) if denominator else None,
        },
        {key: round(value, 6) for key, value in overlaps.items()},
    )


def evaluate(
    reference_json: bytes, response_json: bytes, audio: Path
) -> dict[str, Any]:
    expected_hash, duration, reference = _reference(reference_json)
    audio_hash = _sha(audio)
    if audio_hash != expected_hash:
        raise EvaluationError("audio SHA-256 does not match fixture")
    result = _result(response_json)
    der, overlap = _diarization(reference, result.segments, duration)
    if overlap["reference"] <= 0:
        raise EvaluationError("fixture has no reference overlap")

    speakers = {item.speaker for item in result.segments if item.speaker}
    issues = []
    if not result.text.strip():
        issues.append("empty_result_text")
    if not result.segments:
        issues.append("no_segments")
    if any(not item.text.strip() for item in result.segments):
        issues.append("empty_segment_text")
    if any(not item.speaker for item in result.segments):
        issues.append("missing_speaker")
    if len(speakers) < 2:
        issues.append("fewer_than_two_speakers")
    if result.text != " ".join(item.text for item in result.segments):
        issues.append("flat_text_mismatch")
    if any(item.end > duration + 0.1 for item in result.segments) or (
        result.duration is not None and result.duration > duration + 0.1
    ):
        issues.append("timestamp_outside_fixture")
    if (
        result.engine != "moss"
        or result.model_ids != [MODEL]
        or result.revision != REVISION
        or result.device != "cuda"
        or result.dtype != "bfloat16"
    ):
        issues.append("moss_provenance_mismatch")
    if overlap["hypothesis"] < MIN_OVERLAP_SECONDS:
        issues.append("no_cross_speaker_overlap")
    if overlap["coincident"] < MIN_OVERLAP_SECONDS:
        issues.append("no_reference_aligned_overlap")

    ref_words = _normalize(" ".join(item.text for item in reference)).split()
    hyp_words = _normalize(" ".join(item.text for item in result.segments)).split()
    ref_streams, hyp_streams = _streams(reference), _streams(result.segments)
    text_mapping, score = _mapping(
        sorted(ref_streams),
        sorted(hyp_streams),
        lambda h, r: -_edit(hyp_streams.get(h, ""), ref_streams.get(r, "")),
    )
    del text_mapping
    cp_errors = int(-score)
    cp_characters = sum(len(value) for value in ref_streams.values())
    word_errors = _edit(ref_words, hyp_words)
    return {
        "schema": "frisket.transcription.fixture-eval.v1",
        "hashes": {
            "audio_sha256": audio_hash,
            "reference_sha256": hashlib.sha256(reference_json).hexdigest(),
            "response_sha256": hashlib.sha256(response_json).hexdigest(),
        },
        "structural": {
            "passed": not issues,
            "issues": issues,
            "reference_segment_count": len(reference),
            "segment_count": len(result.segments),
            "reference_speaker_count": len({item.speaker for item in reference}),
            "speaker_count": len(speakers),
            "reference_overlap_seconds": overlap["reference"],
            "hypothesis_overlap_seconds": overlap["hypothesis"],
            "coincident_overlap_seconds": overlap["coincident"],
        },
        "metrics": {
            "normalization": "strip_bracket_events+NFKC+casefold+letters_numbers",
            "wer": {
                "errors": word_errors,
                "reference_words": len(ref_words),
                "rate": round(word_errors / len(ref_words), 6) if ref_words else None,
            },
            "cpcer": {
                "errors": cp_errors,
                "reference_characters": cp_characters,
                "rate": round(cp_errors / cp_characters, 6) if cp_characters else None,
            },
            "der": der,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        artifact = evaluate(
            args.reference.read_bytes(), args.response.read_bytes(), args.audio
        )
    except (EvaluationError, OSError) as exc:
        parser.exit(2, f"fixture evaluation failed: {exc}\n")
    print(json.dumps(artifact, sort_keys=True, separators=(",", ":")))
    return 0 if artifact["structural"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

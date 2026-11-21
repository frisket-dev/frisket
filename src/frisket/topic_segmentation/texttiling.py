"""Dependency-free lexical cohesion segmentation.

This is a first-party implementation of the core TextTiling procedure from
Hearst (1997): divide the token stream into pseudosentences, compare adjacent
term-frequency blocks, find valleys in the cohesion curve, and select the
strongest depth scores as boundaries.  It intentionally does not import NLTK:
NLTK's implementation assumes English stopwords and ASCII-oriented cleanup.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import regex
from pydantic import JsonValue

from .contracts import (
    BetweenUnits,
    BoundaryCandidate,
    EngineDefinition,
    PreflightResult,
    SegmentationContext,
    SegmentationResult,
    SegmentationSnapshot,
    WithinUnit,
    validate_detail_settings,
)


# Unicode letters/numbers with combining marks and internal apostrophes. This
# keeps the lexical engine useful for whitespace-delimited non-English text
# without smuggling an English stopword corpus into its behavior.
_TOKEN_RE = regex.compile(
    r"[\p{L}\p{N}][\p{L}\p{M}\p{N}]*"
    r"(?:['’][\p{L}\p{N}][\p{L}\p{M}\p{N}]*)*"
)

_MIN_TOKENS = 20
_MIN_REPEATED_TERMS = 2
_TARGET_BLOCK_COUNT = 8
_MIN_BLOCK_SIZE = 5
_MAX_BLOCK_SIZE = 20
_BLOCK_WINDOW = 3


@dataclass(frozen=True, slots=True)
class _SourceToken:
    value: str
    unit_index: int
    offset_in_unit: int


def _source_tokens(snapshot: SegmentationSnapshot) -> list[_SourceToken]:
    tokens: list[_SourceToken] = []
    for unit_index, unit in enumerate(snapshot.units):
        values = [match.group(0).casefold() for match in _TOKEN_RE.finditer(unit.text)]
        tokens.extend(
            _SourceToken(value=value, unit_index=unit_index, offset_in_unit=offset)
            for offset, value in enumerate(values)
        )
    return tokens


def _effective_block_size(token_count: int) -> int:
    # TextTiling's block size is an algorithm parameter, not a user-facing
    # product knob. Keep the established 20-token ceiling, while allowing a
    # short but usable transcript to form enough blocks to expose a valley.
    return max(
        _MIN_BLOCK_SIZE,
        min(_MAX_BLOCK_SIZE, token_count // _TARGET_BLOCK_COUNT),
    )


def _counter_cosine(left: Counter[str], right: Counter[str]) -> float:
    dot = sum(count * right.get(term, 0) for term, count in left.items())
    if dot == 0:
        return 0.0
    left_norm = math.sqrt(sum(count * count for count in left.values()))
    right_norm = math.sqrt(sum(count * count for count in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return float(dot / (left_norm * right_norm))


def _block_cohesion(blocks: Sequence[Counter[str]], window: int) -> list[float]:
    """Compare the term frequencies in local block groups around each gap."""

    scores: list[float] = []
    for gap in range(len(blocks) - 1):
        left: Counter[str] = Counter()
        right: Counter[str] = Counter()
        for block in blocks[max(0, gap - window + 1) : gap + 1]:
            left.update(block)
        for block in blocks[gap + 1 : gap + 1 + window]:
            right.update(block)
        scores.append(_counter_cosine(left, right))
    return scores


def _depth_scores(scores: Sequence[float], *, clip: int = 0) -> list[float]:
    """Return TextTiling valley depth for each cohesion gap."""

    depths = [0.0] * len(scores)
    for index in range(clip, max(clip, len(scores) - clip)):
        gap_score = float(scores[index])

        left_peak = gap_score
        for candidate in reversed(scores[:index]):
            if candidate < left_peak:
                break
            left_peak = float(candidate)

        right_peak = gap_score
        for candidate in scores[index + 1 :]:
            if candidate < right_peak:
                break
            right_peak = float(candidate)

        depths[index] = max(
            0.0,
            0.5 * (left_peak + right_peak - (2.0 * gap_score)),
        )
    return depths


def _depth_cutoff(depths: Sequence[float], detail: str, *, semantic: bool) -> float:
    if not depths:
        return math.inf
    mean = float(statistics.fmean(depths))
    deviation = float(statistics.pstdev(depths)) if len(depths) > 1 else 0.0
    if semantic:
        # DeepTiling's reference default (mean + 2σ) is the intentionally
        # sparse mode. A balanced default at +1σ still finds two equally clear
        # shifts in a short interview instead of suppressing both as outliers.
        multiplier = {"fewer": 2.0, "balanced": 1.0, "more": 0.0}[detail]
        return max(0.01, mean + (multiplier * deviation))
    # Center the reporter-facing default on the mean so incidental shallow
    # valleys do not flood a transcript with sections. The three levels remain
    # the same TextTiling depth threshold, not three different algorithms.
    multiplier = {"fewer": 0.5, "balanced": 0.0, "more": -0.5}[detail]
    return max(0.0, mean + (multiplier * deviation))


def _select_local_maxima(
    depths: Sequence[float],
    *,
    cutoff: float,
    minimum_distance: int = 2,
) -> list[int]:
    eligible = [
        index for index, depth in enumerate(depths) if depth > 0.0 and depth > cutoff
    ]
    # Keep the deepest member of a nearby cluster, then restore source order.
    selected: list[int] = []
    for index in sorted(eligible, key=lambda value: (-depths[value], value)):
        if all(abs(index - prior) >= minimum_distance for prior in selected):
            selected.append(index)
    return sorted(selected)


class TextTilingSegmenter:
    """Local lexical topic segmentation with source-addressable token gaps."""

    _definition = EngineDefinition(
        id="texttiling",
        version="1",
        label="Lexical cohesion",
        description=(
            "Find topic shifts from repeated vocabulary and local lexical cohesion. "
            "Fast and fully local; best for languages with word boundaries."
        ),
        available=True,
        recommended=False,
    )

    @property
    def catalog_definition(self) -> EngineDefinition:
        return self._definition

    @property
    def definition(self) -> EngineDefinition:
        return self.catalog_definition

    def validate_settings(
        self,
        settings: Mapping[str, JsonValue] | None,
    ) -> Mapping[str, JsonValue]:
        return validate_detail_settings(settings)

    def preflight(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
    ) -> PreflightResult:
        self.validate_settings(settings)
        nonempty_units = sum(bool(unit.text.strip()) for unit in snapshot.units)
        if nonempty_units < 2:
            return PreflightResult.failed(
                "insufficient_text",
                "Lexical cohesion needs at least two non-empty transcript units.",
                nonempty_units=nonempty_units,
            )

        tokens = _source_tokens(snapshot)
        if len(tokens) < _MIN_TOKENS:
            return PreflightResult.failed(
                "insufficient_text",
                f"Lexical cohesion needs at least {_MIN_TOKENS} usable word tokens.",
                token_count=len(tokens),
                minimum_token_count=_MIN_TOKENS,
            )

        frequencies = Counter(token.value for token in tokens)
        repeated_terms = sum(count > 1 for count in frequencies.values())
        if repeated_terms < _MIN_REPEATED_TERMS:
            return PreflightResult.failed(
                "insufficient_lexical_signal",
                "This transcript has too little repeated vocabulary for lexical cohesion.",
                token_count=len(tokens),
                repeated_term_count=repeated_terms,
            )
        return PreflightResult.passed(
            token_count=len(tokens),
            repeated_term_count=repeated_terms,
        )

    def segment(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
        context: SegmentationContext,
    ) -> SegmentationResult:
        resolved_settings = self.validate_settings(settings)
        self.preflight(snapshot, resolved_settings).require()
        context.raise_if_cancelled()

        tokens = _source_tokens(snapshot)
        block_size = _effective_block_size(len(tokens))
        blocks = [
            Counter(token.value for token in tokens[start : start + block_size])
            for start in range(0, len(tokens), block_size)
        ]
        cohesion_scores = _block_cohesion(blocks, _BLOCK_WINDOW)
        # Smoothing a short transcript can turn one clear valley into a broad
        # plateau and produce two imprecise cuts on either side. TextTiling's
        # smoothing is optional; retain the native cohesion gaps and ignore
        # only the untrustworthy outermost gaps.
        depths = _depth_scores(cohesion_scores, clip=1)
        detail = str(resolved_settings["detail"])
        cutoff = _depth_cutoff(depths, detail, semantic=False)
        selected = _select_local_maxima(depths, cutoff=cutoff)

        candidates: list[BoundaryCandidate] = []
        for score_index in selected:
            context.raise_if_cancelled()
            token_index = min((score_index + 1) * block_size, len(tokens) - 1)
            left_token = tokens[token_index - 1]
            right_token = tokens[token_index]
            if left_token.unit_index == right_token.unit_index:
                locator: Any = WithinUnit(
                    unit_id=snapshot.units[right_token.unit_index].id
                )
            else:
                # Point at an actual adjacent source-unit gap even if empty
                # units occurred between the two token-bearing units.
                right_unit_index = right_token.unit_index
                locator = BetweenUnits(
                    left_unit_id=snapshot.units[right_unit_index - 1].id,
                    right_unit_id=snapshot.units[right_unit_index].id,
                )
            candidates.append(
                BoundaryCandidate(
                    id=f"texttiling-gap-{score_index:04d}",
                    locator=locator,
                    strength=float(depths[score_index]),
                    diagnostics={
                        "block_gap": score_index,
                        "token_index": token_index,
                        "token_offset_in_right_unit": right_token.offset_in_unit,
                        "cohesion": float(cohesion_scores[score_index]),
                        "cutoff": cutoff,
                    },
                )
            )

        context.raise_if_cancelled()
        return SegmentationResult(
            engine_id=self.definition.id,
            engine_version=self.definition.version,
            boundaries=tuple(candidates),
            resolved_settings=resolved_settings,
            diagnostics={
                "algorithm": "texttiling",
                "tokenizer": "unicode_words_v1",
                "token_count": len(tokens),
                "block_size": block_size,
                "block_window": _BLOCK_WINDOW,
                "cohesion_gap_count": len(cohesion_scores),
                "cutoff": cutoff,
            },
        )


__all__ = ["TextTilingSegmenter"]

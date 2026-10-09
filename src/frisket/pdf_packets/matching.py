"""Rank possible first pages from visual exemplars and OCR phrases.

The caller owns the user's confirmed and rejected labels. This module derives
suggestions and questions from one immutable snapshot; it never promotes its
own output into another training example.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import math
import unicodedata

import numpy as np
import regex


_KIND_SIMILARITY = 0.90
_NEGATIVE_MARGIN = 0.05
_QUESTION_BAND = 30.0
_QUESTION_LIMIT = 3
_NEAR_DUPLICATE_SIMILARITY = 0.98


@dataclass(frozen=True)
class PhraseRule:
    text: str
    enabled: bool = True
    fuzzy: bool = False


@dataclass(frozen=True)
class StartKind:
    id: int
    confirmed_pages: tuple[int, ...]


@dataclass(frozen=True)
class PageMatch:
    page: int
    visual_score: float | None
    closest_confirmed_page: int | None
    kind_id: int | None
    matched_phrases: tuple[str, ...]
    suggested: bool


@dataclass(frozen=True)
class PacketMatchResult:
    kinds: tuple[StartKind, ...]
    pages: tuple[PageMatch, ...]
    suggested_pages: tuple[int, ...]
    question_pages: tuple[int, ...]


@dataclass(frozen=True)
class _PreparedPhrase:
    label: str
    normalized: str
    fuzzy_pattern: regex.Pattern[str] | None


def _page_set(values: Collection[int], *, page_count: int, label: str) -> set[int]:
    pages = set(values)
    if any(type(page) is not int or not 1 <= page <= page_count for page in pages):
        raise ValueError(f"{label} contains a page outside 1..{page_count}")
    return pages


def _unit_vectors(
    vectors: Mapping[int, Sequence[float]], *, page_count: int
) -> dict[int, np.ndarray]:
    units: dict[int, np.ndarray] = {}
    dimension: int | None = None
    for page in sorted(vectors):
        if type(page) is not int or not 1 <= page <= page_count:
            raise ValueError(f"vectors contains a page outside 1..{page_count}")
        try:
            vector = np.asarray(vectors[page], dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"vector for page {page} must be numeric") from exc
        if vector.ndim != 1 or vector.size == 0:
            raise ValueError(f"vector for page {page} must be one-dimensional")
        if dimension is None:
            dimension = int(vector.size)
        elif vector.size != dimension:
            raise ValueError("all page vectors must have the same dimension")
        if not np.isfinite(vector).all():
            raise ValueError(f"vector for page {page} must contain only finite values")
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError(f"vector for page {page} must have a nonzero finite norm")
        units[page] = vector / norm
    return units


def _start_kinds(
    confirmed: set[int], units: Mapping[int, np.ndarray]
) -> tuple[tuple[StartKind, ...], dict[int, int]]:
    ordered = sorted(confirmed)
    parent = {page: page for page in ordered}

    def find(page: int) -> int:
        while parent[page] != page:
            parent[page] = parent[parent[page]]
            page = parent[page]
        return page

    for offset, left in enumerate(ordered):
        left_vector = units.get(left)
        if left_vector is None:
            continue
        for right in ordered[offset + 1 :]:
            right_vector = units.get(right)
            if right_vector is None:
                continue
            if float(left_vector @ right_vector) >= _KIND_SIMILARITY:
                left_root = find(left)
                right_root = find(right)
                if left_root != right_root:
                    parent[right_root] = left_root

    components: dict[int, list[int]] = {}
    for page in ordered:
        components.setdefault(find(page), []).append(page)
    groups = sorted((tuple(pages) for pages in components.values()), key=lambda p: p[0])
    kinds = tuple(
        StartKind(id=kind_id, confirmed_pages=pages)
        for kind_id, pages in enumerate(groups, start=1)
    )
    kind_by_page = {page: kind.id for kind in kinds for page in kind.confirmed_pages}
    return kinds, kind_by_page


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\u00ad", "")
    return " ".join(value.casefold().split())


def _fuzzy_errors(phrase: str) -> int:
    if len(phrase) < 5:
        return 0
    if len(phrase) < 12:
        return 1
    return 2


def _prepare_phrases(phrases: Sequence[PhraseRule]) -> tuple[_PreparedPhrase, ...]:
    prepared: list[_PreparedPhrase] = []
    seen: set[tuple[str, bool]] = set()
    for rule in phrases:
        if not isinstance(rule, PhraseRule):
            raise ValueError("phrases must contain PhraseRule values")
        if not rule.enabled:
            continue
        if not isinstance(rule.text, str):
            raise ValueError("phrase text must be a string")
        label = rule.text.strip()
        normalized = _normalize_text(label)
        if not normalized or (normalized, rule.fuzzy) in seen:
            continue
        seen.add((normalized, rule.fuzzy))
        errors = _fuzzy_errors(normalized) if rule.fuzzy else 0
        fuzzy_pattern = (
            regex.compile(
                f"(?:{regex.escape(normalized)}){{e<={errors}}}", regex.BESTMATCH
            )
            if errors
            else None
        )
        prepared.append(
            _PreparedPhrase(
                label=label,
                normalized=normalized,
                fuzzy_pattern=fuzzy_pattern,
            )
        )
    return tuple(prepared)


def _phrase_matches(text: str, phrases: Sequence[_PreparedPhrase]) -> tuple[str, ...]:
    normalized = _normalize_text(text)
    matches: list[str] = []
    for phrase in phrases:
        matched = phrase.normalized in normalized
        if not matched and phrase.fuzzy_pattern is not None:
            matched = phrase.fuzzy_pattern.search(normalized) is not None
        if matched and phrase.label not in matches:
            matches.append(phrase.label)
    return tuple(matches)


def _visual_match(
    page: int,
    *,
    units: Mapping[int, np.ndarray],
    confirmed_vectors: Sequence[tuple[int, np.ndarray]],
    rejected_vectors: Sequence[np.ndarray],
    kind_by_confirmed_page: Mapping[int, int],
) -> tuple[float | None, int | None, int | None]:
    vector = units.get(page)
    if vector is None or not confirmed_vectors:
        return None, None, kind_by_confirmed_page.get(page)

    closest_page, positive = max(
        (
            (anchor_page, float(vector @ anchor))
            for anchor_page, anchor in confirmed_vectors
        ),
        key=lambda item: (item[1], -item[0]),
    )
    adjusted = positive
    if rejected_vectors:
        negative = max(float(vector @ rejected) for rejected in rejected_vectors)
        adjusted -= max(0.0, negative - positive + _NEGATIVE_MARGIN)
    score = round(100.0 * min(1.0, max(0.0, adjusted)), 1)
    return score, closest_page, kind_by_confirmed_page[closest_page]


def _question_pages(
    candidates: Sequence[PageMatch],
    *,
    threshold: float,
    units: Mapping[int, np.ndarray],
) -> tuple[int, ...]:
    def priority(match: PageMatch) -> tuple[float, int]:
        assert match.visual_score is not None
        return threshold - match.visual_score, match.page

    ordered = sorted(candidates, key=priority)
    representative_by_kind: dict[int, PageMatch] = {}
    for match in ordered:
        assert match.kind_id is not None
        representative_by_kind.setdefault(match.kind_id, match)
    selected = sorted(representative_by_kind.values(), key=priority)[:_QUESTION_LIMIT]

    remaining = [match for match in ordered if match not in selected]
    while len(selected) < _QUESTION_LIMIT and remaining:
        distinct = next(
            (
                match
                for match in remaining
                if all(
                    float(units[match.page] @ units[chosen.page])
                    < _NEAR_DUPLICATE_SIMILARITY
                    for chosen in selected
                )
            ),
            None,
        )
        chosen = distinct or remaining[0]
        selected.append(chosen)
        remaining.remove(chosen)
    return tuple(match.page for match in selected)


def match_packet_pages(
    *,
    page_count: int,
    vectors: Mapping[int, Sequence[float]],
    confirmed: Collection[int],
    rejected: Collection[int] = (),
    threshold: float = 80.0,
    ocr_text: Mapping[int, str] | None = None,
    phrases: Sequence[PhraseRule] = (),
) -> PacketMatchResult:
    """Derive page suggestions and questions from one packet-session snapshot.

    Pages are 1-based. ``vectors`` and ``ocr_text`` may be partial while their
    background jobs are running. Visual scores and ``threshold`` use the same
    0..100 scale. Text phrase matches and visual matches are OR inputs, while
    explicit labels always remain authoritative.
    """

    if type(page_count) is not int or page_count < 1:
        raise ValueError("page_count must be a positive integer")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be a finite number from 0 to 100")
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0 <= threshold <= 100:
        raise ValueError("threshold must be a finite number from 0 to 100")

    confirmed_pages = _page_set(confirmed, page_count=page_count, label="confirmed")
    rejected_pages = _page_set(rejected, page_count=page_count, label="rejected")
    if 1 not in confirmed_pages:
        raise ValueError("confirmed must include page 1")
    if confirmed_pages & rejected_pages:
        raise ValueError("confirmed and rejected pages must be disjoint")

    units = _unit_vectors(vectors, page_count=page_count)
    kinds, kind_by_confirmed_page = _start_kinds(confirmed_pages, units)
    confirmed_vectors = tuple(
        (page, units[page]) for page in sorted(confirmed_pages) if page in units
    )
    rejected_vectors = tuple(
        units[page] for page in sorted(rejected_pages) if page in units
    )

    texts = {} if ocr_text is None else dict(ocr_text)
    for page, text in texts.items():
        if type(page) is not int or not 1 <= page <= page_count:
            raise ValueError(f"ocr_text contains a page outside 1..{page_count}")
        if not isinstance(text, str):
            raise ValueError(f"OCR text for page {page} must be a string")
    prepared_phrases = _prepare_phrases(phrases)

    page_matches: list[PageMatch] = []
    for page in range(1, page_count + 1):
        visual_score, closest_page, kind_id = _visual_match(
            page,
            units=units,
            confirmed_vectors=confirmed_vectors,
            rejected_vectors=rejected_vectors,
            kind_by_confirmed_page=kind_by_confirmed_page,
        )
        matched_phrases = _phrase_matches(texts.get(page, ""), prepared_phrases)
        labeled = page in confirmed_pages or page in rejected_pages
        suggested = not labeled and (
            (visual_score is not None and visual_score >= threshold)
            or bool(matched_phrases)
        )
        page_matches.append(
            PageMatch(
                page=page,
                visual_score=visual_score,
                closest_confirmed_page=closest_page,
                kind_id=kind_id,
                matched_phrases=matched_phrases,
                suggested=suggested,
            )
        )

    question_candidates = tuple(
        match
        for match in page_matches
        if match.visual_score is not None
        and threshold - _QUESTION_BAND <= match.visual_score < threshold
        and match.page not in confirmed_pages
        and match.page not in rejected_pages
        and not match.suggested
    )
    questions = _question_pages(question_candidates, threshold=threshold, units=units)
    suggestions = tuple(match.page for match in page_matches if match.suggested)
    return PacketMatchResult(
        kinds=kinds,
        pages=tuple(page_matches),
        suggested_pages=suggestions,
        question_pages=questions,
    )


__all__ = [
    "PacketMatchResult",
    "PageMatch",
    "PhraseRule",
    "StartKind",
    "match_packet_pages",
]

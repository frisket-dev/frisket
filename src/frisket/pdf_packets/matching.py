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

import regex

from frisket.pdf_packets.signatures import signature_similarity


# The packet trial found that whole-packet k-medoids isolated the memo covers,
# but the global silhouette was weak (0.074).  Grouping only user-confirmed
# examples at the descriptor's observed family boundary is smaller and avoids
# turning advisory layout clusters into a classifier.
_KIND_SIMILARITY = 0.75
_UNSURE_BAND = 20.0


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
    unsure_pages: tuple[int, ...]


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


def _page_signatures(
    signatures: Mapping[int, int], *, page_count: int
) -> dict[int, int]:
    checked: dict[int, int] = {}
    for page, signature in signatures.items():
        if type(page) is not int or not 1 <= page <= page_count:
            raise ValueError(f"signatures contains a page outside 1..{page_count}")
        try:
            signature_similarity(signature, signature)
        except ValueError as exc:
            raise ValueError(
                f"signature for page {page} must be a 256-bit integer"
            ) from exc
        checked[page] = signature
    return checked


def _start_kinds(
    confirmed: set[int], signatures: Mapping[int, int]
) -> tuple[tuple[StartKind, ...], dict[int, int]]:
    ordered = sorted(confirmed)
    parent = {page: page for page in ordered}

    def find(page: int) -> int:
        while parent[page] != page:
            parent[page] = parent[parent[page]]
            page = parent[page]
        return page

    for offset, left in enumerate(ordered):
        left_signature = signatures.get(left)
        if left_signature is None:
            continue
        for right in ordered[offset + 1 :]:
            right_signature = signatures.get(right)
            if right_signature is None:
                continue
            if (
                signature_similarity(left_signature, right_signature)
                >= _KIND_SIMILARITY
            ):
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
    signatures: Mapping[int, int],
    confirmed_signatures: Sequence[tuple[int, int]],
    rejected_signatures: Sequence[int],
    kind_by_confirmed_page: Mapping[int, int],
) -> tuple[float | None, int | None, int | None, bool]:
    signature = signatures.get(page)
    if signature is None or not confirmed_signatures:
        return None, None, kind_by_confirmed_page.get(page), False

    closest_page, positive = max(
        (
            (anchor_page, signature_similarity(signature, anchor))
            for anchor_page, anchor in confirmed_signatures
        ),
        key=lambda item: (item[1], -item[0]),
    )
    vetoed = (
        bool(rejected_signatures)
        and max(
            signature_similarity(signature, rejected)
            for rejected in rejected_signatures
        )
        >= positive
    )
    score = round(100.0 * positive, 1)
    return score, closest_page, kind_by_confirmed_page[closest_page], vetoed


def match_packet_pages(
    *,
    page_count: int,
    signatures: Mapping[int, int],
    confirmed: Collection[int],
    rejected: Collection[int] = (),
    threshold: float = 80.0,
    ocr_text: Mapping[int, str] | None = None,
    phrases: Sequence[PhraseRule] = (),
) -> PacketMatchResult:
    """Derive suggested and unsure pages from one packet-session snapshot.

    Pages are 1-based. ``signatures`` and ``ocr_text`` may be partial while their
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

    checked_signatures = _page_signatures(signatures, page_count=page_count)
    kinds, kind_by_confirmed_page = _start_kinds(confirmed_pages, checked_signatures)
    confirmed_signatures = tuple(
        (page, checked_signatures[page])
        for page in sorted(confirmed_pages)
        if page in checked_signatures
    )
    rejected_signatures = tuple(
        checked_signatures[page]
        for page in sorted(rejected_pages)
        if page in checked_signatures
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
        visual_score, closest_page, kind_id, vetoed = _visual_match(
            page,
            signatures=checked_signatures,
            confirmed_signatures=confirmed_signatures,
            rejected_signatures=rejected_signatures,
            kind_by_confirmed_page=kind_by_confirmed_page,
        )
        matched_phrases = _phrase_matches(texts.get(page, ""), prepared_phrases)
        labeled = page in confirmed_pages or page in rejected_pages
        suggested = (
            not labeled
            and not vetoed
            and (
                (visual_score is not None and visual_score >= threshold)
                or bool(matched_phrases)
            )
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

    unsure = tuple(
        match.page
        for match in page_matches
        if match.visual_score is not None
        and match.page not in confirmed_pages
        and match.page not in rejected_pages
        and not match.suggested
        and threshold - _UNSURE_BAND <= match.visual_score < threshold
    )
    suggestions = tuple(match.page for match in page_matches if match.suggested)
    return PacketMatchResult(
        kinds=kinds,
        pages=tuple(page_matches),
        suggested_pages=suggestions,
        unsure_pages=unsure,
    )


__all__ = [
    "PacketMatchResult",
    "PageMatch",
    "PhraseRule",
    "StartKind",
    "match_packet_pages",
]

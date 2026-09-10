from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from frisket.engine.store.grounding_contract import (
    GroundTarget,
    Match,
    SegmentStream,
    Source,
    SpanSpec,
    TextTarget,
    WordStream,
    register_strategy,
)


# --------------------------------------------------------------------------- #
# Verbatim shape table + blend from natural-pdf/selectors/ocr_match.py
# --------------------------------------------------------------------------- #

# Multi-character rewrites applied before shape mapping (NPD ``_MULTI_CHAR_RULES``)
_MULTI_CHAR_RULES = [
    ("rn", "m"),
    ("nn", "m"),
    ("ri", "n"),
    ("cl", "d"),
    ("vv", "w"),
    ("ii", "u"),
]

# Shape equivalence classes: visually similar characters -> the same class
# (NPD ``_SHAPE_MAP`` / ``_shape_groups``, ~25 classes; verbatim).
_SHAPE_MAP: dict[str, str] = {}
_shape_groups = [
    ("I", "il1I|!tfj"),  # vertical strokes
    ("O", "oO0cCeQa"),  # round/oval shapes
    ("M", "mwMW"),  # wide humps
    ("N", "nuvNUV"),  # single humps
    ("B", "bhk6"),  # ascender + loop
    ("P", "pqgy9"),  # descender + loop
    ("D", "dDPRB"),  # right-facing bowls
    ("X", "xXzZK"),  # diagonals
    ("S", "sS5$"),  # S-snakes
    ("H", "-_~="),  # horizontals
    ("2", "234"),  # small digits
    ("7", "78"),  # large digits
]
for _cls, _chars in _shape_groups:
    for _ch in _chars:
        _SHAPE_MAP[_ch] = _cls
_SHAPE_MAP["r"] = "R"

# Default threshold (NPD ``DEFAULT_THRESHOLD``): F1=0.985, 98.4% TP, 0.3% FP on
# 560 real OCR error pairs / 74 documents, measured against this blend.
DEFAULT_THRESHOLD = 0.75

# Hyphen characters that de-hyphenation joins across a line/token break:
# ASCII hyphen, soft hyphen, unicode hyphen, non-breaking hyphen.
_HYPHENS = "-­‐‑"

# Multi-column gutter split: the horizontal blank that separates two text
# columns is, by typographic convention, wide relative to the line height —
# inter-WORD spacing on a single baseline stays well under one line-height even in
# loosely justified text, whereas a real column gutter runs several line-heights.
# So a same-baseline gap wider than this multiple of the median token height marks
# a column boundary and the baseline is split there (per-column boxes) instead of
# unioned into one gutter-spanning band. 1.5 keeps clearance above justified word
# spacing while sitting far below any real gutter; derived from token geometry (no
# absolute page constant), so it holds regardless of page size or DPI.
_COLUMN_GUTTER_HEIGHT_FACTOR = 1.5

_WS_RE = re.compile(r"\s+")
# Edge punctuation strip: leading/trailing non-alnum per token (NPD ``_PUNCT_RE``,
# but unicode-aware — keeps interior punctuation).
_EDGE_PUNCT_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def _shape_compress(text: str) -> str:
    """Compress text to visual shape classes (NPD ``_shape_compress``, verbatim):
    NFKC + lowercase, apply the multi-char rewrites, then map each char to its
    shape class."""

    text = unicodedata.normalize("NFKC", text).lower()
    for pattern, replacement in _MULTI_CHAR_RULES:
        text = text.replace(pattern, replacement)
    return "".join(_SHAPE_MAP.get(ch, ch) for ch in text)


def _length_ratio(a: str, b: str) -> float:
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    return min(la, lb) / max(la, lb)


def _blend_score(q: str, c: str, *, ocr_shape: bool) -> float:
    """Core blend on already-normalized strings (NPD ``_ocr_score_normalized``).

    ``ocr_shape`` gates the OCR shape layer (W1.2): ON for ``ocr``/``word``
    streams (the shape term uses shape-compression so ``rn``~``m`` matches); OFF
    for ``pdf_text``/``segment`` (clean text — shape-compressing it would inject
    false matches), where the shape term degrades to a plain ratio on the
    normalized strings."""

    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    # SHORT-QUOTE floor: a <=3-char normalized quote is never a confident fuzzy
    # match ("us" must not match "is"). align_quote_to_tokens rejects such quotes
    # outright with [] BEFORE any scoring (the authoritative, stricter behavior),
    # so this branch is only a defensive floor for a hypothetical direct caller —
    # it degrades to 0.0.
    if len(q) <= 3:
        return 0.0
    jw = JaroWinkler.similarity(q, c)
    if ocr_shape:
        sq, sc = _shape_compress(q), _shape_compress(c)
    else:
        sq, sc = q, c
    shape_lev = fuzz.ratio(sq, sc) / 100.0
    lr = _length_ratio(q, c)
    # Blend: 0.4*JaroWinkler + 0.4*shape + 0.2*length (NPD; the
    # "seqratio = fuzz.ratio" phrasing is the same JaroWinkler-class family — we
    # use NPD's exact JaroWinkler term to preserve its measured F1 basis).
    return 0.4 * jw + 0.4 * shape_lev + 0.2 * lr


# --------------------------------------------------------------------------- #
# Public types (strategy A internals; the contract adapter maps them to Match)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Token:
    text: str
    box: tuple[float, float, float, float] | None = None  # normalized 0-1
    page: int | None = None
    start_ms: int | None = None  # temporal streams only
    end_ms: int | None = None
    source: str = "ocr"  # "ocr" | "pdf_text" | "segment" | "word"
    # SOURCE segment index (temporal streams): the caller's own segment id, NOT
    # this token's position in the stream — a scoped stream can start at any
    # index (e.g. a page/segment-filtered slice), so the two diverge.
    index: int | None = None


@dataclass(frozen=True)
class LineBox:
    page: int
    bbox: tuple[float, float, float, float]  # union over one visual line's tokens
    token_index_range: tuple[int, int]  # [start, end) into the input stream
    # The EXACT covered source-token indices unioned into this line (a subset of
    # token_index_range, which may straddle uncovered tokens between columns). The
    # contract adapter surfaces these on the region span so a caller can map covered
    # geometry back to its parallel refs BY INDEX, not by box-center containment.
    token_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class AlignResult:
    score: float
    covered_token_indices: list[int]
    line_boxes: list[LineBox] = field(default_factory=list)
    char_start: int | None = None
    char_end: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    segment_indices: list[int] = field(default_factory=list)
    method: str = ""  # "exact" | "sliding_window" | "none"
    grouping: str = ""  # "" | "per_token_fallback" (degradation)


# --------------------------------------------------------------------------- #
# Normalization: char stream + char->token index map
# --------------------------------------------------------------------------- #


def _normalize_quote(quote: str) -> str:
    """Common-layer normalize a standalone quote: NFKC, lowercase, de-hyphenate
    line-break hyphens (``infor-\\nmation`` -> ``information``), collapse
    whitespace, strip edges."""

    text = unicodedata.normalize("NFKC", quote).lower()
    text = re.sub(rf"[{_HYPHENS}]\s+(?=\w)", "", text)
    text = _WS_RE.sub(" ", text).strip()
    return _EDGE_PUNCT_RE.sub("", text)


@dataclass(frozen=True)
class _EffectiveToken:
    orig_index: int
    core: str  # edge-punct-stripped normalized text
    trailing_hyphen: bool


def _effective_tokens(tokens: Sequence[Token]) -> list[_EffectiveToken]:
    out: list[_EffectiveToken] = []
    for i, tok in enumerate(tokens):
        n = unicodedata.normalize("NFKC", tok.text).lower()
        n = _WS_RE.sub(" ", n).strip()
        if not n:
            continue
        trailing_hyphen = n[-1] in _HYPHENS
        core = _EDGE_PUNCT_RE.sub("", n)
        if not core:
            continue
        out.append(
            _EffectiveToken(orig_index=i, core=core, trailing_hyphen=trailing_hyphen)
        )
    return out


def _build_char_map(effective: list[_EffectiveToken]) -> tuple[str, list[int]]:
    """Concatenate the effective tokens into one normalized string with a
    per-char source-token-index map. De-hyphenation joins with no separator (the
    trailing hyphen is already stripped from ``core``); otherwise a single space
    joins. Separator chars map to ``-1``."""

    chars: list[str] = []
    char_token: list[int] = []
    prev_trailing_hyphen = False
    emitted_any = False
    for eff in effective:
        if emitted_any:
            if prev_trailing_hyphen and eff.core[:1].isalnum():
                sep = ""  # de-hyphenation join
            else:
                sep = " "
            for ch in sep:
                chars.append(ch)
                char_token.append(-1)
        for ch in eff.core:
            chars.append(ch)
            char_token.append(eff.orig_index)
        prev_trailing_hyphen = eff.trailing_hyphen
        emitted_any = True
    return "".join(chars), char_token


# --------------------------------------------------------------------------- #
# Line grouping (spatial — per-line rects, NOT a merged union band)
# --------------------------------------------------------------------------- #


def _group_line_boxes(
    tokens: Sequence[Token], covered: list[int]
) -> tuple[list[LineBox], str]:
    """Group covered spatial tokens into visual lines (one rect per line).
    Baseline-band grouping; degrades to one box per token on an implausible line
    count (orphan rate > 0.3) rather than a wrong merged band (W1.7)."""

    positioned = [
        i for i in covered if tokens[i].box is not None and tokens[i].page is not None
    ]
    if not positioned:
        return [], ""

    def y_center(i: int) -> float:
        b = tokens[i].box
        return (b[1] + b[3]) / 2.0

    def height(i: int) -> float:
        b = tokens[i].box
        return abs(b[3] - b[1])

    ordered = sorted(
        positioned, key=lambda i: (tokens[i].page, y_center(i), tokens[i].box[0])
    )
    heights = sorted(height(i) for i in ordered)
    median_h = heights[len(heights) // 2] if heights else 0.0
    band = 0.6 * median_h if median_h > 0 else 0.0

    lines: list[list[int]] = []
    for i in ordered:
        placed = False
        if lines:
            last = lines[-1]
            same_page = tokens[i].page == tokens[last[0]].page
            baseline = sum(y_center(j) for j in last) / len(last)
            if same_page and (band == 0.0 or abs(y_center(i) - baseline) <= band):
                last.append(i)
                placed = True
        if not placed:
            lines.append([i])

    def line_box(members: list[int]) -> LineBox:
        page = tokens[members[0]].page
        xs0 = [tokens[j].box[0] for j in members]
        ys0 = [tokens[j].box[1] for j in members]
        xs1 = [tokens[j].box[2] for j in members]
        ys1 = [tokens[j].box[3] for j in members]
        lo = min(members)
        hi = max(members) + 1
        return LineBox(
            page=int(page),
            bbox=(min(xs0), min(ys0), max(xs1), max(ys1)),
            token_index_range=(lo, hi),
            token_indices=tuple(sorted(members)),
        )

    # Orphan heuristic: single-token lines that likely mean a column/table layout
    # was mis-banded — fall back to one box per token, honestly recorded. Checked
    # on the BASELINE lines (before the column split) so a genuinely orphan-heavy
    # layout still degrades to per-token rather than trusting the banding.
    orphan_lines = sum(1 for ln in lines if len(ln) == 1)
    if (
        len(ordered) >= 4
        and orphan_lines / len(lines) > 0.3
        and len(lines) < len(ordered)
    ):
        per_token = [
            LineBox(
                page=int(tokens[i].page),
                bbox=tuple(tokens[i].box),  # type: ignore[arg-type]
                token_index_range=(i, i + 1),
                token_indices=(i,),
            )
            for i in ordered
        ]
        return per_token, "per_token_fallback"

    # Column split (multi-column guard): a well-formed baseline may still carry
    # tokens from two columns on the same row — split each baseline at any gutter
    # so per-column boxes never union across the blank between columns.
    gutter = _COLUMN_GUTTER_HEIGHT_FACTOR * median_h if median_h > 0 else 0.0
    segments: list[list[int]] = []
    for members in lines:
        segments.extend(_split_line_by_gutter(members, tokens, gutter))

    return [line_box(members) for members in segments], ""


def _split_line_by_gutter(
    members: list[int], tokens: Sequence[Token], gutter: float
) -> list[list[int]]:
    """Split one baseline line into per-column segments at horizontal gaps wider
    than ``gutter`` (see :data:`_COLUMN_GUTTER_HEIGHT_FACTOR`). ``gutter <= 0``
    (degenerate geometry) or a single-token line -> no split."""

    if gutter <= 0.0 or len(members) <= 1:
        return [list(members)]
    ordered_x = sorted(members, key=lambda i: tokens[i].box[0])  # type: ignore[index]
    segments: list[list[int]] = [[ordered_x[0]]]
    for prev, cur in zip(ordered_x, ordered_x[1:]):
        gap = tokens[cur].box[0] - tokens[prev].box[2]  # type: ignore[index]
        if gap > gutter:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


# --------------------------------------------------------------------------- #
# The matcher (exact fast path, then token sliding window)
# --------------------------------------------------------------------------- #


def _covered_from_char_span(char_token: list[int], start: int, end: int) -> list[int]:
    seen: list[int] = []
    for idx in char_token[start:end]:
        if idx >= 0 and idx not in seen:
            seen.append(idx)
    return seen


def _temporal_result(
    tokens: Sequence[Token],
    covered: list[int],
    *,
    score: float,
    method: str,
    char_start: int | None,
    char_end: int | None,
) -> AlignResult:
    starts = [tokens[i].start_ms for i in covered if tokens[i].start_ms is not None]
    ends = [tokens[i].end_ms for i in covered if tokens[i].end_ms is not None]
    # Emit the SOURCE segment indexes the covered tokens carry, NOT their stream
    # positions — the two differ whenever the stream is a scoped slice. Fall back
    # to the position only when a token carries no explicit index.
    segment_indices = sorted(
        tokens[i].index if tokens[i].index is not None else i for i in covered
    )
    return AlignResult(
        score=score,
        covered_token_indices=sorted(covered),
        line_boxes=[],
        char_start=char_start,
        char_end=char_end,
        start_ms=min(starts) if starts else None,
        end_ms=max(ends) if ends else None,
        segment_indices=segment_indices,
        method=method,
    )


def _spatial_result(
    tokens: Sequence[Token],
    covered: list[int],
    *,
    score: float,
    method: str,
    char_start: int | None,
    char_end: int | None,
) -> AlignResult:
    line_boxes, grouping = _group_line_boxes(tokens, covered)
    return AlignResult(
        score=score,
        covered_token_indices=sorted(covered),
        line_boxes=line_boxes,
        char_start=char_start,
        char_end=char_end,
        method=method,
        grouping=grouping,
    )


def _is_temporal(tokens: Sequence[Token]) -> bool:
    return any(t.start_ms is not None or t.end_ms is not None for t in tokens)


def align_quote_to_tokens(
    quote: str,
    tokens: Sequence[Token],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_candidates: int = 3,
    context_before: str | None = None,
    context_after: str | None = None,
) -> list[AlignResult]:
    """Align ``quote`` against a positioned token stream. Returns ranked-desc
    ``AlignResult``s that clear ``threshold``; ``[]`` if none do (-> Degradation
    Law at the caller). Spatial streams carry ``box``/``page`` and yield
    ``line_boxes``; temporal streams carry ``start_ms``/``end_ms`` and yield
    ``segment_indices``."""

    q_norm = _normalize_quote(quote)
    if not q_norm or not tokens:
        return []

    effective = _effective_tokens(tokens)
    if not effective:
        return []
    concat, char_token = _build_char_map(effective)
    temporal = _is_temporal(tokens)
    ocr_shape = any(t.source in {"ocr", "word"} for t in tokens)

    result_builder = _temporal_result if temporal else _spatial_result

    # SHORT-QUOTE guard (NPD): a quote normalizing to <=3 chars is noise
    # to ground — it matches "us"/"is" everywhere, including as a sub-fragment of
    # the exact fast path. Return [] outright (Degradation Law -> the caller keeps
    # the chunk anchor); a 2-char fuzzy OR substring hit is not honest geometry.
    if len(q_norm) <= 3:
        return []

    # Tier 1: exact fast path — normalized quote is a contiguous substring.
    results: list[AlignResult] = []
    for m in re.finditer(re.escape(q_norm), concat):
        covered = _covered_from_char_span(char_token, m.start(), m.end())
        if not covered:
            continue
        results.append(
            result_builder(
                tokens,
                covered,
                score=1.0,
                method="exact",
                char_start=m.start(),
                char_end=m.end(),
            )
        )
    if results:
        return _rank_and_disambiguate(
            results,
            tokens,
            char_token,
            concat,
            context_before,
            context_after,
            max_candidates,
        )

    # Tier 2/3: token sliding window with the scored blend.
    cores = [eff.core for eff in effective]
    n_q = max(1, len(q_norm.split()))
    n_c = len(cores)
    window_sizes = {size for size in (n_q - 1, n_q, n_q + 1) if 1 <= size <= n_c}
    scored: list[tuple[float, int, int]] = []  # (score, eff_start, eff_end)
    for ws in sorted(window_sizes):
        for i in range(n_c - ws + 1):
            window_text = " ".join(cores[i : i + ws])
            score = _blend_score(q_norm, window_text, ocr_shape=ocr_shape)
            if score >= threshold:
                scored.append((score, i, i + ws))
    if not scored:
        return []

    for score, eff_start, eff_end in scored:
        covered = [effective[j].orig_index for j in range(eff_start, eff_end)]
        char_start, char_end = _eff_range_to_char_span(char_token, covered)
        results.append(
            result_builder(
                tokens,
                covered,
                score=score,
                method="sliding_window",
                char_start=char_start,
                char_end=char_end,
            )
        )
    return _rank_and_disambiguate(
        results,
        tokens,
        char_token,
        concat,
        context_before,
        context_after,
        max_candidates,
    )


def _eff_range_to_char_span(
    char_token: list[int], covered: list[int]
) -> tuple[int | None, int | None]:
    positions = [i for i, idx in enumerate(char_token) if idx in covered]
    if not positions:
        return None, None
    return positions[0], positions[-1] + 1


def _rank_and_disambiguate(
    results: list[AlignResult],
    tokens: Sequence[Token],
    char_token: list[int],
    concat: str,
    context_before: str | None,
    context_after: str | None,
    max_candidates: int,
) -> list[AlignResult]:
    """Rank desc by score; on repeats, break ties by neighbouring-context match
    (W1.3 (b)/(c)) — never silently pick the first occurrence."""

    if not results:
        return []
    scored: list[tuple[float, AlignResult]] = []
    for res in results:
        bonus = _context_bonus(res, char_token, concat, context_before, context_after)
        scored.append((res.score + bonus, res))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [res for _, res in scored[:max_candidates]]


def _context_bonus(
    res: AlignResult,
    char_token: list[int],
    concat: str,
    context_before: str | None,
    context_after: str | None,
) -> float:
    if not (context_before or context_after) or res.char_start is None:
        return 0.0
    bonus = 0.0
    window = 60
    if context_before:
        before = concat[max(0, res.char_start - window) : res.char_start]
        cb = _normalize_quote(context_before)
        if cb and cb[-min(len(cb), window) :] in before:
            bonus += 0.05
    if context_after and res.char_end is not None:
        after = concat[res.char_end : res.char_end + window]
        ca = _normalize_quote(context_after)
        if ca and ca[: min(len(ca), window)] in after:
            bonus += 0.05
    return bonus


# --------------------------------------------------------------------------- #
# Contract adapter — strategy A behind grounding_contract.ground()
# --------------------------------------------------------------------------- #


def _tokens_from_source(source: Source) -> list[Token]:
    if isinstance(source, WordStream):
        return [
            Token(text=t.text, box=t.box, page=t.page, source=t.source)
            for t in source.tokens
        ]
    if isinstance(source, SegmentStream):
        return [
            Token(
                text=t.text,
                start_ms=t.start_ms,
                end_ms=t.end_ms,
                source="segment",
                index=t.index,
            )
            for t in source.tokens
        ]
    raise TypeError(f"align does not accept source type {type(source).__name__}")


def _line_box_to_bbox_dict(lb: LineBox) -> dict[str, object]:
    x0, y0, x1, y1 = lb.bbox
    return {
        "space": "page_normalized",
        "x0": round(x0, 6),
        "y0": round(y0, 6),
        "x1": round(x1, 6),
        "y1": round(y1, 6),
        "raw": [x0, y0, x1, y1],
    }


def align(
    target: GroundTarget,
    source: Source,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Match]:
    """Strategy A: adapt :func:`align_quote_to_tokens` to the grounding
    contract. Spatial sources -> per-line ``region`` spans; temporal sources ->
    a ``temporal`` span. ``[]`` means no confident match (Degradation Law)."""

    if not isinstance(target, TextTarget):
        raise TypeError("align requires a TextTarget")
    tokens = _tokens_from_source(source)
    results = align_quote_to_tokens(
        target.text,
        tokens,
        threshold=threshold,
        context_before=target.context,
    )
    matches: list[Match] = []
    for res in results:
        if res.line_boxes:
            spans = [
                SpanSpec(
                    span_kind="region",
                    page_start=lb.page,
                    page_end=lb.page,
                    char_start=res.char_start,
                    char_end=res.char_end,
                    bbox=[_line_box_to_bbox_dict(lb)],
                    quote=target.text,
                    # covered_token_indices: the EXACT source-token indices this
                    # line unions (Token.index machinery). A caller whose
                    # source stream is parallel to its own refs maps covered
                    # geometry back to refs BY INDEX -- no box-center containment,
                    # so a decoy that merely overlaps the line box is never picked.
                    metadata={
                        **({"grouping": res.grouping} if res.grouping else {}),
                        "covered_token_indices": list(lb.token_indices),
                    },
                )
                for lb in res.line_boxes
            ]
        elif res.start_ms is not None or res.end_ms is not None:
            spans = [
                SpanSpec(
                    span_kind="temporal",
                    start_ms=res.start_ms,
                    end_ms=res.end_ms,
                    quote=target.text,
                    metadata={"segment_indices": res.segment_indices},
                )
            ]
        else:
            # Matched, but no positional selector available -> floor rung.
            spans = [
                SpanSpec(
                    span_kind="text",
                    char_start=res.char_start,
                    char_end=res.char_end,
                    quote=target.text,
                )
            ]
        matches.append(Match(spans=spans, score=res.score, method="align"))
    return matches


# Register strategy A in the contract's dict. Importing this module wires
# "align" into grounding_contract.STRATEGIES (kept here to avoid an import cycle).
register_strategy("align", align)

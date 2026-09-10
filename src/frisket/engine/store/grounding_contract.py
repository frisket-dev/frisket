from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Union


# --------------------------------------------------------------------------- #
# The target: what the caller HAS — free text (A) or anchor ids (B), never both
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TextTarget:
    """Free text to fuzzy-match (strategy A): a quote, an extracted field value,
    or a citation snippet."""

    text: str
    context: str | None = None  # optional disambiguation hint — strategy A only


@dataclass(frozen=True)
class AnchorTarget:
    """The anchor unit id(s) the model returned (strategy B), indexing an
    :class:`AnchorStream`."""

    unit_ids: list[int | str]


GroundTarget = Union[TextTarget, AnchorTarget]


# --------------------------------------------------------------------------- #
# The source: one addressable representation of the cited chunk (three, closed)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WordToken:
    """One positioned token in a :class:`WordStream` (OCR/PDF spatial)."""

    text: str
    box: tuple[float, float, float, float] | None  # normalized 0-1 (x0,y0,x1,y1)
    page: int | None
    source: str = "ocr"  # "ocr" | "pdf_text" | "word" — selects the norm profile


@dataclass(frozen=True)
class WordStream:
    """Ordered positioned tokens. OCR/PDF spatial. Strategy A input."""

    tokens: list[WordToken]


@dataclass(frozen=True)
class SegmentToken:
    """One transcript segment (or word) in a :class:`SegmentStream`."""

    text: str
    start_ms: int | None
    end_ms: int | None
    index: int
    # Optional anonymous speaker turn label ("S1"/"S2"), present only on
    # diarized transcripts. Default None keeps old callers valid.
    speaker: str | None = None


@dataclass(frozen=True)
class SegmentStream:
    """Ordered temporal segments (optional per-word times). Transcript. A + B."""

    tokens: list[SegmentToken]


@dataclass(frozen=True)
class AnchorUnit:
    """One addressable, id'd unit in an :class:`AnchorStream`. Keeps a
    back-pointer to its source range so its geometry is that range's union."""

    unit_id: int | str
    spans: list["SpanSpec"]  # the geometry this numbered unit resolves to
    text: str = ""
    # Optional anonymous speaker turn label, threaded from the
    # segment's ``selector.speaker``. Default None keeps old callers valid.
    speaker: str | None = None


@dataclass(frozen=True)
class AnchorStream:
    """The same tokens grouped into id'd units — the general addressable-unit
    stream strategy B resolves against (numbered OCR lines OR numbered transcript
    segments). A WordStream/SegmentStream projects into it cheaply."""

    units: list[AnchorUnit]


Source = Union[WordStream, SegmentStream, AnchorStream]


# --------------------------------------------------------------------------- #
# The output: the EXISTING span model + an explicit score + the method
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpanSpec:
    """Exactly the geometry fields ``store.evidence.record_source_span`` takes —
    NO new "line" kind (per-line rects are a shared write-time builder detail,
    not a contract concept, RISK #1). ``span_kind in {region, temporal, text,
    page_range}``."""

    span_kind: str
    page_start: int | None = None
    page_end: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    bbox: list[dict[str, Any]] | None = None
    quote: str | None = None
    snippet: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record_kwargs(self) -> dict[str, Any]:
        """The kwargs to splat into ``record_source_span`` (minus artifact_id)."""

        return {
            "span_kind": self.span_kind,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "bbox": self.bbox,
            "quote": self.quote,
            "snippet": self.snippet,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Match:
    """A ranked grounding result. ``spans`` reconstruct only real source
    geometry; ``score`` is explicit (invariant #5); ``method`` names the strategy
    for provenance."""

    spans: list[SpanSpec]
    score: float
    method: str


# --------------------------------------------------------------------------- #
# Strategy B: validate-and-lookup (tiny; self-contained — no rapidfuzz)
# --------------------------------------------------------------------------- #


def anchor_id(
    target: GroundTarget,
    source: Source,
    *,
    threshold: float = 0.75,  # meaningless for a lookup; accepted for signature parity
) -> list[Match]:
    """Strategy B. Validate each id in ``target.unit_ids`` against the
    ``AnchorStream``'s known ids and resolve valid ones BY LOOKUP to their unit
    spans (never by re-matching text, §2). Out-of-range/unknown ids are dropped.
    At least one valid id -> one ``Match`` (union of valid units' spans,
    ``score=1.0``); all invalid -> ``[]`` (Degradation Law)."""

    del threshold  # a lookup either resolves or it does not
    if not isinstance(target, AnchorTarget):
        raise TypeError("anchor_id requires an AnchorTarget")
    if not isinstance(source, AnchorStream):
        raise TypeError("anchor_id requires an AnchorStream source")

    by_id = {unit.unit_id: unit for unit in source.units}
    resolved_spans: list[SpanSpec] = []
    for unit_id in target.unit_ids:
        unit = by_id.get(unit_id)
        if unit is None:
            continue  # out-of-range / hallucinated id -> dropped
        resolved_spans.extend(unit.spans)
    if not resolved_spans:
        return []
    return [Match(spans=resolved_spans, score=1.0, method="anchor_id")]


# --------------------------------------------------------------------------- #
# Registration: a dict, and the type-routed dispatcher
# --------------------------------------------------------------------------- #

# Registry of strategies (name -> function matching the ground() signature). The
# same shape OCR/transcribe use for engines. ``align`` is
# registered by ``store.quote_align`` on import (kept out of this module to avoid
# an import cycle: quote_align imports these types). ``hybrid`` is deliberately
# absent — the C composition is a call-site sequence, not a registered strategy.
STRATEGIES: dict[str, Callable[..., list[Match]]] = {
    "anchor_id": anchor_id,
}


def register_strategy(name: str, fn: Callable[..., list[Match]]) -> None:
    """Register a strategy function under ``name`` (idempotent)."""

    STRATEGIES[name] = fn


def _ensure_builtins_registered() -> None:
    """Complete the builtin registry regardless of caller import order.

    ``align`` lives in ``store.quote_align`` (which imports THIS module's types),
    so it self-registers on import. We import it lazily HERE — at first accessor
    call, never at module init — so the builtin set is always complete without a
    module-init import cycle (quote_align -> grounding_contract) and without
    depending on some earlier caller having imported quote_align first. ``anchor_id``
    is defined in this module and is registered eagerly above."""

    if "align" not in STRATEGIES:
        import frisket.engine.store.quote_align  # noqa: F401  (registers "align")


def strategies() -> dict[str, Callable[..., list[Match]]]:
    """The strategy registry with all builtins guaranteed present. Callers (and
    the conformance corpus) iterate/look up through THIS accessor, never the raw
    ``STRATEGIES`` dict, so registration never hinges on import order."""

    _ensure_builtins_registered()
    return STRATEGIES


def _default_method_for(target: GroundTarget) -> str:
    if isinstance(target, TextTarget):
        return "align"
    if isinstance(target, AnchorTarget):
        return "anchor_id"
    raise TypeError(f"unroutable target type: {type(target).__name__}")


def ground(
    target: GroundTarget,
    source: Source,
    *,
    threshold: float = 0.75,  # TextTarget only; ignored by anchor_id
    method: str | None = None,
) -> list[Match]:
    """Route a target to its default strategy (TextTarget -> ``align``,
    AnchorTarget -> ``anchor_id``) or to an explicit ``method`` override, and
    return ranked ``Match``es best-first (``[]`` means "no confident match" ->
    degrade). ``method`` is a call-site override for the §2 per-path default
    map — not a new lever on the contract shape."""

    name = method or _default_method_for(target)
    fn = strategies().get(name)
    if fn is None:
        raise KeyError(f"unknown grounding strategy: {name!r}")
    return fn(target, source, threshold=threshold)

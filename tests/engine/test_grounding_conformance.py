from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from frisket.engine.store.grounding_contract import (
    AnchorStream,
    AnchorTarget,
    AnchorUnit,
    Match,
    SegmentStream,
    SegmentToken,
    SpanSpec,
    TextTarget,
    WordStream,
    WordToken,
    ground,
)
from frisket.engine.store.quote_align import (  # noqa: F401 -- registers "align"
    DEFAULT_THRESHOLD,
    _blend_score,
    _normalize_quote,
    align,
)

FIXTURE_DIR = pathlib.Path(__file__).parent.parent / "fixtures" / "grounding"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def _load(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _build_target(spec: dict[str, Any]):
    if spec["kind"] == "text":
        return TextTarget(text=spec["text"], context=spec.get("context"))
    if spec["kind"] == "anchor":
        return AnchorTarget(unit_ids=list(spec["unit_ids"]))
    raise ValueError(spec["kind"])


def _build_source(spec: dict[str, Any]):
    if spec["kind"] == "word":
        return WordStream(
            tokens=[
                WordToken(
                    text=t["text"],
                    box=tuple(t["box"]) if t.get("box") else None,
                    page=t.get("page"),
                    source=t.get("source", "ocr"),
                )
                for t in spec["tokens"]
            ]
        )
    if spec["kind"] == "segment":
        return SegmentStream(
            tokens=[
                SegmentToken(
                    text=t["text"],
                    start_ms=t.get("start_ms"),
                    end_ms=t.get("end_ms"),
                    index=t["index"],
                )
                for t in spec["tokens"]
            ]
        )
    if spec["kind"] == "anchor":
        return AnchorStream(
            units=[
                AnchorUnit(
                    unit_id=u["unit_id"],
                    text=u.get("text", ""),
                    spans=[SpanSpec(**s) for s in u["spans"]],
                )
                for u in spec["units"]
            ]
        )
    raise ValueError(spec["kind"])


def _granularity(matches: list[Match]) -> str:
    """Read the honest rung off the resolved spans (contract docstring / item 12):
    region+bbox -> block; temporal from word timing -> word, else segment;
    page_range+no-bbox -> page_range; text+no-position -> none."""

    if not matches:
        return "none"
    spans = [span for match in matches for span in match.spans]
    if any(s.span_kind == "region" and s.bbox for s in spans):
        return "block"
    if any(s.span_kind == "temporal" for s in spans):
        worded = any("word" in (s.metadata or {}).get("granularity", "") for s in spans)
        return "word" if worded else "segment"
    if any(s.span_kind == "page_range" and not s.bbox for s in spans):
        return "page_range"
    if any(s.span_kind == "text" and not s.bbox for s in spans):
        return "none"
    return "none"


def _assert_invariants(matches: list[Match], source: Any) -> None:
    for match in matches:
        # Explicit score in [0, 1] (invariant #5).
        assert 0.0 <= match.score <= 1.0
        assert match.method
        assert match.spans, "a Match must carry at least one span"
        for span in match.spans:
            # Output derivable from input: a region bbox must be a real 0-1 box.
            if span.span_kind == "region":
                assert span.bbox, "region span must carry a bbox"
                box = span.bbox[0]
                assert 0.0 <= box["x0"] < box["x1"] <= 1.0
                assert 0.0 <= box["y0"] < box["y1"] <= 1.0
        # An anchor-id match is a validated lookup, so it always scores 1.0.
        if match.method == "anchor_id":
            assert match.score == 1.0


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_grounding_conformance(path: pathlib.Path) -> None:
    fixture = _load(path)
    target = _build_target(fixture["target"])
    source = _build_source(fixture["source"])
    expect = fixture["expect"]

    matches = ground(target, source, method=fixture["strategy"])

    _assert_invariants(matches, source)

    if not expect.get("matches", True):
        # Degradation Law: no confident match -> [] -> caller keeps chunk anchor.
        assert matches == [], f"{path.stem}: expected degradation, got {matches}"
        assert _granularity(matches) == expect.get("granularity", "none")
        return

    assert matches, f"{path.stem}: expected a match, got []"

    # Expected coverage: at least ``min_spans`` spans over the top match(es).
    total_spans = sum(len(m.spans) for m in matches)
    assert total_spans >= expect.get("min_spans", 1)

    # Expected GRANULARITY rung (item 12) — the honest span_kind, not more.
    assert _granularity(matches) == expect["granularity"], (
        f"{path.stem}: granularity mismatch"
    )

    if "score" in expect:
        assert matches[0].score == pytest.approx(expect["score"])

    if "n_matches" in expect:
        # Repeated text: return ALL occurrences ranked, never results[0] bias.
        assert len(matches) == expect["n_matches"]

    if "grouping" in expect:
        groupings = {
            (s.metadata or {}).get("grouping") for m in matches for s in m.spans
        }
        assert expect["grouping"] in groupings

    if "segment_indices" in expect:
        idx = matches[0].spans[0].metadata.get("segment_indices")
        assert idx == expect["segment_indices"]

    if "start_ms" in expect:
        assert matches[0].spans[0].start_ms == expect["start_ms"]
    if "end_ms" in expect:
        assert matches[0].spans[0].end_ms == expect["end_ms"]

    if "top_page_y_at_least" in expect:
        # Context disambiguation put the right (lower-on-page) occurrence first.
        top_box = matches[0].spans[0].bbox[0]
        assert top_box["y0"] >= expect["top_page_y_at_least"]

    if "max_span_width" in expect:
        # Multi-column guard: no region box may span the gutter — every emitted
        # box stays within one column (width bounded), never a same-baseline union
        # across columns.
        for match in matches:
            for span in match.spans:
                if span.span_kind == "region" and span.bbox:
                    box = span.bbox[0]
                    width = box["x1"] - box["x0"]
                    assert width <= expect["max_span_width"], (
                        f"{path.stem}: region span width {width} exceeds "
                        f"max_span_width {expect['max_span_width']} "
                        "(gutter-spanning union?)"
                    )


def test_strategies_accessor_registers_builtins_independent_of_import_order() -> None:
    """A fresh interpreter that imports ONLY grounding_contract
    (never quote_align) must still resolve "align" via strategies() — proving
    registration does not depend on the caller importing quote_align first."""

    import subprocess
    import sys

    code = (
        "from frisket.engine.store.grounding_contract import strategies, STRATEGIES;"
        "names = set(strategies());"
        "assert {'align', 'anchor_id'} <= names, sorted(names);"
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# --------------------------------------------------------------------------- #
# 0.75 threshold re-pin: re-measure precision/recall on our own labeled pairs
# under the rapidfuzz blend (W1.5/W1.6). This is the same-PR re-measurement the
# plan requires — not a magic constant with no referent.
# --------------------------------------------------------------------------- #

# (query, candidate) real OCR-garble pairs that SHOULD match (positives).
_POSITIVE_PAIRS = [
    ("reviewer", "roviowor"),
    ("modern", "modem"),
    ("information", "informalion"),
    ("company", "cornpany"),
    ("million", "rnillion"),
    ("date received", "date recelved"),
    ("integrity", "integrlty"),
    ("election", "electlon"),
]

# Unrelated pairs that SHOULD NOT match (negatives).
_NEGATIVE_PAIRS = [
    ("integrity", "category"),
    ("revenue", "sunset"),
    ("company", "monetary"),
    ("election", "committee"),
    ("quarterly", "sandwich"),
]


def _score(a: str, b: str) -> float:
    return _blend_score(_normalize_quote(a), _normalize_quote(b), ocr_shape=True)


def test_threshold_075_separates_ocr_garble_from_unrelated() -> None:
    tp = sum(1 for a, b in _POSITIVE_PAIRS if _score(a, b) >= DEFAULT_THRESHOLD)
    fn = len(_POSITIVE_PAIRS) - tp
    fp = sum(1 for a, b in _NEGATIVE_PAIRS if _score(a, b) >= DEFAULT_THRESHOLD)
    tn = len(_NEGATIVE_PAIRS) - fp

    recall = tp / (tp + fn)
    precision = tp / (tp + fp) if (tp + fp) else 1.0

    # The re-pin: 0.75 keeps recall perfect and precision high on our data,
    # confirming NPD's default holds under our rapidfuzz blend (W1.5). If this
    # ever fails, re-pin the default HERE and in the quote_align docstring.
    assert recall == 1.0, f"recall={recall} (fn={fn})"
    assert precision >= 0.8, f"precision={precision} (fp={fp})"
    assert tn >= len(_NEGATIVE_PAIRS) - 1

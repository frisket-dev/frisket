#!/usr/bin/env python3
"""Render a pricing-refresh PR body: every changed rate, big swings flagged.

Usage: pricing_delta_report.py <before.json> <after.json>

A swing above 50% on any single rate gets a loud marker — that's either
real market news or upstream breakage, and both deserve a human look
before the numbers feed cost estimates and consent bounds.
"""

from __future__ import annotations

import json
import sys

SWING_FLAG = 0.5


def _rates(doc: dict) -> dict[str, list[float] | dict[str, float]]:
    out: dict[str, list[float] | dict[str, float]] = {}
    for section in ("text", "audio"):
        for model, rates in (doc.get(section) or {}).items():
            out[f"{section}:{model}"] = rates
    return out


def _named_rates(rates: list[float] | dict[str, float]) -> dict[str, float]:
    if isinstance(rates, list):
        return {f"rate[{index}]": rate for index, rate in enumerate(rates)}
    return rates


def main() -> int:
    before = _rates(json.load(open(sys.argv[1])))
    after = _rates(json.load(open(sys.argv[2])))
    lines = [
        "Nightly price-table refresh. Review before merge — these",
        "numbers feed cost estimates and consent bounds.",
        "",
    ]
    flagged = False
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if b is None:
            lines.append(f"- **{key}**: NEW {a}")
            continue
        if a is None:
            lines.append(f"- **{key}**: REMOVED (was {b})")
            continue
        marks = []
        before_rates = _named_rates(b)
        after_rates = _named_rates(a)
        for rate_name in sorted(set(before_rates) & set(after_rates)):
            x, y = before_rates[rate_name], after_rates[rate_name]
            if x and abs(y - x) / abs(x) > SWING_FLAG:
                marks.append(
                    f"{rate_name} moved {x} -> {y} (>{int(SWING_FLAG * 100)}%)"
                )
        note = "  ⚠ " + "; ".join(marks) if marks else ""
        flagged = flagged or bool(marks)
        lines.append(f"- **{key}**: {b} -> {a}{note}")
    if flagged:
        lines.insert(
            2,
            "⚠ **At least one rate swung more than 50% — verify upstream before merging.**",
        )
        lines.insert(3, "")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

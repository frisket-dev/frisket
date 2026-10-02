#!/usr/bin/env python3
"""Render a pricing-refresh PR body showing every changed rate.

Usage: pricing_delta_report.py <before.json> <after.json>

Valid prices auto-merge regardless of the size of the change.
"""

from __future__ import annotations

import json
import sys


def _rates(doc: dict) -> dict[str, list[float] | dict[str, float]]:
    out: dict[str, list[float] | dict[str, float]] = {}
    for section in ("text", "audio"):
        for model, rates in (doc.get(section) or {}).items():
            out[f"{section}:{model}"] = rates
    return out


def main() -> int:
    before = _rates(json.load(open(sys.argv[1])))
    after = _rates(json.load(open(sys.argv[2])))
    lines = [
        "Automatic price-table refresh for cost estimates.",
        "Validated pricing-only changes auto-merge after required checks pass.",
        "",
    ]
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
        lines.append(f"- **{key}**: {b} -> {a}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

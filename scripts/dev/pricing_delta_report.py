#!/usr/bin/env python3
"""Render a catalog-refresh PR body: model and rate changes, big swings flagged.

Usage: pricing_delta_report.py <before.json> <after.json>

A swing above 50% on any single rate gets a loud marker — that's either
real market news or upstream breakage, and both deserve a human look
before the numbers feed cost estimates and consent bounds.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SWING_FLAG = 0.5


def _rates(doc: dict) -> dict[str, list[float] | dict[str, Any]]:
    out: dict[str, list[float] | dict[str, Any]] = {}
    for section in ("text", "audio"):
        for model, rates in (doc.get(section) or {}).items():
            out[f"{section}:{model}"] = rates
    document_catalog = doc.get("opendocrouter") or {}
    for model in (
        *document_catalog.get("data", []),
        *document_catalog.get("retired_data", []),
    ):
        out[f"opendocrouter:{model['id']}"] = {
            "price_version": doc["opendocrouter"]["price_version"],
            "max_per_page": model["max_charge_per_page_usd"],
            "typical_per_page": model["avg_charge_per_page_usd"],
            **model["price_per_million_tokens"],
        }
    return out


def _named_rates(rates: list[float] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(rates, list):
        return {f"rate[{index}]": rate for index, rate in enumerate(rates)}
    return rates


def _models(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{provider}/{entry['id']}": {**entry, "position": position + 1}
        for provider, entries in (doc.get("providers") or {}).items()
        for position, entry in enumerate(entries)
    }


def _document_models(doc: dict) -> dict:
    return {
        f"opendocrouter/{row['id']}": {
            "status": section,
            "position": position + 1,
            **{key: row[key] for key in ("name", "version", "max_sync_pages")},
        }
        for section in ("data", "retired_data")
        for position, row in enumerate(
            (doc.get("opendocrouter") or {}).get(section, [])
        )
    }


def main() -> int:
    before_doc = json.load(open(sys.argv[1]))
    after_doc = json.load(open(sys.argv[2]))
    before = _rates(before_doc)
    after = _rates(after_doc)
    lines = [
        "Catalog refresh. Review before merge — these",
        "rows feed model selection, cost estimates and consent bounds.",
        "",
    ]
    before_models = _document_models(before_doc)
    after_models = _document_models(after_doc)
    if len(sys.argv) == 5:
        before_models.update(_models(json.load(open(sys.argv[3]))))
        after_models.update(_models(json.load(open(sys.argv[4]))))
    if before_models != after_models:
        lines.append(
            "Model metadata changes (names, capabilities and placement need review):"
        )
        for model_id in sorted(set(before_models) | set(after_models)):
            old = before_models.get(model_id)
            new = after_models.get(model_id)
            if old == new:
                continue
            if old is None:
                lines.append(f"- **model:{model_id}**: NEW {new}")
            elif new is None:
                lines.append(f"- **model:{model_id}**: REMOVED (was {old})")
            else:
                lines.append(f"- **model:{model_id}**: {old} -> {new}")
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
            if (
                type(x) in (int, float)
                and type(y) in (int, float)
                and x
                and abs(y - x) / abs(x) > SWING_FLAG
            ):
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

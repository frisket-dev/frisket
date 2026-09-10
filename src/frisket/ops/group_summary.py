"""Pure grouped-summary inputs, provider messages, and advisory quotes.

The host supplies admitted row values and owns provider dispatch, checkpoints,
and aggregate-sheet publication. These functions do not read project state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from frisket.ai.llm import estimate_tokens, model_pricing


def group_summary_rows(
    rows: Mapping[int, Mapping[str, Any]],
    *,
    source: Sequence[str],
    group_by: str | None,
) -> list[dict[str, Any]]:
    """Retain admitted row order and all contributing IDs in each group."""
    groups: dict[str, dict[str, Any]] = {}
    for row_id, row in rows.items():
        raw_group = row.get(group_by) if group_by is not None else "all"
        name = "null" if raw_group is None else str(raw_group)
        group = groups.setdefault(
            name, {"name": name, "source_row_ids": [], "rows": []}
        )
        group["source_row_ids"].append(row_id)
        group["rows"].append(
            {"row_id": row_id, "values": {column: row.get(column) for column in source}}
        )
    return list(groups.values())


def group_summary_messages(
    instruction: str, group: Mapping[str, Any]
) -> list[dict[str, str]]:
    rows = "\n".join(
        json.dumps(row, sort_keys=True, ensure_ascii=False) for row in group["rows"]
    )
    return [
        {
            "role": "system",
            "content": (
                "You synthesize grouped spreadsheet rows into one faithful "
                "summary. Preserve common themes and important outliers."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Instruction: {instruction}\n"
                f"Group: {group['name']}\n"
                f"Source row count: {len(group['source_row_ids'])}\n\n"
                f"Rows:\n{rows}"
            ),
        },
    ]


def estimate_group_summary(
    model: str, messages: Sequence[list[dict[str, str]]]
) -> dict[str, Any]:
    """Quote the same resolved messages that the host will actually dispatch."""
    token_counts = [estimate_tokens(json.dumps(value)) for value in messages]
    prompt_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
    )
    common = {
        "groups": len(messages),
        "avg_input_tokens": int(sum(token_counts) / max(1, len(token_counts))),
        "resolved_prompt_hash": prompt_hash,
    }
    pricing = model_pricing(model)
    if pricing.price is None:
        return {"cost": None, "cost_source": "unknown", **common}
    price_in, price_out = pricing.price
    cost = sum((tokens * price_in + 350 * price_out) / 1e6 for tokens in token_counts)
    return {
        "cost": round(cost, 4),
        "cost_source": pricing.cost_source,
        **(
            {"pricing_key": pricing.pricing_key}
            if pricing.pricing_key is not None
            else {}
        ),
        **common,
    }

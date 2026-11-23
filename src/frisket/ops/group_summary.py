"""Pure grouped-summary inputs, provider messages, and advisory quotes.

The host supplies admitted row values and owns provider dispatch, checkpoints,
and aggregate-sheet publication. These functions do not read project state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from frisket.ai.llm import estimate_tokens, model_pricing
from frisket.ai.llm.pricing import ModelServers


def group_summary_rows(
    rows: Mapping[int, Mapping[str, Any]],
    *,
    source: Sequence[str],
    group_by: str | None,
    group_type: str | None = None,
) -> list[dict[str, Any]]:
    """Retain admitted row order and all contributing IDs in each group."""
    groups: dict[str, dict[str, Any]] = {}
    for row_id, row in rows.items():
        name, identity_key = group_summary_identity(
            row.get(group_by) if group_by is not None else "all", group_type
        )
        group = groups.setdefault(
            identity_key,
            {
                "name": name,
                "identity_key": identity_key,
                "source_row_ids": [],
                "rows": [],
            },
        )
        group["source_row_ids"].append(row_id)
        group["rows"].append(
            {"row_id": row_id, "values": {column: row.get(column) for column in source}}
        )
    return list(groups.values())


def group_summary_identity(
    raw_group: Any, group_type: str | None = None
) -> tuple[str, str]:
    """Typed key and display label shared by memory and disk input traversal."""
    name = "null" if raw_group is None else str(raw_group)
    key_value = raw_group
    if group_type == "number" and (type(key_value) is int or type(key_value) is float):
        key_value = float(key_value)
    identity_key = json.dumps(
        [group_type or type(key_value).__name__, key_value],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return name, identity_key


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
    model: str,
    messages: Iterable[list[dict[str, str]]],
    model_servers: ModelServers = (),
) -> dict[str, Any]:
    """Quote the same resolved messages that the host will actually dispatch."""
    digest = hashlib.sha256(b"[")
    groups, total_tokens, token_cost = 0, 0, 0.0
    pricing = model_pricing(model, model_servers)
    for value in messages:
        tokens = estimate_tokens(json.dumps(value))
        if groups:
            digest.update(b",")
        digest.update(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        groups += 1
        total_tokens += tokens
        if pricing.price is not None:
            price_in, price_out = pricing.price
            token_cost += (tokens * price_in + 350 * price_out) / 1e6
    digest.update(b"]")
    common = {
        "groups": groups,
        "avg_input_tokens": int(total_tokens / max(1, groups)),
        "resolved_prompt_hash": "sha256:" + digest.hexdigest(),
    }
    if pricing.price is None:
        return {"cost": None, "cost_source": "unknown", **common}
    return {
        "cost": round(token_cost, 4),
        "cost_source": pricing.cost_source,
        **(
            {"pricing_key": pricing.pricing_key}
            if pricing.pricing_key is not None
            else {}
        ),
        **common,
    }

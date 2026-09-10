from __future__ import annotations

import re
from collections import Counter
from typing import Any

from frisket.plugins.sdk import Plugin


plugin = Plugin()
_WS = re.compile(r"\s+")


def _clean_name(value: Any) -> str | None:
    text = "" if value is None else str(value)
    text = _WS.sub(" ", text).strip(" \t\r\n,.;:")
    if not text:
        return None
    return text.title()


@plugin.op(
    kind="demo.generated_map_batch.op.clean_names",
    handler_key="clean_names",
    title="Clean names through generated-map batch",
    description="Fixture rows-scoped generated-map materialization.",
    scope="rows",
    inputs=[{"name": "input_column", "column_types": ["text", "category", "link"]}],
)
async def clean_names(ctx, rows, params):
    del ctx
    async for row in _clean_names_rows(rows, params):
        yield row


@plugin.op(
    kind="demo.generated_map_batch.op.clean_names_queued",
    handler_key="clean_names_queued",
    title="Clean names through queued generated-map batch",
    description="Fixture rows-scoped queued generated-map materialization.",
    scope="rows",
    inputs=[{"name": "input_column", "column_types": ["text", "category", "link"]}],
)
async def clean_names_queued(ctx, rows, params):
    del ctx
    async for row in _clean_names_rows(rows, params):
        yield row


async def _clean_names_rows(rows, params):
    seen: list[dict[str, Any]] = []
    async for row in rows:
        raw = row["inputs"].get("input_column")
        cleaned = _clean_name(raw)
        seen.append(
            {
                "row_id": int(row["rowId"]),
                "cleaned": cleaned,
                "key": (cleaned or "").casefold(),
            }
        )

    counts = Counter(item["key"] for item in seen if item["key"])
    total = len(seen)
    for item in seen:
        group_size = counts.get(item["key"], 0)
        confidence = round(group_size / total, 4) if total and group_size else 0.0
        justification = f"batch_total={total}; group_size={group_size}"
        yield {
            "rowId": item["row_id"],
            "outputs": {
                "value": item["cleaned"],
                "confidence": confidence,
                "justification": justification,
            },
            "result": {
                "outcome": "ok",
                "confidence": confidence,
                "justification": justification,
            },
        }

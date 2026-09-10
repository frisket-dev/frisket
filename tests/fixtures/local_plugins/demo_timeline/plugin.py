from __future__ import annotations

from typing import Any

from frisket.plugins.sdk import Plugin


plugin = Plugin()


@plugin.projection(
    "timeline",
    title="Build timeline projection",
    description="Builds timeline items from date/title/case row inputs.",
)
async def timeline(ctx, rows: list[dict[str, Any]], *, target, params):
    del ctx, target, params
    for row in rows:
        inputs = row.get("inputs") if isinstance(row, dict) else {}
        if not isinstance(inputs, dict):
            continue
        date = str(inputs.get("date") or "").strip()
        if not date:
            continue
        title = str(inputs.get("title") or "").strip()
        if not title:
            title = f"Row {row.get('rowId')}"
        item = {
            "sourceRowId": int(row["rowId"]),
            "date": date,
            "title": title,
        }
        case_id = inputs.get("case_id")
        if case_id is not None and str(case_id).strip():
            item["caseId"] = str(case_id).strip()
        yield item

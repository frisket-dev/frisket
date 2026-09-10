from __future__ import annotations

import json
import re
from typing import Any

from frisket.plugins.sdk import Plugin


plugin = Plugin()


CASE_ID_RE = re.compile(r"^(?:CASE-\d{4}|case[\s_-]*\d{1,4})$", re.IGNORECASE)


@plugin.importer(
    "cases",
    title="Import case NDJSON",
    columns=[
        {"name": "case_id", "type": "case_id"},
        {"name": "title", "type": "text"},
        {"name": "summary", "type": "text"},
        {"name": "priority", "type": "integer"},
    ],
)
async def import_cases(ctx, source, **_params: Any):
    async for line_no, line in source.text_lines(numbered=True):
        text = line.strip()
        if not text:
            ctx.warning(
                "blank_line",
                "Blank NDJSON line was skipped",
                attach=ctx.source_line(line_no),
            )
            continue
        try:
            record = json.loads(text)
        except ValueError as exc:
            raise ctx.error(
                "invalid_json",
                "Case import line is not valid JSON",
                attach=ctx.source_line(line_no),
            ) from exc
        if not isinstance(record, dict):
            raise ctx.error(
                "invalid_case_object",
                "Case import line must contain a JSON object",
                attach=ctx.source_line(line_no),
            )

        raw_case_id = record.get("case_id", record.get("id"))
        case_id = str(raw_case_id or "").strip()
        if not CASE_ID_RE.fullmatch(case_id):
            raise ctx.error(
                "invalid_case_id",
                "Case id must look like CASE-0000",
                attach=ctx.source_line(
                    line_no,
                    field="case_id",
                    object_id=case_id or None,
                ),
            )

        title = str(record.get("title") or record.get("name") or "").strip()
        if not title:
            ctx.warning(
                "missing_title",
                "Case title is empty",
                attach=ctx.source_line(
                    line_no,
                    field="title",
                    object_id=case_id,
                ),
            )

        summary_value = record.get("summary")
        summary = str(summary_value).strip() if summary_value is not None else ""
        if not summary:
            ctx.warning(
                "missing_summary",
                "Case summary is empty",
                attach=ctx.source_line(
                    line_no,
                    field="summary",
                    object_id=case_id,
                ),
            )

        priority = record.get("priority", 0)
        if isinstance(priority, bool):
            priority = 0
        try:
            priority = int(priority)
        except (TypeError, ValueError) as exc:
            raise ctx.error(
                "invalid_priority",
                "Case priority must be an integer",
                attach=ctx.source_line(
                    line_no,
                    field="priority",
                    object_id=case_id,
                ),
            ) from exc

        yield {
            "case_id": case_id,
            "title": title,
            "summary": summary,
            "priority": priority,
        }

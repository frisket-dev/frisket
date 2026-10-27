"""Bounded source-link labels for the cells already in a grid page."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote


def add_source_link_labels(project: Any, columns, data, meta) -> None:
    targets: dict[str, list[tuple[int, str]]] = {}
    link_columns = [str(column["id"]) for column in columns if column["type"] == "link"]
    for row_id, cells in data.items():
        for column_id in link_columns:
            value = cells.get(column_id)
            if not isinstance(value, str):
                continue
            match = re.fullmatch(
                r"/p/[^/?#]+/evidence/([^/?#]+)(?:\?page=[1-9][0-9]*)?", value
            )
            if match:
                targets.setdefault(unquote(match[1]), []).append((row_id, column_id))
    keys = list(targets)
    for offset in range(0, len(keys), 400):
        batch = keys[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        # Only metadata is read: no artifact text, page images or full viewer.
        for row in project.db.execute(
            "SELECT link.stable_id,substr(artifact.filename,1,240) AS filename,"
            "span.page_start,span.page_end FROM evidence_links link "
            "JOIN evidence_link_spans member ON member.link_id=link.id "
            "JOIN source_spans span ON span.id=member.span_id "
            "JOIN source_artifacts artifact ON artifact.id=span.artifact_id "
            f"WHERE link.stable_id IN ({placeholders}) "
            "AND link.link_role='source_provenance' AND artifact.media_type='application/pdf' "
            "ORDER BY link.id,span.id",
            batch,
        ):
            label = str(row["filename"] or "Original PDF")
            if row["page_start"] is not None:
                first, last = int(row["page_start"]), row["page_end"]
                label += (
                    f" · p {first}"
                    if last is None or last == first
                    else f" · pp {first}–{last}"
                )
            for row_id, column_id in targets.pop(row["stable_id"], ()):
                meta[row_id].setdefault(column_id, {})["source_link"] = {
                    "label": label[:300]
                }

"""Bounded source inspection for local search preparation plans and previews."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from frisket.engine.pdf_text import PdfTextError, extract_pdf_text
from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError

_GROUP_ORDER = ("native_pdf", "ocr", "transcribe")
_SCAN_PAGE = 200


def build_plan(
    project: Any,
    *,
    sheet_id: int,
    row_ids: Sequence[int] | None,
    force_ocr: bool,
    requirements: Sequence[Any],
    requirement_dict: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    columns = [
        column
        for column in project.columns(sheet_id)
        if column["type"] in {"file", "image", "audio", "video"}
        and not dict(column).get("hidden", False)
    ]
    selected = project.visible_row_ids(
        sheet_id, None if row_ids is None else list(row_ids)
    )
    if row_ids is not None and set(selected) != set(row_ids):
        raise RouteError(404, "one or more selected rows are not visible in this sheet")
    items: list[dict[str, Any]] = []
    counts: defaultdict[str, int] = defaultdict(int)
    for offset in range(0, len(selected), _SCAN_PAGE):
        page_rows = selected[offset : offset + _SCAN_PAGE]
        for column in columns:
            for selected_row, value in project.get_values(
                sheet_id, int(column["id"]), row_ids=page_rows
            ).items():
                if not isinstance(value, dict) or not isinstance(
                    value.get("blob"), str
                ):
                    continue
                source = resolve_cell_blob_source(
                    project,
                    sheet_id=sheet_id,
                    row_id=selected_row,
                    column_id=int(column["id"]),
                )
                if source is None:
                    continue
                mime = source.mime_type.lower()
                kind: str | None = None
                page_count: int | None = None
                if mime == "application/pdf":
                    native, page_count = _pdf_has_complete_text(
                        project, source.blob_hash, source.page_number
                    )
                    kind = "ocr" if force_ocr or not native else "native_pdf"
                elif mime.startswith("image/"):
                    kind = "ocr"
                elif mime.startswith("audio/") or mime.startswith("video/"):
                    kind = "transcribe"
                if kind is None:
                    continue
                if not (
                    force_ocr and mime == "application/pdf" and kind == "ocr"
                ) and _has_current_result(project, source, kind):
                    continue
                items.append(
                    {
                        "kind": kind,
                        "row_id": selected_row,
                        "column_id": int(column["id"]),
                        "column_name": str(column["name"]),
                        "blob_hash": source.blob_hash,
                        "mime": mime,
                        "filename": value.get("filename"),
                        "page_number": source.page_number,
                        "page_count": page_count,
                        "retrieval_revision": source.binding.retrieval_revision,
                    }
                )
                counts[kind] += 1
    capabilities = {
        capability
        for kind, capability in (("ocr", "ocr"), ("transcribe", "transcription"))
        if counts[kind]
    }
    models = []
    for requirement in requirements:
        capability = str(requirement.capability)
        if capability in capabilities:
            models.append(requirement_dict(requirement, required=True))
        elif capability == "semantic_search":
            models.append(requirement_dict(requirement, required=False))
    return {
        "source_cutoff": int(project.op_cursor),
        "force_ocr": force_ocr,
        "groups": [
            {"kind": kind, "count": counts[kind]}
            for kind in _GROUP_ORDER
            if counts[kind]
        ],
        "models": models,
        "items": items,
    }


def _pdf_has_complete_text(
    project: Any, blob_hash: str, page_number: int | None
) -> tuple[bool, int]:
    try:
        with project.materialize_blob(blob_hash) as source:
            pages = asyncio.run(extract_pdf_text(Path(source)))
    except (PdfTextError, OSError):
        return False, 1
    selected = (
        pages if page_number is None else [p for p in pages if p.page == page_number]
    )
    return bool(selected) and all(
        any(token.text.strip() for token in p.tokens) for p in selected
    ), len(pages)


def _has_current_result(project: Any, source: Any, kind: str) -> bool:
    prepared = project.db.execute(
        "SELECT 1 FROM current_cell_values current "
        "JOIN prepared_content_refs ref ON ref.id=current.prepared_ref_id "
        "JOIN prepared_content_sets content ON content.id=ref.set_id "
        "JOIN source_artifacts artifact ON artifact.id=content.source_artifact_id "
        "JOIN columns output_column ON output_column.id=current.column_id "
        "AND output_column.sheet_id=? AND output_column.hidden=0 "
        "WHERE current.row_id=? AND current.value_kind='text' "
        "AND length(trim(current.value))>0 AND artifact.blob_hash=? "
        "AND artifact.source_sheet_id=? AND artifact.source_row_id=? "
        "AND artifact.source_column_id=? AND ref.page_number IS ? LIMIT 1",
        (
            source.sheet_id,
            source.binding.row_id,
            source.blob_hash,
            source.sheet_id,
            source.binding.row_id,
            source.binding.column_id,
            source.page_number,
        ),
    ).fetchone()
    if prepared is not None:
        return True
    if kind != "transcribe":
        return False
    candidates = project.db.execute(
        "SELECT DISTINCT l.column_id,a.id AS artifact_id "
        "FROM source_artifacts a JOIN source_spans s ON s.artifact_id=a.id "
        "JOIN evidence_link_spans ls ON ls.span_id=s.id "
        "JOIN evidence_links l ON l.id=ls.link_id "
        "WHERE a.blob_hash=? AND a.source_sheet_id=? AND a.source_row_id=? "
        "AND a.source_column_id=? AND l.status='active' "
        "AND l.link_role='media_transcribe_temporal'",
        (
            source.blob_hash,
            source.sheet_id,
            source.binding.row_id,
            source.binding.column_id,
        ),
    ).fetchall()
    from frisket.engine.executor.temporal_transcripts import (
        resolve_timestamped_transcript,
    )

    return any(
        resolve_timestamped_transcript(
            project,
            sheet_id=source.sheet_id,
            row_id=source.binding.row_id,
            column_id=int(row["column_id"]),
            artifact_id=int(row["artifact_id"]),
        )
        is not None
        for row in candidates
        if row["column_id"] is not None
    )


def sample_pdf(
    project: Any, sheet_id: int, item: Mapping[str, Any], page: int
) -> tuple[str, int, str]:
    row = project.db.execute(
        "SELECT current.prepared_ref_id FROM current_cell_values current "
        "JOIN prepared_content_refs ref ON ref.id=current.prepared_ref_id "
        "JOIN prepared_content_sets content ON content.id=ref.set_id "
        "JOIN source_artifacts artifact ON artifact.id=content.source_artifact_id "
        "JOIN columns output_column ON output_column.id=current.column_id "
        "AND output_column.sheet_id=? AND output_column.hidden=0 "
        "WHERE current.row_id=? AND current.value_kind='text' "
        "AND length(trim(current.value))>0 AND artifact.blob_hash=? "
        "AND artifact.source_sheet_id=? AND artifact.source_row_id=? "
        "AND artifact.source_column_id=? AND ref.page_number IS ? LIMIT 1",
        (
            sheet_id,
            item["row_id"],
            item["blob_hash"],
            sheet_id,
            item["row_id"],
            item["column_id"],
            item.get("page_number"),
        ),
    ).fetchone()
    if row is not None:
        prepared = PreparedContentStore(project).resolve(int(row[0]))
        pin = next((pin for pin in prepared.pins if pin.page_number == page), None)
        if pin is not None:
            return prepared.text[pin.start : pin.end], len(prepared.pins), "prepared"
    with project.materialize_blob(str(item["blob_hash"])) as source:
        pages = asyncio.run(extract_pdf_text(Path(source)))
    selected = next((value for value in pages if value.page == page), None)
    if selected is None:
        raise RouteError(422, "PDF page is outside the document")
    return " ".join(token.text for token in selected.tokens), len(pages), "native"


__all__ = ["build_plan", "sample_pdf"]

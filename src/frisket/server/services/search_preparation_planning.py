"""Bounded source inspection for local search preparation plans and previews."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from frisket.engine.pdf_text import (
    PdfTextError,
    extract_pdf_text,
    positioned_tokens_text,
)
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.engine.store.prepared_readiness import prepared_source_readiness_ctes
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
    intersect_visible_rows: bool = False,
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
    if (
        row_ids is not None
        and not intersect_visible_rows
        and set(selected) != set(row_ids)
    ):
        raise RouteError(404, "one or more selected rows are not visible in this sheet")
    items: list[dict[str, Any]] = []
    counts: defaultdict[str, int] = defaultdict(int)
    pdf_cache: dict[str, list[Any] | None] = {}
    for offset in range(0, len(selected), _SCAN_PAGE):
        page_rows = selected[offset : offset + _SCAN_PAGE]
        bindings = []
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
                bindings.append((column, selected_row, value, source))
        pdf_pages = {
            (selected_row, source.blob_hash, source.page_number)
            for _column, selected_row, _value, source in bindings
            if source.mime_type.lower() == "application/pdf"
        }
        for column, selected_row, value, source in bindings:
            mime = source.mime_type.lower()
            kind: str | None = None
            page_count: int | None = None
            if mime == "application/pdf":
                if not force_ocr and _has_current_result(project, source, "native_pdf"):
                    continue
                native, page_count = _pdf_has_complete_text(
                    project,
                    source.blob_hash,
                    source.page_number,
                    cache=pdf_cache,
                )
                kind = "ocr" if force_ocr or not native else "native_pdf"
            elif mime.startswith("image/"):
                imported_page = _imported_pdf_page_source(project, source)
                if (
                    imported_page is not None
                    and (
                        selected_row,
                        imported_page[0],
                        imported_page[1],
                    )
                    in pdf_pages
                ):
                    continue
                kind = "ocr"
            elif mime.startswith("audio/") or mime.startswith("video/"):
                kind = "transcribe"
            if kind is None:
                continue
            if mime != "application/pdf" and _has_current_result(project, source, kind):
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
    project: Any,
    blob_hash: str,
    page_number: int | None,
    *,
    cache: dict[str, list[Any] | None],
) -> tuple[bool, int]:
    pages = _pdf_pages(project, blob_hash, cache=cache)
    if pages is None:
        return False, 1
    selected = (
        pages if page_number is None else [p for p in pages if p.page == page_number]
    )
    if page_number is not None and not selected:
        raise RouteError(422, "PDF page is outside the document")
    return bool(selected) and all(
        any(token.text.strip() for token in p.tokens) for p in selected
    ), len(pages)


def _pdf_pages(
    project: Any,
    blob_hash: str,
    *,
    cache: dict[str, list[Any] | None],
) -> list[Any] | None:
    if blob_hash in cache:
        return cache[blob_hash]
    try:
        with project.materialize_blob(blob_hash) as source:
            pages = run_media_sync(lambda _cancelled: extract_pdf_text(Path(source)))
    except (PdfTextError, OSError):
        pages = None
    cache[blob_hash] = pages
    return pages


def _imported_pdf_page_source(project: Any, source: Any) -> tuple[str, int] | None:
    row = project.db.execute(
        "SELECT artifact.blob_hash, span.page_start "
        "FROM evidence_links link "
        "JOIN evidence_link_spans linked ON linked.link_id=link.id "
        "JOIN source_spans span ON span.id=linked.span_id "
        "JOIN source_artifacts artifact ON artifact.id=span.artifact_id "
        "WHERE link.status='active' AND link.link_role='primary_support' "
        "AND link.subject_kind='cell_value' AND link.sheet_id=? "
        "AND link.row_id=? AND link.column_id=? "
        "AND artifact.media_type='application/pdf' AND span.span_kind='page' "
        "AND span.page_start=span.page_end "
        "AND json_extract(span.preview_json, '$.blob_hash')=? "
        "ORDER BY link.id DESC LIMIT 1",
        (
            source.sheet_id,
            source.binding.row_id,
            source.binding.column_id,
            source.blob_hash,
        ),
    ).fetchone()
    if row is None or row["blob_hash"] is None or row["page_start"] is None:
        return None
    return str(row["blob_hash"]), int(row["page_start"])


def _has_current_result(project: Any, source: Any, kind: str) -> bool:
    prepared = _current_prepared_result(
        project,
        sheet_id=source.sheet_id,
        row_id=source.binding.row_id,
        column_id=source.binding.column_id,
        blob_hash=source.blob_hash,
        page_number=source.page_number,
    )
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


def _current_prepared_result(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    blob_hash: str,
    page_number: int | None,
) -> Any:
    return project.db.execute(
        "WITH prepared_readiness_scope(sheet_id,row_id) AS (VALUES (?,?)), "
        f"{prepared_source_readiness_ctes(scope_relation='prepared_readiness_scope')} "
        "SELECT prepared_ref_id,page_count FROM current_prepared_sources "
        "WHERE source_sheet_id=? AND source_row_id=? AND source_column_id=? "
        "AND blob_hash=? AND page_number IS ? "
        "ORDER BY prepared_ref_id DESC LIMIT 1",
        (
            sheet_id,
            row_id,
            sheet_id,
            row_id,
            column_id,
            blob_hash,
            page_number,
        ),
    ).fetchone()


def sample_pdf(
    project: Any, sheet_id: int, item: Mapping[str, Any], page: int
) -> tuple[str, int, str]:
    selected_page = item.get("page_number")
    if selected_page is not None and page != selected_page:
        raise RouteError(422, "preview must use the selected PDF page")
    row = _current_prepared_result(
        project,
        sheet_id=sheet_id,
        row_id=int(item["row_id"]),
        column_id=int(item["column_id"]),
        blob_hash=str(item["blob_hash"]),
        page_number=item.get("page_number"),
    )
    if row is not None:
        prepared = PreparedContentStore(project).resolve(int(row["prepared_ref_id"]))
        pin = next((pin for pin in prepared.pins if pin.page_number == page), None)
        if pin is not None:
            page_count = row["page_count"] or item.get("page_count")
            return (
                prepared.text[pin.start : pin.end],
                int(page_count) if page_count else len(prepared.pins),
                "prepared",
            )
    with project.materialize_blob(str(item["blob_hash"])) as source:
        pages = run_media_sync(lambda _cancelled: extract_pdf_text(Path(source)))
    selected = next((value for value in pages if value.page == page), None)
    if selected is None:
        raise RouteError(422, "PDF page is outside the document")
    page_count = item.get("page_count")
    return (
        positioned_tokens_text(selected.tokens),
        int(page_count) if page_count else len(pages),
        "native",
    )


__all__ = ["build_plan", "sample_pdf"]

"""Bounded source inspection for local search preparation plans and previews."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from frisket.engine.pdf_text import (
    PdfTextError,
    PdfTextSample,
    sample_pdf_text,
)
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.store.blob_backend import BlobIntegrityError, BlobNotFoundError
from frisket.engine.store.current_value_windows import read_sql_text
from frisket.engine.store.prepared_readiness import prepared_source_readiness_ctes
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError

_GROUP_ORDER = ("native_pdf", "ocr", "transcribe")
_SCAN_PAGE = 200
_PREVIEW_CHARS = 12_000
_PLAN_WARNING_LIMIT = 20


def capture_sources(
    project: Any, *, sheet_id: int, row_ids: Sequence[int] | None
) -> list[dict[str, Any]]:
    """Capture exact automatic follow-up bindings before any PDF preview IO."""
    selected = project.visible_row_ids(
        sheet_id, None if row_ids is None else list(row_ids)
    )
    columns = [
        c
        for c in project.columns(sheet_id)
        if c["type"] in {"file", "image", "audio", "video"}
        and not dict(c).get("hidden", False)
    ]
    sources = []
    for offset in range(0, len(selected), _SCAN_PAGE):
        for column in columns:
            for row_id, value in project.get_values(
                sheet_id,
                int(column["id"]),
                row_ids=selected[offset : offset + _SCAN_PAGE],
            ).items():
                if not isinstance(value, dict) or not isinstance(
                    value.get("blob"), str
                ):
                    continue
                source = resolve_cell_blob_source(
                    project,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=int(column["id"]),
                )
                if source is not None:
                    sources.append(
                        {
                            "row_id": row_id,
                            "column_id": int(column["id"]),
                            "blob_hash": source.blob_hash,
                            "page_number": source.page_number,
                            "retrieval_revision": source.binding.retrieval_revision,
                        }
                    )
    return sources


def build_plan(
    project: Any,
    *,
    sheet_id: int,
    row_ids: Sequence[int] | None,
    force_ocr: bool,
    requirements: Sequence[Any],
    requirement_dict: Callable[..., dict[str, Any]],
    intersect_visible_rows: bool = False,
    source_cells: frozenset[tuple[int, int]] | None = None,
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
    sources: list[dict[str, Any]] = []
    warnings: dict[str, None] = {}
    warnings_omitted = False
    first_preview_error: RouteError | None = None
    counts: defaultdict[str, int] = defaultdict(int)
    pdf_cache: dict[tuple[str, int], PdfTextSample] = {}
    for offset in range(0, len(selected), _SCAN_PAGE):
        page_rows = selected[offset : offset + _SCAN_PAGE]
        bindings = []
        for column in columns:
            for selected_row, value in project.get_values(
                sheet_id, int(column["id"]), row_ids=page_rows
            ).items():
                if (
                    source_cells is not None
                    and (selected_row, int(column["id"])) not in source_cells
                ):
                    continue
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
        for _column, selected_row, _value, source in bindings:
            if (
                source.mime_type.lower() != "application/pdf"
                or source.page_number is not None
            ):
                continue
            original_page = _imported_pdf_child_page_source(project, source)
            if original_page is not None:
                pdf_pages.add((selected_row, *original_page))
        for column, selected_row, value, source in bindings:
            mime = source.mime_type.lower()
            kind: str | None = None
            page_count: int | None = None
            source_item = {
                "row_id": selected_row,
                "column_id": int(column["id"]),
                "column_name": str(column["name"]),
                "blob_hash": source.blob_hash,
                "mime": mime,
                "filename": value.get("filename"),
                "page_number": source.page_number,
                "retrieval_revision": source.binding.retrieval_revision,
            }
            if mime == "application/pdf":
                if not force_ocr and _has_current_result(project, source, "native_pdf"):
                    continue
                try:
                    sample = _pdf_sample(
                        project,
                        source.blob_hash,
                        source.page_number or 1,
                        cache=pdf_cache,
                    )
                except RouteError as exc:
                    if exc.status_code != 422:
                        raise
                    sources.append(source_item)
                    first_preview_error = first_preview_error or exc
                    filename = str(value.get("filename") or f"File row {selected_row}")[
                        :200
                    ]
                    warning = f"Skipped {filename}: PDF text preview could not be read."
                    if warning not in warnings:
                        if len(warnings) < _PLAN_WARNING_LIMIT:
                            warnings[warning] = None
                        else:
                            warnings_omitted = True
                    continue
                page_count = sample.page_count
                # This is evidence about the sampled page, not a claim that
                # every page contains usable native text. The user reviews it
                # before choosing whether to keep native text or run OCR.
                kind = (
                    "ocr"
                    if force_ocr or (not sample.text.strip() and not sample.truncated)
                    else "native_pdf"
                )
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
            sources.append(source_item)
            items.append(
                {
                    **source_item,
                    "kind": kind,
                    "page_count": page_count,
                }
            )
            counts[kind] += 1
    if not items and first_preview_error is not None:
        raise first_preview_error
    warning_messages = list(warnings)
    if warnings_omitted:
        warning_messages[-1] = "Additional unreadable PDF files were skipped."
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
        "sources": sources,
        "warnings": warning_messages,
    }


def _pdf_sample(
    project: Any,
    blob_hash: str,
    page: int,
    *,
    cache: dict[tuple[str, int], PdfTextSample],
) -> PdfTextSample:
    key = (blob_hash, page)
    if key in cache:
        return cache[key]
    try:
        with project.materialize_blob(blob_hash) as source:
            sample = run_media_sync(
                lambda cancelled: sample_pdf_text(
                    Path(source),
                    page=page,
                    max_chars=_PREVIEW_CHARS,
                    should_cancel=cancelled,
                )
            )
    except (PdfTextError, OSError, BlobIntegrityError, BlobNotFoundError) as exc:
        raise RouteError(
            422, "PDF text preview could not be read. Check the PDF and selected page."
        ) from exc
    cache[key] = sample
    return sample


def _imported_pdf_child_page_source(
    project: Any, source: Any
) -> tuple[str, int] | None:
    """Recognize a physical one-page child beside its original's page image."""
    row = project.db.execute(
        "SELECT original.blob_hash, span.page_start "
        "FROM evidence_links link "
        "JOIN evidence_link_spans linked ON linked.link_id=link.id "
        "JOIN source_spans span ON span.id=linked.span_id "
        "JOIN source_artifacts original ON original.id=span.artifact_id "
        "WHERE link.status='active' AND link.link_role='source_provenance' "
        "AND link.subject_kind='cell_value' AND link.sheet_id=? "
        "AND link.row_id=? AND link.column_id=? "
        "AND original.media_type='application/pdf' AND span.span_kind='page_range' "
        "AND span.page_start=span.page_end "
        "AND EXISTS (SELECT 1 FROM source_artifacts child "
        "WHERE child.blob_hash=? AND child.media_type='application/pdf' "
        "AND child.page_count=1 AND child.source_sheet_id=link.sheet_id "
        "AND child.source_row_id=link.row_id AND child.source_column_id=link.column_id) "
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
    if prepared is not None and prepared["has_text"] and prepared["coverage_complete"]:
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
        "SELECT prepared_ref_id,page_count,has_text,coverage_complete FROM current_prepared_sources "
        "WHERE source_sheet_id=? AND source_row_id=? AND source_column_id=? "
        "AND blob_hash=? AND page_number IS ? "
        "ORDER BY (has_text AND coverage_complete) DESC, prepared_ref_id DESC LIMIT 1",
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
) -> tuple[str, int, str, bool]:
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
    # Flat manual Text is ready, but has no trustworthy physical-page partition.
    # This page-inspection preview may still show explicitly labelled native text.
    if row is not None and row["prepared_ref_id"] is not None:
        params = {"ref": int(row["prepared_ref_id"]), "page": page}
        select_value = (
            "SELECT version.prepared_text AS value FROM prepared_content_refs ref "
            "JOIN prepared_content_set_pages member ON member.set_id=ref.set_id "
            "AND member.page_number=:page "
            "JOIN prepared_page_versions version ON version.id=member.version_id "
            "WHERE ref.id=:ref AND (ref.page_number IS NULL OR ref.page_number=:page)"
        )
        sizes = project.db.execute(
            "SELECT length(value),length(CAST(value AS BLOB)),instr(value,char(0))>0 "
            "FROM (" + select_value + ")",
            params,
        ).fetchone()
        if sizes is not None:
            # The text reader also preserves embedded NUL characters. Only a
            # prefix of this exact page reaches Python, including one character
            # beyond the preview limit to establish whether it was truncated.
            prefix = read_sql_text(
                project.db,
                select_value,
                params,
                start=0,
                limit=_PREVIEW_CHARS + 1,
                sizes=(int(sizes[0]), int(sizes[1]), bool(sizes[2])),
            )
            page_count = row["page_count"] or item.get("page_count") or page
            return (
                prefix[:_PREVIEW_CHARS],
                int(page_count),
                "prepared",
                len(prefix) > _PREVIEW_CHARS,
            )
    sample = _pdf_sample(project, str(item["blob_hash"]), page, cache={})
    return (
        sample.text,
        sample.page_count,
        "native",
        sample.truncated,
    )


__all__ = ["build_plan", "sample_pdf"]

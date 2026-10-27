"""Bounded text inspection for admitted existing PDF occurrences."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

from frisket.documents.text_quality import assess_text_quality
from frisket.engine.pdf_text import PdfTextCancelled, PdfTextError, sample_pdf_text
from frisket.engine.store.current_value_windows import read_sql_text
from frisket.engine.store.document_text import (
    DocumentTextUnavailable,
    resolve_document_text_selection,
    document_text_role,
)
from frisket.server.services.pdf_imports.analysis import (
    MAX_SAMPLE_CHARS,
    representative_pages,
)
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource


@dataclass(frozen=True)
class PacketTextPage:
    text: str
    source: Literal["prepared", "native"]
    truncated: bool
    completed_ocr: bool = False


@dataclass(frozen=True)
class PacketTextInspection:
    category: Literal["text", "missing", "suspicious", "unreadable"]
    pages: dict[int, PacketTextPage]
    manual_text: str | None = None


def classify_packet_text(
    texts: list[str], *, completed_empty_ocr: bool = False
) -> Literal["text", "missing", "suspicious"]:
    text = "\n".join(texts)[:MAX_SAMPLE_CHARS]
    if not text.strip():
        return "text" if completed_empty_ocr else "missing"
    flags = assess_text_quality(text)
    return (
        "suspicious" if any(str(flag).lower() != "empty" for flag in flags) else "text"
    )


def read_prepared_page(project: Any, ref_id: int, page: int) -> PacketTextPage | None:
    select = (
        "SELECT version.prepared_text AS value FROM prepared_content_refs ref "
        "JOIN prepared_content_set_pages member ON member.set_id=ref.set_id AND member.page_number=:page "
        "JOIN prepared_page_versions version ON version.id=member.version_id "
        "WHERE ref.id=:ref AND (ref.page_number IS NULL OR ref.page_number=:page)"
    )
    params = {"ref": ref_id, "page": page}
    sizes = project.db.execute(
        "SELECT length(value),length(CAST(value AS BLOB)),instr(value,char(0))>0 FROM ("
        + select
        + ")",
        params,
    ).fetchone()
    if sizes is None:
        return None
    text = read_sql_text(
        project.db,
        select,
        params,
        start=0,
        limit=MAX_SAMPLE_CHARS + 1,
        sizes=(int(sizes[0]), int(sizes[1]), bool(sizes[2])),
    )
    origin = project.db.execute(
        "SELECT EXISTS(SELECT 1 FROM prepared_content_refs ref JOIN prepared_content_set_pages member ON member.set_id=ref.set_id AND member.page_number=? JOIN prepared_page_versions version ON version.id=member.version_id WHERE ref.id=? AND (EXISTS(SELECT 1 FROM runs run WHERE run.op_id=version.producing_op_id AND run.action_kind='media.ocr') OR (json_extract(version.positions_json,'$.engine') IS NOT NULL AND json_extract(version.positions_json,'$.engine') NOT IN ('native','pdf_native','native_pdf_text'))))",
        (page, ref_id),
    ).fetchone()[0]
    return PacketTextPage(
        text[:MAX_SAMPLE_CHARS], "prepared", len(text) > MAX_SAMPLE_CHARS, bool(origin)
    )


def refresh_packet_source(
    project: Any, source: AdmittedPacketSource
) -> tuple[AdmittedPacketSource, tuple[int | None, int | None]]:
    """Recheck the frozen file cell and refresh its current prepared selection."""
    if (
        source.source_sheet_id is None
        or source.source_row_id is None
        or source.source_column_id is None
    ):
        return source, (None, source.prepared_ref_id)
    if (
        project.get_values(
            source.source_sheet_id,
            source.source_column_id,
            row_ids=[source.source_row_id],
        ).get(source.source_row_id)
        != source.frozen_value
    ):
        raise ValueError("source file changed; refresh setup")
    role = document_text_role(project, source_column_id=source.source_column_id)
    revision = None
    if role is not None:
        row = project.db.execute(
            "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
            (source.source_row_id, role.text_column_id),
        ).fetchone()
        revision = int(row[0]) if row is not None else None
    try:
        selection = resolve_document_text_selection(
            project,
            (source.source_sheet_id, source.source_row_id, source.source_column_id),
        )
    except DocumentTextUnavailable:
        selection = None
    ref = selection.prepared_ref_id if selection is not None else None
    pages = ()
    if ref is not None:
        pages = tuple(
            int(row[0])
            for row in project.db.execute(
                "SELECT member.page_number FROM prepared_content_refs ref JOIN prepared_content_set_pages member ON member.set_id=ref.set_id WHERE ref.id=? AND member.page_number BETWEEN ? AND ? AND (ref.page_number IS NULL OR ref.page_number=member.page_number) ORDER BY member.page_number",
                (ref, source.page_start, source.page_end),
            )
        )
    return replace(source, prepared_ref_id=ref, prepared_pages=pages), (revision, ref)


def _manual_text(project: Any, source: AdmittedPacketSource) -> str | None:
    if (
        source.source_sheet_id is None
        or source.source_row_id is None
        or source.source_column_id is None
    ):
        return None
    try:
        selection = resolve_document_text_selection(
            project,
            (source.source_sheet_id, source.source_row_id, source.source_column_id),
        )
    except DocumentTextUnavailable:
        return None
    if selection is None or selection.prepared_ref_id is not None:
        return None
    if (
        project.get_values(
            source.source_sheet_id,
            source.source_column_id,
            row_ids=[source.source_row_id],
        ).get(source.source_row_id)
        != source.frozen_value
    ):
        return None
    row = project.db.execute(
        "SELECT substr(value,1,?) FROM current_cell_values WHERE column_id=? AND row_id=? AND validity='valid' AND value_kind='text'",
        (MAX_SAMPLE_CHARS, selection.text_cell[2], source.source_row_id),
    ).fetchone()
    return None if row is None or not isinstance(row[0], str) else row[0]


def sample_packet_page(
    project: Any,
    source: AdmittedPacketSource,
    page: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> PacketTextPage:
    if not source.page_start <= page <= source.page_end:
        raise ValueError("page outside source extent")
    if source.prepared_ref_id is not None:
        cached = read_prepared_page(project, source.prepared_ref_id, page)
        if cached is not None:
            return cached
    with source.open_source() as path:
        sample = asyncio.run(
            sample_pdf_text(
                path,
                page=page,
                max_chars=MAX_SAMPLE_CHARS,
                timeout_seconds=30,
                should_cancel=should_cancel,
            )
        )
    return PacketTextPage(sample.text, "native", sample.truncated)


def inspect_packet_source(
    project: Any,
    source: AdmittedPacketSource,
    *,
    should_cancel: Callable[[], bool] | None = None,
    known_pages: dict[int, PacketTextPage] | None = None,
) -> PacketTextInspection:
    manual = _manual_text(project, source)
    selected = [
        source.page_start + n - 1
        for n in representative_pages(source.page_end - source.page_start + 1)
    ]
    try:
        pages = {
            page: (known_pages or {}).get(page)
            or sample_packet_page(project, source, page, should_cancel=should_cancel)
            for page in selected
        }
    except PdfTextCancelled:
        raise
    except (PdfTextError, OSError):
        return PacketTextInspection("unreadable", {}, manual)
    return PacketTextInspection(
        classify_packet_text(
            [page.text for page in pages.values()],
            completed_empty_ocr=bool(pages)
            and all(page.completed_ocr for page in pages.values()),
        ),
        pages,
        manual,
    )

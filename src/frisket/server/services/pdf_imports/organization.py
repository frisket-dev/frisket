"""Admit reviewed PDF inventory to the shared page/split workflow."""

from __future__ import annotations

import os
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import FileLock

from frisket.server.services.pdf_packet_sources import (
    AdmittedPacketSource,
    MAX_INTERACTIVE_PDF_PAGES,
)

from .inventory import PdfDraftInventory
from .storage import PdfImportDraftError

if TYPE_CHECKING:
    from .service import PdfImportService


def reviewed_sources(
    service: PdfImportService,
    project_id: str,
    draft_id: str,
    expected_revision: int,
    organization: str | None = None,
) -> tuple[AdmittedPacketSource, ...]:
    directory, _ = service._open(project_id, draft_id)
    sources = []
    with (
        service._lock(directory),
        PdfDraftInventory(directory / "inventory.db") as inventory,
    ):
        header = service._header(project_id, directory)
        service._require_state(header, "open")
        service._revision(header, expected_revision)
        if (
            organization is not None
            and header["choices"]["organization"] != organization
        ):
            raise PdfImportDraftError(
                409, "PDF organization changed; reopen the review"
            )
        stats = inventory.stats()
        if (
            not stats["manifest_sealed"]
            or stats["analyzed_count"] != header["expected_files"]
        ):
            raise PdfImportDraftError(409, "Wait for the files to finish checking")
        after = 0
        while items := inventory.staged_page(after=after, limit=256):
            for item in items:
                after = int(item["ordinal"])
                if (
                    item["media_kind"] != "pdf"
                    or item["matched"]
                    or not item["page_count"]
                    or item["error"]
                ):
                    continue
                key, digest, size = item["occurrence_id"], item["sha256"], item["size"]
                sources.append(
                    AdmittedPacketSource(
                        key=key,
                        sha256=digest,
                        filename=item["filename"],
                        size=size,
                        page_count=item["page_count"],
                        page_start=1,
                        page_end=item["page_count"],
                        source_url=item.get("source_url"),
                        open_source=lambda key=key, digest=digest, size=size: (
                            _borrow_source(
                                service, project_id, draft_id, key, digest, size
                            )
                        ),
                    )
                )
    return tuple(sources)


@contextmanager
def _borrow_source(service, project_id, draft_id, key, digest, size):
    directory, _ = service._open(project_id, draft_id)
    # Pin the same inode outside the draft so cleanup cannot remove a reader's
    # source. Hold the file lock only for admission, never while rendering.
    with TemporaryDirectory(prefix=".pdf-reader-", dir=directory.parent) as scratch:
        pinned = Path(scratch) / "source.pdf"
        with service._files_lock(directory):
            header = service._header(project_id, directory)
            if header["state"] not in {"open", "committing", "committed"}:
                raise PdfImportDraftError(
                    409, "The imported PDF is no longer available"
                )
            with PdfDraftInventory(directory / "inventory.db") as inventory:
                item = service._staged(inventory, key)
            if item["sha256"] != digest or item["size"] != size:
                raise PdfImportDraftError(409, "The imported PDF changed after review")
            path = service._files_root(directory, header) / digest
            try:
                os.link(path, pinned)
            except FileNotFoundError as exc:
                raise PdfImportDraftError(
                    409, "The imported PDF is no longer available"
                ) from exc
        with FileLock(str(Path(scratch) / ".reader.lock")):
            yield pinned


def validate_split_starts(inventory, organization, starts):
    if organization != "split_documents":
        return {}
    normalized = {}
    if not isinstance(starts, dict):
        raise PdfImportDraftError(422, "Document starts must be grouped by file")
    for key, pages in starts.items():
        item = inventory.selected(key)
        if (
            item is None
            or item["media_kind"] != "pdf"
            or item["error"]
            or not item["page_count"]
        ):
            raise PdfImportDraftError(422, "Choose a readable PDF for document starts")
        if (
            not isinstance(pages, list)
            or not pages
            or pages[0] != 1
            or any(
                type(page) is not int or page < 1 or page > item["page_count"]
                for page in pages
            )
            or pages != sorted(set(pages))
        ):
            raise PdfImportDraftError(
                422, "Document starts must include page 1 and be in page order"
            )
        normalized[key] = pages
    return normalized


def organization_summary(directory, choices):
    organization = choices.get("organization", "keep_documents")
    starts = choices.get("split_starts", {})
    pdf_count = page_count = eligible_count = eligible_pages = output_rows = (
        new_rows
    ) = 0
    unknown = new_unknown = False
    category_rows = {}
    new_category_rows = {}
    with PdfDraftInventory(directory / "inventory.db") as inventory:
        cursor = inventory.db.execute(
            "SELECT occurrence_id,media_kind,page_count,category,error,matched,analysis_state "
            "FROM pdf_selected_state"
        )
        for item in cursor:
            category = item["category"]
            is_pdf = item["media_kind"] == "pdf"
            pdf_count += is_pdf
            eligible = is_pdf and item["page_count"] and not item["error"]
            page_count += int(item["page_count"] or 0) if is_pdf else 0
            if eligible and not item["matched"]:
                eligible_count += 1
                eligible_pages += int(item["page_count"])
            rows = 1
            if item["analysis_state"] == "pending":
                rows = None
            elif eligible and organization == "pages":
                rows = int(item["page_count"])
            elif eligible and organization == "split_documents":
                pages = starts.get(item["occurrence_id"])
                rows = len(pages) if pages is not None else None
            if rows is None:
                unknown = True
                new_unknown |= not item["matched"]
                category_rows[category] = None
                if not item["matched"]:
                    new_category_rows[category] = None
                continue
            output_rows += rows
            if not item["matched"]:
                new_rows += rows
                if new_category_rows.get(category, 0) is not None:
                    new_category_rows[category] = (
                        new_category_rows.get(category, 0) + rows
                    )
            if category_rows.get(category, 0) is not None:
                category_rows[category] = category_rows.get(category, 0) + rows
    maximum = MAX_INTERACTIVE_PDF_PAGES
    return {
        "pdf_count": pdf_count,
        "eligible_pdf_count": eligible_count,
        "page_count": page_count,
        "output_rows": None if unknown else output_rows,
        "new_output_rows": None if new_unknown else new_rows,
        "category_rows": category_rows,
        "new_category_rows": new_category_rows,
        "max_interactive_pages": maximum,
        "split_supported": eligible_count > 0 and eligible_pages <= maximum,
        "split_limit_reason": f"Split up to {maximum:,} pages at a time."
        if eligible_pages > maximum
        else None,
    }


def validate_complete_starts(inventory, starts):
    normalized = validate_split_starts(inventory, "split_documents", starts)
    after = 0
    while rows := inventory.staged_page(after=after, limit=256):
        for row in rows:
            after = int(row["ordinal"])
            if (
                row["media_kind"] == "pdf"
                and row["page_count"]
                and not row["error"]
                and not row["matched"]
                and row["occurrence_id"] not in normalized
            ):
                raise PdfImportDraftError(
                    409, "Review document starts for every PDF before importing"
                )
    return normalized

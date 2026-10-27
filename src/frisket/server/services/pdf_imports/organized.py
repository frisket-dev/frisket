"""Freeze reviewed upload facts for the shared durable PDF publisher."""

from __future__ import annotations

from contextlib import contextmanager

from frisket.contracts.http.pdf_packet_splits import PdfPacketDestination
from frisket.pdf_packets.source_map import SourceMap, SourceOccurrence
from frisket.server.services.pdf_packet_commit import (
    PdfPacketCommitter,
    PdfSplitPassthrough,
)
from frisket.server.services.pdf_packet_ocr import (
    PdfPacketOcrCoordinator,
    PdfPacketOcrQuote,
    packet_ocr_params,
)
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.services.pdf_split_preparation import (
    PdfSplitPageTarget,
    PdfSplitPreparationStore,
)

from .organization import _borrow_source
from .storage import PdfImportDraftError


def uses_packet_publication(header, inventory) -> bool:
    return (
        header["choices"].get("organization", "keep_documents") != "keep_documents"
        or inventory.db.execute(
            "SELECT 1 FROM pdf_source_acquisition LIMIT 1"
        ).fetchone()
        is not None
    )


@contextmanager
def _locked_source(path):
    # Used only while the caller owns the draft metadata lock. Discard must
    # acquire that same lock before removing these immutable admitted bytes.
    yield path


def draft_sources(service, project_id, directory, header, inventory):
    sources = []
    after = 0
    while rows := inventory.staged_page(after=after, limit=256):
        for row in rows:
            after = int(row["ordinal"])
            if (
                row["matched"]
                or row["error"]
                or not row["page_count"]
                or row["media_kind"] not in {"pdf", "image"}
            ):
                continue
            path = service._files_root(directory, header) / row["sha256"]
            sources.append(
                AdmittedPacketSource(
                    key=row["occurrence_id"],
                    sha256=row["sha256"],
                    filename=row["filename"],
                    size=row["size"],
                    page_count=row["page_count"],
                    page_start=1,
                    page_end=row["page_count"],
                    open_source=lambda path=path: _locked_source(path),
                    source_url=row.get("source_url"),
                    preview_inventory_path=directory / "inventory.db",
                    mime=row["mime"],
                    provider=row.get("provider"),
                    metadata=row.get("metadata") or {},
                )
            )
    return tuple(sources)


def cached_pages(directory, sources, selection, page_numbers=None):
    params = packet_ocr_params(selection)
    cached = {}
    with PdfSplitPreparationStore(directory / "inventory.db") as store:
        for source in sources:
            for page in (
                range(source.page_start, source.page_end + 1)
                if page_numbers is None
                else page_numbers.get(source.key, ())
            ):
                target = PdfSplitPageTarget(
                    source.key, None, source.sha256, None, page, params
                )
                value = store.cached_preview(target)
                if value is not None:
                    cached.setdefault(source.key, {})[page] = value
    return cached


def quote_pages(
    service, project_id, directory, header, inventory, *, request_context=None
):
    sources = draft_sources(service, project_id, directory, header, inventory)
    cached = cached_pages(directory, sources, header["choices"]["ocr_selection"])
    page_text = {
        (source.key, 1): "" for source in sources if source.mime.startswith("image/")
    }
    accepted = []
    for key, pages in cached.items():
        for page, payload in pages.items():
            page_text[(key, page)] = str(payload.get("text") or "")
            accepted.append((key, page))
    coordinator = PdfPacketOcrCoordinator(
        service._workspace,
        preview_runs=service._preview_runs,
        inventory_path=directory / "inventory.db",
    )
    holder = coordinator.quote(
        project_id=project_id,
        sources=sources,
        revision=inventory.selected_through(),
        ocr_policy=header["choices"]["ocr_policy"],
        ocr_selection=header["choices"]["ocr_selection"],
        page_text=page_text,
        accepted_ocr_pages=accepted,
        request_context=request_context,
    )
    return coordinator, holder, sources, cached


def estimate_pages(
    service, project_id, directory, header, inventory, *, request_context=None
):
    _coordinator, holder, _sources, _cached = quote_pages(
        service,
        project_id,
        directory,
        header,
        inventory,
        request_context=request_context,
    )
    header["page_ocr_quote"] = holder.snapshot(directory)
    estimate = dict(holder.public["estimate"])
    count = int(holder.public["pages"])
    authorization = holder.quoted.get("authorization") or {}
    source_count = len(
        {
            target["occurrence_key"]
            for target in authorization.get("scope", {}).get("targets", [])
        }
    )
    header["batch_ocr"] = {
        "state": "ready",
        "revision": header["revision"] + 1,
        "advisory": False,
        "purpose": "batch_pdf_ocr",
        "ocr_action_params": packet_ocr_params(header["choices"]["ocr_selection"]),
        "options": {"dpi": 200, "language": None, "keep_text_position": True},
        "ocr_policy": header["choices"]["ocr_policy"],
        "manifest_revision": header["revision"],
        "manifest_hash": holder.binding_hash,
        "work_scope_hash": holder.binding_hash,
        "selected_through": inventory.selected_through(),
        "selected_count": source_count,
        "known_pages": count,
        "estimate": estimate,
        "needs_confirmation": bool(estimate.get("requires_confirmation")),
    }
    service._save(directory, header)
    return dict(header["batch_ocr"])


def approve_pages(
    service,
    project_id,
    directory,
    header,
    inventory,
    *,
    confirmation,
    request_context=None,
):
    coordinator, current, sources, cached = quote_pages(
        service,
        project_id,
        directory,
        header,
        inventory,
        request_context=request_context,
    )
    saved = header.get("page_ocr_quote")
    holder = PdfPacketOcrQuote.restore(saved, directory) if saved is not None else None
    remote = not service._is_rapidocr(
        service._ocr_action_params(header["choices"]["ocr_selection"])
    )
    if holder is None:
        if remote and current.public["pages"]:
            raise PdfImportDraftError(
                409, "paid or remote OCR requires an exact source-page quote"
            )
        holder = current
    elif holder.binding_hash != current.binding_hash:
        raise PdfImportDraftError(
            409, "PDF source-page OCR quote changed; review the current price"
        )
    try:
        preparation = coordinator.approve(
            holder,
            project_id=project_id,
            sources=sources,
            revision=inventory.selected_through(),
            ocr_policy=header["choices"]["ocr_policy"],
            ocr_selection=header["choices"]["ocr_selection"],
            confirmation=confirmation,
            request_context=request_context,
            current=current,
        )
    except ValueError as exc:
        raise PdfImportDraftError(409, str(exc)) from exc
    header["page_ocr_quote"] = holder.snapshot(directory)
    native = {
        key: {
            page: path
            for page, path in pages.items()
            if page not in cached.get(key, {})
        }
        for key, pages in current.native_files.items()
    }
    return preparation, native, cached


def start_publication(workspace, intake, project_id, frozen, *, request_context=None):
    committer = PdfPacketCommitter(workspace)
    preparation = dict(frozen.pdf_preparation or {})
    if preparation or frozen.semantic_search:
        preparation.update(
            kind="pdf_split_source_pages",
            semantic_search=frozen.semantic_search,
            semantic_setup_confirmation=frozen.semantic_setup_confirmation,
        )
        preparation.setdefault("ocr_authorization", None)
    idempotency_key = f"pdf-import:{frozen.commit_id}"
    replay = committer.resume(
        project_id,
        idempotency_key=idempotency_key,
        pdf_preparation=preparation or None,
        request_context=request_context,
    )
    if replay is not None:
        if frozen.commit_result is None and frozen.directory.is_dir():
            _record_handoff(intake, project_id, frozen, replay)
        return replay
    if frozen.commit_result is not None:
        raise PdfImportDraftError(409, "The admitted PDF import is unavailable")
    sources, passthrough, order = [], [], []
    after = 0
    while occurrences := frozen.page_occurrences(after=after, limit=256):
        for occurrence in occurrences:
            order.append(occurrence.occurrence_id)
            opener = (
                None
                if occurrence.sha256 is None
                else lambda item=occurrence: _borrow_source(
                    intake,
                    project_id,
                    frozen.draft_id,
                    item.occurrence_id,
                    item.sha256,
                    item.size,
                )
            )
            if (
                occurrence.mime == "application/pdf"
                and occurrence.page_count
                and not occurrence.error
            ):
                sources.append(
                    AdmittedPacketSource(
                        key=occurrence.occurrence_id,
                        sha256=occurrence.sha256,
                        filename=occurrence.filename,
                        size=occurrence.size,
                        page_count=occurrence.page_count,
                        page_start=1,
                        page_end=occurrence.page_count,
                        open_source=opener,
                        source_url=occurrence.source_url,
                        mime=occurrence.mime,
                        provider=occurrence.provider,
                        metadata=occurrence.metadata or {},
                    )
                )
            else:
                passthrough.append(
                    PdfSplitPassthrough(
                        key=occurrence.occurrence_id,
                        sha256=occurrence.sha256,
                        filename=occurrence.filename,
                        size=occurrence.size,
                        mime=occurrence.mime,
                        open_source=opener,
                        source_url=occurrence.source_url,
                        error=occurrence.error,
                        provider=occurrence.provider,
                        metadata=occurrence.metadata or {},
                        page_count=1
                        if occurrence.mime.startswith("image/") and not occurrence.error
                        else None,
                    )
                )
        after = occurrences[-1].ordinal
    source_map = (
        SourceMap(
            [
                SourceOccurrence(
                    source.key, source.sha256, source.page_start, source.page_end
                )
                for source in sources
            ]
        )
        if sources
        else None
    )
    if source_map is None:
        ranges = ()
    elif frozen.organization == "pages":
        ranges = source_map.page_ranges()
    elif frozen.organization == "keep_documents":
        ranges = source_map.ranges(source_map.fixed_starts)
    else:
        starts = []
        for source, offset in zip(sources, source_map.fixed_starts, strict=True):
            local = frozen.split_starts.get(source.key)
            if not local:
                raise PdfImportDraftError(
                    409, "Review document starts for every PDF before importing"
                )
            starts.extend(offset + page - source.page_start for page in local)
        ranges = source_map.ranges(starts)
    destination = (
        PdfPacketDestination(
            kind="new_sheet",
            name=frozen.destination["sheet_name"],
        )
        if frozen.destination["kind"] == "new"
        else PdfPacketDestination(
            kind="append", sheet_id=frozen.destination["sheet_id"]
        )
    )
    child_names = {
        source.key: [source.filename]
        * sum(item.occurrence.key == source.key for item in ranges)
        for source in sources
    }
    result = committer.start(
        project_id,
        sources=sources,
        source_map=source_map,
        ranges=ranges,
        destination=destination,
        idempotency_key=idempotency_key,
        request_context=request_context,
        organization=frozen.organization,
        child_names=child_names,
        passthrough=passthrough,
        pdf_preparation=preparation or None,
        native_pages=frozen.native_pages,
        cached_pages=frozen.cached_pages,
        source_order=order,
        preserve_originals=frozen.organization == "keep_documents",
        skipped_duplicates=frozen.selected_total - frozen.committed_total,
    )
    _record_handoff(intake, project_id, frozen, result)
    return result


def _record_handoff(intake, project_id, frozen, result):
    intake.record_commit_handoff(
        project_id,
        frozen.draft_id,
        idempotency_key=frozen.idempotency_key,
        result={
            key: result[key]
            for key in ("import_ref", "receipt_id", "sheet_id", "job_id")
        },
    )

"""Apply frozen PDF source groups on the ordinary project job queue."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_jobs import ActionJobEnvelope, bind_typed_action_job
from frisket.engine.executor.ocr_read import ocr_token_granularity
from frisket.engine.pdf_text import (
    PdfTextCancelled,
    extract_pdf_text_batch,
    positioned_tokens_text,
)
from frisket.engine.store import Project
from frisket.engine.store.disk_capacity import require_disk_headroom
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.engine.store.import_blobs import (
    ImportBlob,
    ImportBlobCell,
    ImportBlobPlan,
    prepare_import_blobs,
)
from frisket.engine.store.import_intake import (
    import_intake_dir,
    import_worker_lock,
    read_import_header,
)
from frisket.engine.store.import_sessions import (
    ImportSessionConflict,
    ImportSessionStore,
)
from frisket.engine.store.pdf_split import PdfSplitWriter
from frisket.engine.store.pdf_split_intake import pdf_split_status, read_pdf_split_plan
from frisket.engine.store.prepared_content import PreparedPageDraft
from frisket.engine.store.value_codec import decode_stored_value
from frisket.pdf_packets.materialize import write_pdf_range
from frisket.redaction import redact_stored_error

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import PDF_SPLIT_APPLY_KIND, JobQueue, claimed_project_location
from .worker import HandlerRegistration, HandlerRegistry

LOG = logging.getLogger(__name__)


class _Superseded(RuntimeError):
    pass


def _snapshot_values(directory, source):
    values = {}
    for name, snapshot in source["context"].items():
        value = snapshot.get("value")
        if "file" in snapshot:
            path = directory / snapshot["file"]
            value = path.read_text()
            if hashlib.sha256(value.encode()).hexdigest() != snapshot["sha256"]:
                raise ValueError("carried snapshot changed")
        values[name] = decode_stored_value(snapshot["kind"], value)
    return values


def _page_cache(directory, ordinal, source, source_path, stopped):
    cache = {}
    for page, relative in source["cache"].items():
        cache[int(page)] = json.loads((directory / relative).read_text())
    if source["kind"] == "image":
        return cache
    retained = set(source.get("prepared_pages", ()))
    first, last = source["children"][0]["start"], source["children"][-1]["end"]
    missing = [
        page
        for page in range(first, last + 1)
        if page not in retained and page not in cache
    ]
    native_directory = directory / "native" / str(ordinal)
    native_directory.mkdir(parents=True, exist_ok=True)
    for page in tuple(missing):
        path = native_directory / f"{page}.json"
        if path.is_file():
            cache[page] = json.loads(path.read_text())
    missing = [page for page in missing if page not in cache]
    while missing:
        if stopped():
            raise InterruptedError("PDF splitting was stopped")
        first_missing = missing[0]
        count = 1
        while count < min(64, len(missing)) and missing[count] == first_missing + count:
            count += 1
        batch = asyncio.run(
            extract_pdf_text_batch(
                source_path,
                start_page=first_missing,
                max_pages=count,
                should_cancel=stopped,
            )
        )
        for page in batch.pages:
            payload = {
                "text": positioned_tokens_text(page.tokens),
                "engine": "native",
                "positions": {
                    "engine": "native",
                    "width": page.width,
                    "height": page.height,
                    "blocks": [
                        {
                            "text": token.text,
                            "bbox": {
                                "space": "page_normalized",
                                **token.box.model_dump(),
                            },
                        }
                        for token in page.tokens
                    ],
                },
            }
            cache[page.page] = payload
            (native_directory / f"{page.page}.json").write_text(json.dumps(payload))
        missing = missing[count:]
    return cache


def _source_plan(
    project, project_id, directory, ordinal, source, roles, stopped, progress
):
    if source["kind"] == "error":
        record = {
            roles["filename"]: source["filename"],
            roles["source"]: source.get("source_url") or "",
            roles["text"]: "",
            roles["error"]: source["error"],
        }
        yield [record], prepare_import_blobs(project, ImportBlobPlan())
        return
    staged = directory / "staged" / source["sha256"]
    if staged.is_file():
        project.blob_store.put_path(staged, expected_digest=source["sha256"])
        staged.unlink()
    with (
        project.blob_store.materialize(source["sha256"]) as original,
        tempfile.TemporaryDirectory(
            prefix="pdf-split-source-", dir=directory
        ) as temporary,
    ):
        scratch = Path(temporary)
        require_disk_headroom(scratch, source["size"])
        context = _snapshot_values(directory, source)
        if source["kind"] not in ("pdf", "image"):
            blob = ImportBlob(
                0,
                None,
                source["sha256"],
                source["size"],
                source["filename"],
                source["mime"],
                source.get("metadata", {}),
                "attachment",
                owner=project.blob_store,
                source_url=source["source_url"],
                provider=source.get("provider"),
                occurrence_ref={
                    "kind": "import_file",
                    "source_key": source["key"],
                    "error": source.get("error"),
                },
            )
            record = {
                roles["filename"]: source["filename"],
                roles["media"]: {
                    "blob": blob.digest,
                    "filename": blob.filename,
                    "mime": blob.mime,
                },
                roles["source"]: source.get("source_url") or "",
                roles["text"]: "",
                **context,
            }
            if source.get("error") and "error" in roles:
                record[roles["error"]] = source["error"]
            yield (
                [record],
                prepare_import_blobs(
                    project,
                    ImportBlobPlan((blob,), (ImportBlobCell(0, 0, roles["media"]),)),
                ),
            )
            return
        reader = PdfReader(original, strict=False) if source["kind"] == "pdf" else None
        if reader is not None and len(reader.pages) != source["page_count"]:
            raise ValueError("admitted PDF page count changed")
        cache = _page_cache(directory, ordinal, source, Path(original), stopped)
        document = ImportBlob(
            0,
            None,
            source["sha256"],
            source["size"],
            source["filename"],
            source["mime"],
            source.get("metadata", {}),
            "document",
            owner=project.blob_store,
            source_url=source["source_url"],
            provider=source.get("provider"),
            page_count=source["page_count"],
            occurrence_ref={
                "kind": "pdf_packet_split_source"
                if source["kind"] == "pdf"
                else "pdf_split_source",
                "source_key": source["key"],
                "sibling_count": len(source["children"]),
                "original_source_cell": None
                if source["source_row_id"] is None
                else {
                    "sheet_id": source["source_sheet_id"],
                    "row_id": source["source_row_id"],
                    "column_id": source["source_column_id"],
                },
            },
            original_source=True,
            source_sheet_id=source["source_sheet_id"],
            source_row_id=source["source_row_id"],
            source_column_id=source["source_column_id"],
        )
        blobs = [document]
        cells = []
        records = []
        for index, child in enumerate(source["children"], 1):
            if stopped():
                raise InterruptedError("PDF splitting was stopped")
            start, end = child["start"], child["end"]
            require_disk_headroom(
                scratch,
                max(
                    1024 * 1024,
                    source["size"] * (end - start + 1) // source["page_count"],
                ),
            )
            if child.get("keep_original"):
                path, digest, size = None, source["sha256"], source["size"]
            else:
                path = scratch / f"{index}.pdf"
                write_pdf_range(reader, path, start, end, should_cancel=stopped)
                with path.open("rb") as child_source:
                    digest = hashlib.file_digest(child_source, "sha256").hexdigest()
                size = path.stat().st_size
            page_drafts = []
            engines = set()
            granularities = set()
            if source.get("prepared_engine"):
                engines.add(str(source["prepared_engine"]))
            if source.get("prepared_token_granularity") in {"word", "line", "block"}:
                granularities.add(source["prepared_token_granularity"])
            for page in range(start, end + 1):
                if page not in cache:
                    continue
                payload = cache[page]
                positions = payload.get("positions")
                if positions is None and "blocks" in payload:
                    positions = {
                        key: payload[key]
                        for key in ("blocks", "width", "height", "engine")
                        if key in payload
                    }
                page_drafts.append(
                    PreparedPageDraft(
                        page - start + 1, str(payload.get("text") or ""), positions
                    )
                )
                engines.add(str(payload.get("engine") or "native"))
                granularity = payload.get("token_granularity") or payload.get(
                    "ocr_token_granularity"
                )
                if granularity in {"word", "line", "block"}:
                    granularities.add(granularity)
            engine = next(iter(engines)) if len(engines) == 1 else None
            granularity = (
                next(iter(granularities))
                if len(granularities) == 1
                else (
                    ocr_token_granularity(engine)
                    if engine not in (None, "native", "pdf_native")
                    else None
                )
            )
            blob = ImportBlob(
                index,
                path,
                digest,
                size,
                child["filename"],
                source["mime"],
                source.get("metadata", {}) if child.get("keep_original") else {},
                "attachment",
                source_url=source["source_url"],
                provider=source.get("provider"),
                owner=project.blob_store if child.get("keep_original") else None,
                preserve_original=bool(child.get("keep_original")),
                packet_source_id=0,
                packet_page_start=start,
                packet_page_end=end,
                packet_sibling_count=len(source["children"]),
                prepared_column_name=roles["text"],
                prepared_pages=tuple(page_drafts),
                prepared_source_ref_id=source.get("prepared_ref_id"),
                prepared_engine=engine,
                prepared_token_granularity=granularity,
                source_link_column_name=roles["source"],
                source_link_project_id=project_id,
            )
            blobs.append(blob)
            cells.append(ImportBlobCell(index, index - 1, roles["media"]))
            page_role = "page" if "page" in roles else "pages"
            record = {
                **context,
                roles["filename"]: child["filename"],
                roles["media"]: {
                    "blob": digest,
                    "filename": child["filename"],
                    "mime": source["mime"],
                },
                roles[page_role]: start
                if page_role == "page"
                else str(start)
                if start == end
                else f"{start}\u2013{end}",
                roles["source"]: "",
                roles["text"]: "",
            }
            records.append(record)
            if progress is not None:
                progress(index, len(source["children"]))
        prepared = prepare_import_blobs(
            project, ImportBlobPlan(tuple(blobs), tuple(cells))
        )
        yield records, prepared


def register_pdf_split_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
    on_terminal_import: Callable[[str, str, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, context: JobHandlerContext) -> dict:
        if (
            context.job_id is None
            or context.job_attempt is None
            or not context.handler_authority_id
        ):
            raise ValueError("PDF splitting requires a row-backed queue claim")
        project_id, _, path = claimed_project_location(
            payload,
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            require_storage_identity=require_storage_identity
            or project_opener is not None,
        )
        project = (
            Project(path)
            if project_opener is None
            else open_claimed_project(payload, path, project_opener)
        )
        ref = payload["import_ref"]
        directory = import_intake_dir(project.path, ref)

        def stopped():
            current = queue.get(context.job_id)
            if (
                read_import_header(directory).cancel_requested
                or (cancelled is not None and cancelled())
                or (current is not None and current.status == "cancelled")
            ):
                return True
            if (
                current is None
                or current.status != "running"
                or current.attempts != context.job_attempt
            ):
                raise _Superseded("split queue claim changed")
            return False

        try:
            with import_worker_lock(directory, timeout=5):
                header = read_import_header(directory)
                if (
                    header.project_id != project_id
                    or header.storage_identity != project.storage_identity
                ):
                    raise ValueError("split intake belongs to another project")
                plan = read_pdf_split_plan(
                    directory, expected_hash=payload["plan_hash"]
                )
                envelope = ActionJobEnvelope.from_json(header.envelope)
                bound = bind_typed_action_job(envelope, project=project)
                if (
                    isinstance(bound, ActionResult)
                    or bound.params.source_ref != ref
                    or bound.params.plan_hash != payload["plan_hash"]
                ):
                    raise ValueError("split action differs from its admitted plan")
                session = ImportSessionStore(project).get(ref)
                if session is None:
                    raise ValueError("split publication session is missing")
                if session.state in ("kept", "completed", "removed"):
                    return pdf_split_status(project, ref)
                writer = PdfSplitWriter.resume_session(
                    project,
                    session_id=ref,
                    writer_authority=context.handler_authority_id,
                    expected_cursor=session.cursor,
                )
                columns = {
                    column["name"]: int(column["id"])
                    for column in project.columns(writer.sheet_id, include_hidden=True)
                }
                ensure_document_text_role(
                    project,
                    source_column_id=columns[plan["roles"]["media"]],
                    text_column_id=columns[plan["roles"]["text"]],
                    commit=True,
                )
                cursor = session.cursor
                stopped_remaining = False
                for ordinal, source in enumerate(plan["sources"]):
                    if ordinal < cursor:
                        continue
                    if stopped():
                        stopped_remaining = True
                        break
                    try:
                        staged = _source_plan(
                            project,
                            project_id,
                            directory,
                            ordinal,
                            source,
                            plan["roles"],
                            stopped,
                            progress,
                        )
                        try:
                            records, blobs = next(staged)
                            if stopped():
                                stopped_remaining = True
                                break
                            writer.publish_source(
                                records,
                                expected_cursor=cursor,
                                committed_bytes=source["size"],
                                blob_plan=blobs,
                                source_error=source.get("error"),
                            )
                        finally:
                            staged.close()
                    except (InterruptedError, PdfTextCancelled):
                        stopped_remaining = True
                        break
                    except _Superseded:
                        raise
                    except Exception as exc:
                        message = (
                            "The source changed before it could be split."
                            if isinstance(exc, ImportSessionConflict)
                            else redact_stored_error(
                                exc, fallback_code="pdf_split_source_failed"
                            )
                            or "The PDF could not be split."
                        )
                        writer.fail_source(expected_cursor=cursor, error=message)
                        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                            cursor += 1
                            stopped_remaining = True
                            break
                    cursor += 1
                publication = writer.finalize(
                    expected_cursor=cursor, stopped=stopped_remaining
                )
                if on_terminal_import is not None and publication.row_count:
                    try:
                        on_terminal_import(
                            project_id, publication.receipt_id, publication.sheet_id
                        )
                    except Exception:
                        LOG.warning("split_preparation_handoff_pending", exc_info=True)
                return pdf_split_status(project, ref)
        except _Superseded:
            return {"status": "superseded"}
        finally:
            project.close()

    return registry.add(
        PDF_SPLIT_APPLY_KIND, handle, origin="frisket.production.pdf_split"
    )


__all__ = ["register_pdf_split_handler"]

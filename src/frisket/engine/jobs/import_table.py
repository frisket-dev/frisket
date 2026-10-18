"""Stream one reviewed table source on the ordinary project job queue."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_jobs import ActionJobEnvelope, bind_typed_action_job
from frisket.engine.store import Project
from frisket.engine.store.import_intake import (
    import_admit_lock,
    import_intake_dir,
    import_worker_lock,
    read_import_header,
    set_import_resolution,
)
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.engine.store.table_import_intake import (
    FrozenTableImport,
    TableDedupSnapshot,
    TableImportIssue,
    advance_table_progress,
    compact_table_intake,
    finish_table_intake,
    frozen_table_import,
    record_table_issues,
    table_dedup_key,
    table_import_status,
)
from frisket.project_identity import ProjectStorageKey
from frisket.table_import import RawCell, TableRow, convert_cell, open_table

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import (
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    IMPORT_TABLE_STREAM_KIND,
    JobQueue,
    claimed_project_location,
)
from .worker import HandlerRegistration, HandlerRegistry


LOG = logging.getLogger(__name__)
_PAGE_ROWS = 256
_PAGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class _PreparedRow:
    source_cursor: int
    values: dict[str, Any] | None
    issues: tuple[TableImportIssue, ...]
    source_bytes: int


def table_import_dedupe_key(project_id: str, import_ref: str, plan_hash: str) -> str:
    return f"import-table:{project_id}:{import_ref}:{plan_hash}"


def _issue(
    row: TableRow,
    *,
    source_cursor: int,
    column_name: str,
    code: str,
    message: str,
    original: str,
) -> TableImportIssue:
    return TableImportIssue(
        source_ordinal=source_cursor,
        line_start=row.source.physical_line_start,
        line_end=row.source.physical_line_end,
        column_name=column_name,
        code=code,
        message=message,
        original=original,
    )


def _prepared_rows(
    frozen: FrozenTableImport, *, start_cursor: int
) -> Iterator[_PreparedRow]:
    plan = frozen.plan
    source_path = frozen.intake_dir / "source"
    dedup = TableDedupSnapshot(frozen.intake_dir) if plan.dedup is not None else None
    try:
        reader_context = open_table(
            source_path,
            plan.settings,
            total_record_count=(
                frozen.source_record_count if plan.settings.skip_rows_end else None
            ),
        )
        with reader_context as reader:
            available = {column.index: column.name for column in reader.columns}
            for column in plan.columns:
                if available.get(column.source_index) != column.source_name:
                    raise ValueError("frozen table source columns changed")

            data_cursor = 0
            for row in reader:
                if row.role != "data":
                    continue
                data_cursor += 1
                if data_cursor <= start_cursor:
                    continue
                row_issues = [
                    _issue(
                        row,
                        source_cursor=data_cursor,
                        column_name="",
                        code=item.code,
                        message=item.message,
                        original=" | ".join(item.original_values),
                    )
                    for item in (*row.source.issues, *row.issues)
                ]
                values: dict[str, Any] = {}
                source_bytes = 0
                for column in plan.columns:
                    cell = (
                        row.cells[column.source_index]
                        if column.source_index < len(row.cells)
                        else RawCell("", "blank")
                    )
                    source_bytes += len(cell.text.encode("utf-8"))
                    conversion = convert_cell(
                        cell,
                        column.target_type,
                        decimal_separator=plan.settings.decimal_separator,
                        thousands_separator=plan.settings.thousands_separator,
                        date_order=column.date_order or "month_first",
                    )
                    values[column.target_name] = conversion.value
                    if conversion.issue is not None:
                        row_issues.append(
                            _issue(
                                row,
                                source_cursor=data_cursor,
                                column_name=column.target_name,
                                code=conversion.issue.code,
                                message=conversion.issue.message,
                                original=conversion.issue.original,
                            )
                        )
                if plan.extra_values_target_name is not None:
                    extra = [cell.text for cell in row.extra_values]
                    values[plan.extra_values_target_name] = (
                        None if not extra else " | ".join(extra)
                    )
                    source_bytes += sum(len(value.encode("utf-8")) for value in extra)
                if plan.dedup is not None:
                    key_column = next(
                        column
                        for column in plan.columns
                        if column.source_index == plan.dedup.source_index
                    )
                    key = table_dedup_key(values[key_column.target_name])
                    # The immutable destination snapshot is never amended by this
                    # reader, so duplicates within the incoming source remain.
                    if key is not None and dedup is not None and dedup.contains(key):
                        values_or_none = None
                    else:
                        values_or_none = values
                else:
                    values_or_none = values
                yield _PreparedRow(
                    source_cursor=data_cursor,
                    values=values_or_none,
                    issues=tuple(row_issues),
                    source_bytes=source_bytes,
                )
    finally:
        if dedup is not None:
            dedup.close()


def _validate_reserved_action(bound: Any, frozen: FrozenTableImport) -> None:
    expected_action = (
        "import.rows"
        if frozen.plan.destination_sheet_id is None
        else "import.append_rows"
    )
    if bound.action.action_id != expected_action:
        raise ValueError("table intake action does not match its destination")
    if getattr(bound.params, "rows", None) != []:
        raise ValueError("streaming table action must not carry inline rows")
    actual_columns = [
        (
            column.name,
            column.source_name,
            column.type,
            column.format,
        )
        for column in getattr(bound.params, "columns", ())
    ]
    expected_columns = [
        (
            column.target_name,
            column.source_name,
            column.output_type,
            column.format,
        )
        for column in frozen.plan.columns
    ]
    if frozen.plan.extra_values_target_name is not None:
        expected_columns.append(
            (frozen.plan.extra_values_target_name, None, "text", None)
        )
    if actual_columns != expected_columns:
        raise ValueError("table intake columns changed after review")


def _project_payload(
    payload: dict[str, Any],
    context: JobHandlerContext,
    project_id: str,
    project_root: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "project_id": project_id,
        "workspace_root": str(project_root),
    }
    claimed = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
    if isinstance(claimed, ProjectStorageKey):
        result["storage_org_id"] = claimed.storage_org_id
    if isinstance(context.trusted_job_org_id, int):
        result["org_id"] = context.trusted_job_org_id
    return result


def register_import_table_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, context: JobHandlerContext) -> dict:
        if (
            context.job_id is None
            or context.job_attempt is None
            or not context.handler_authority_id
        ):
            raise ValueError("table import requires a row-backed queue claim")

        def live() -> bool:
            current = queue.get(context.job_id)
            return (
                current is not None
                and current.status == "running"
                and current.attempts == context.job_attempt
            )

        project_id, project_root, path = claimed_project_location(
            payload,
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            require_storage_identity=require_storage_identity
            or project_opener is not None,
        )
        import_ref = str(payload["import_ref"])
        project = (
            Project(path)
            if project_opener is None
            else open_claimed_project(payload, path, project_opener)
        )
        directory = import_intake_dir(project.path, import_ref)
        sessions = ImportSessionStore(project)
        try:
            with import_worker_lock(directory, timeout=5):
                header = read_import_header(directory)
                if (
                    header.intake_kind != "table"
                    or header.project_id != project_id
                    or header.storage_identity != project.storage_identity
                ):
                    raise ValueError("table intake belongs to another project")
                frozen = frozen_table_import(directory)
                requested_hash = payload.get("plan_hash")
                if requested_hash != frozen.plan_hash:
                    raise ValueError("queued table plan identity changed")
                envelope = ActionJobEnvelope.from_json(header.envelope)
                bound = bind_typed_action_job(envelope, project=project)
                if isinstance(bound, ActionResult):
                    raise ValueError("table intake request no longer validates")
                _validate_reserved_action(bound, frozen)
                session = sessions.get(import_ref)
                if session is None:
                    raise ValueError("table import session has not been started")
                state = table_import_status(directory)
                if state.phase == "terminal" or session.state in {
                    "completed",
                    "kept",
                    "removed",
                }:
                    return {
                        "status": session.state,
                        "completed_rows": session.committed_rows,
                        "sheet_id": session.sheet_id,
                        "problems": state.problem_count,
                    }
                stored = ReceiptStore(project).find_by_id(envelope.receipt_id)
                if stored is None or stored.status not in {"queued", "running"}:
                    raise ValueError("table import reservation is unavailable")
                if stored.status == "queued":
                    ReceiptStore(project).update_body_status(
                        stored.parsed().model_copy(update={"status": "running"}),
                        require_status="queued",
                    )
                if session.state == "paused":
                    StreamingSheetWriter.resume_session(
                        project,
                        session_id=import_ref,
                        writer_authority=context.handler_authority_id,
                        expected_cursor=session.cursor,
                    )
                    session = sessions.get(import_ref)
                    assert session is not None
                elif session.state != "active":
                    raise ValueError("table import session cannot execute")
                elif session.writer_authority != context.handler_authority_id:
                    StreamingSheetWriter.resume_session(
                        project,
                        session_id=import_ref,
                        writer_authority=context.handler_authority_id,
                        expected_cursor=session.cursor,
                    )
                    session = sessions.get(import_ref)
                    assert session is not None

                if state.source_cursor < session.cursor:
                    advance_table_progress(
                        directory,
                        expected_cursor=state.source_cursor,
                        source_cursor=session.cursor,
                    )
                iterator = _prepared_rows(frozen, start_cursor=session.cursor)
                exact_rows = session.cursor
                while True:
                    header = read_import_header(directory)
                    session = sessions.get(import_ref)
                    assert session is not None
                    if header.cancel_requested or not live():
                        writer = StreamingSheetWriter._from_session(project, session)
                        if header.cancel_requested:
                            cancelled = writer.cancel(expected_cursor=session.cursor)
                            writer = StreamingSheetWriter._from_session(
                                project, cancelled
                            )
                            publication = writer.finalize_session(
                                expected_cursor=cancelled.cursor,
                                keep_cancelled=True,
                            )
                            with import_admit_lock(directory):
                                set_import_resolution(directory, "kept")
                            finish_table_intake(directory, exact_rows=None)
                            compact_table_intake(directory)
                            return {
                                "status": "kept",
                                "completed_rows": publication.row_count,
                                "sheet_id": publication.sheet_id,
                                "problems": table_import_status(
                                    directory
                                ).problem_count,
                            }
                        writer.pause(expected_cursor=session.cursor)
                        return {
                            "status": "paused",
                            "completed_rows": session.committed_rows,
                        }

                    page: list[_PreparedRow] = []
                    page_bytes = 0
                    exhausted = False
                    while len(page) < _PAGE_ROWS and page_bytes < _PAGE_BYTES:
                        try:
                            item = next(iterator)
                        except StopIteration:
                            exhausted = True
                            break
                        page.append(item)
                        page_bytes += item.source_bytes
                        if item.source_bytes > _PAGE_BYTES:
                            raise ValueError(
                                "one table source row exceeds the import page byte limit"
                            )
                    if not page and exhausted:
                        writer = StreamingSheetWriter._from_session(project, session)
                        publication = writer.finalize_session(
                            expected_cursor=session.cursor
                        )
                        exact_rows = max(
                            exact_rows,
                            table_import_status(directory).source_cursor,
                            session.cursor,
                        )
                        finish_table_intake(directory, exact_rows=exact_rows)
                        compact_table_intake(directory)
                        from .search_index import enqueue_search_index

                        enqueue_search_index(
                            project,
                            queue,
                            _project_payload(
                                payload, context, project_id, project_root
                            ),
                        )
                        return {
                            "status": "completed",
                            "completed_rows": publication.row_count,
                            "sheet_id": publication.sheet_id,
                            "problems": table_import_status(directory).problem_count,
                        }
                    exact_rows = page[-1].source_cursor
                    issues = [issue for item in page for issue in item.issues]
                    record_table_issues(directory, issues)
                    records = [item.values for item in page if item.values is not None]
                    next_cursor = page[-1].source_cursor
                    if records:
                        writer = StreamingSheetWriter._from_session(project, session)
                        writer.append_page(
                            records,
                            expected_cursor=session.cursor,
                            next_cursor=next_cursor,
                            committed_bytes=page_bytes,
                        )
                        advance_table_progress(
                            directory,
                            expected_cursor=table_import_status(
                                directory
                            ).source_cursor,
                            source_cursor=next_cursor,
                        )
                    elif exhausted:
                        # A duplicate-only tail owns no project rows. Its exact
                        # source count is still terminal status, while the writer
                        # cursor remains the last atomic row publication.
                        advance_table_progress(
                            directory,
                            expected_cursor=table_import_status(
                                directory
                            ).source_cursor,
                            source_cursor=next_cursor,
                        )
                    from .search_index import enqueue_search_index

                    enqueue_search_index(
                        project,
                        queue,
                        _project_payload(payload, context, project_id, project_root),
                    )
                    if exhausted:
                        # The next iteration finalizes through the ordinary empty
                        # EOF path without reopening the source.
                        continue
        except Exception:
            session = sessions.get(import_ref)
            if (
                session is not None
                and session.state == "active"
                and session.writer_authority == context.handler_authority_id
            ):
                StreamingSheetWriter._from_session(project, session).pause(
                    expected_cursor=session.cursor
                )
            raise
        finally:
            project.close()

    return registry.add(
        IMPORT_TABLE_STREAM_KIND,
        handle,
        origin="frisket.production.import_table",
    )


__all__ = ["register_import_table_handler", "table_import_dedupe_key"]

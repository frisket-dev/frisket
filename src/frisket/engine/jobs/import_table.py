"""Stream one reviewed table source on the ordinary project job queue."""

from __future__ import annotations

import logging
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from contextlib import ExitStack
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
from frisket.engine.store.keyed_import import (
    index_targets,
    publish_keyed_page,
    record_key,
)
from frisket.engine.store.keyed_updates import ValidatedKeyedInput
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
    record_table_execution_error,
    record_table_issues,
    table_dedup_key,
    table_import_status,
)
from frisket.project_identity import ProjectStorageKey
from frisket.redaction import redact_stored_error
from frisket.table_import import RawCell, TableRow, convert_cell, open_table

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import (
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    IMPORT_TABLE_ANALYSIS_KIND,
    IMPORT_TABLE_STREAM_KIND,
    JobQueue,
    claimed_project_location,
)
from .worker import HandlerRegistration, HandlerRegistry


LOG = logging.getLogger(__name__)
_PAGE_ROWS = 256
_PAGE_BYTES = 4 * 1024 * 1024

TableAnalysisRunner = Callable[[Any, str, str, int], object]


@dataclass(frozen=True)
class _PreparedRow:
    source_cursor: int
    values: dict[str, Any] | None
    issues: tuple[TableImportIssue, ...]
    source_bytes: int


def table_import_dedupe_key(project_id: str, import_ref: str, plan_hash: str) -> str:
    return f"import-table:{project_id}:{import_ref}:{plan_hash}"


def table_analysis_dedupe_key(
    project_id: str, draft_id: str, settings_revision: int
) -> str:
    return f"table-analysis:{project_id}:{draft_id}:{settings_revision}"


def _issue(
    row: TableRow,
    *,
    source_cursor: int,
    column_name: str,
    code: str,
    message: str,
    original: str,
    count: int = 1,
) -> TableImportIssue:
    return TableImportIssue(
        source_ordinal=source_cursor,
        line_start=row.source.physical_line_start,
        line_end=row.source.physical_line_end,
        column_name=column_name,
        code=code,
        message=message,
        original=original,
        count=count,
    )


def _prepared_rows(
    frozen: FrozenTableImport, *, start_cursor: int
) -> Iterator[_PreparedRow]:
    plan = frozen.plan
    source_path = frozen.intake_dir / "source"
    if plan.record_encoding == "stored_values":
        from frisket.engine.store.value_codec import decode_stored_value

        with source_path.open("r", encoding="utf-8") as source:
            for ordinal, line in enumerate(source, 1):
                if ordinal <= start_cursor:
                    continue
                fields = json.loads(line)
                values = {
                    name: decode_stored_value(kind, value)
                    for name, (kind, value) in fields.items()
                }
                yield _PreparedRow(ordinal, values, (), len(line.encode("utf-8")))
        return
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
                        count=int(getattr(item, "count", 1)),
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
                    values[plan.extra_values_target_name] = None if not extra else extra
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
    if frozen.plan.update is not None:
        expected_action = frozen.plan.action_kind or "import.update_rows"
    if bound.action.action_id != expected_action:
        raise ValueError("table intake action does not match its destination")
    if frozen.plan.action_kind not in {
        None,
        "import.update_rows",
        "import.update_csv",
        "import.update_xlsx",
    }:
        from frisket.actions.types import RowsUpdater
        from frisket.engine.executor.map_rows_action import typed_request_hash

        if (
            getattr(bound.action.definition.run, "capabilities", ()) != (RowsUpdater,)
            or frozen.plan.record_encoding != "stored_values"
            or frozen.plan.request_hash != typed_request_hash(bound)
            or bound.request.scope.sheet_id != frozen.plan.destination_sheet_id
        ):
            raise ValueError(
                "staged capability update differs from its admitted request"
            )
        return
    if getattr(bound.params, "rows", None) not in (None, []):
        raise ValueError("streaming table action must not carry inline rows")
    actual_columns = [
        (
            column.name,
            column.source_name or column.name,
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
            (
                frozen.plan.extra_values_target_name,
                frozen.plan.extra_values_target_name,
                "json",
                None,
            )
        )
    if frozen.plan.update is not None:
        staged = getattr(bound.params, "staged_input", None)
        if (
            staged is None
            or staged.import_ref != frozen.import_ref
            or staged.plan_hash != frozen.plan_hash
            or staged.source_hash.removeprefix("sha256:")
            != frozen.source.sha256.removeprefix("sha256:")
        ):
            raise ValueError("typed update must identify its admitted staged input")
        expected_keys = [
            column.target_name
            for column in frozen.plan.columns
            if column.source_index in frozen.plan.update.key_source_indices
        ]
        if (
            bound.request.scope.sheet_id != frozen.plan.destination_sheet_id
            or list(bound.params.key_columns) != expected_keys
            or bound.params.update_policy != frozen.plan.update.policy
            or bound.params.relationship_id != frozen.plan.update.relationship_id
            or bound.params.keep_existing_on_blank
            != frozen.plan.update.keep_existing_on_blank
        ):
            raise ValueError(
                "typed update destination or policy changed after admission"
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


def run_table_import(
    project: Any,
    *,
    project_id: str,
    import_ref: str,
    plan_hash: str,
    writer_authority: str,
    live: Callable[[], bool] = lambda: True,
    on_commit: Callable[[], None] = lambda: None,
    publication_owner: ExitStack | None = None,
) -> dict[str, Any]:
    """Drain the same bounded publisher for local calls and queued workers."""
    directory = import_intake_dir(project.path, import_ref)
    sessions = ImportSessionStore(project)
    try:
        with ExitStack() as resources:
            (publication_owner or resources).enter_context(
                import_worker_lock(directory, timeout=5)
            )
            header = read_import_header(directory)
            if (
                header.intake_kind != "table"
                or header.project_id != project_id
                or header.storage_identity != project.storage_identity
            ):
                raise ValueError("table intake belongs to another project")
            frozen = frozen_table_import(directory)
            requested_hash = plan_hash
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
            if session.state in {"completed", "kept"}:
                if state.phase != "terminal":
                    finish_table_intake(
                        directory,
                        exact_rows=max(state.source_cursor, session.cursor),
                    )
                if session.state == "kept" and header.resolution is None:
                    with import_admit_lock(directory):
                        set_import_resolution(directory, "kept")
                compact_table_intake(directory)
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
                    writer_authority=writer_authority,
                    expected_cursor=session.cursor,
                )
                session = sessions.get(import_ref)
                assert session is not None
            elif session.state != "active":
                raise ValueError("table import session cannot execute")
            elif session.writer_authority != writer_authority:
                StreamingSheetWriter.resume_session(
                    project,
                    session_id=import_ref,
                    writer_authority=writer_authority,
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
            keyed = None
            if frozen.plan.update is not None:
                keyed = resources.enter_context(
                    ValidatedKeyedInput(retain_records=False)
                )
                # A later conflicting duplicate must invalidate its first
                # occurrence before any visible field can be published.
                keyed.stage(
                    (record_key(frozen.plan, item.values), item.values)
                    for item in _prepared_rows(frozen, start_cursor=0)
                    if item.values is not None
                )
                keyed.seal()
                index_targets(project, frozen.plan, keyed)
            iterator = _prepared_rows(
                frozen, start_cursor=max(session.cursor, state.source_cursor)
            )
            exact_rows = max(session.cursor, state.source_cursor)
            while True:
                header = read_import_header(directory)
                session = sessions.get(import_ref)
                assert session is not None
                if header.cancel_requested or not live():
                    writer = StreamingSheetWriter._from_session(project, session)
                    if header.cancel_requested:
                        cancelled = writer.cancel(expected_cursor=session.cursor)
                        writer = StreamingSheetWriter._from_session(project, cancelled)
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
                            "problems": table_import_status(directory).problem_count,
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
                    # A page budget is not a cell-size restriction: one large
                    # indivisible record occupies its own page.
                if not page and exhausted:
                    writer = StreamingSheetWriter._from_session(project, session)
                    if publication_owner is not None:
                        writer.pause(expected_cursor=session.cursor)
                        return {
                            "status": "published",
                            "completed_rows": session.committed_rows,
                            "sheet_id": session.sheet_id,
                        }
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
                    on_commit()
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
                if keyed is not None:
                    writer = StreamingSheetWriter._from_session(project, session)
                    publish_keyed_page(
                        writer,
                        frozen.plan,
                        keyed,
                        [
                            (item.source_cursor, item.values)
                            for item in page
                            if item.values is not None
                        ],
                        expected_cursor=session.cursor,
                        next_cursor=next_cursor,
                        committed_bytes=page_bytes,
                    )
                    advance_table_progress(
                        directory,
                        expected_cursor=table_import_status(directory).source_cursor,
                        source_cursor=next_cursor,
                    )
                elif records:
                    if (
                        header.max_rows is not None
                        and session.committed_rows + len(records) > header.max_rows
                    ):
                        raise ValueError(
                            "table import exceeds the configured row limit"
                        )
                    writer = StreamingSheetWriter._from_session(project, session)
                    writer.append_page(
                        records,
                        expected_cursor=session.cursor,
                        next_cursor=next_cursor,
                        committed_bytes=page_bytes,
                    )
                    advance_table_progress(
                        directory,
                        expected_cursor=table_import_status(directory).source_cursor,
                        source_cursor=next_cursor,
                    )
                else:
                    # Duplicate-only pages own no project rows. Their source
                    # cursor remains durable progress even when the full page
                    # has not observed EOF yet.
                    advance_table_progress(
                        directory,
                        expected_cursor=table_import_status(directory).source_cursor,
                        source_cursor=next_cursor,
                    )
                on_commit()
                if exhausted:
                    # The next iteration finalizes through the ordinary empty
                    # EOF path without reopening the source.
                    continue
    except Exception as exc:
        session = sessions.get(import_ref)
        if (
            session is not None
            and session.state == "active"
            and session.writer_authority == writer_authority
        ):
            StreamingSheetWriter._from_session(project, session).pause(
                expected_cursor=session.cursor
            )
        safe_error = redact_stored_error(
            exc,
            fallback_code="table_import_failed",
        )
        if safe_error is not None:
            try:
                record_table_execution_error(directory, safe_error)
            except (OSError, ValueError):
                LOG.warning(
                    "table import error state could not be published",
                    exc_info=True,
                )
        raise


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

        def on_commit() -> None:
            from .search_index import enqueue_search_index

            enqueue_search_index(
                project,
                queue,
                _project_payload(payload, context, project_id, project_root),
            )

        try:
            return run_table_import(
                project,
                project_id=project_id,
                import_ref=import_ref,
                plan_hash=payload["plan_hash"],
                writer_authority=context.handler_authority_id,
                live=live,
                on_commit=on_commit,
            )
        finally:
            project.close()

    return registry.add(
        IMPORT_TABLE_STREAM_KIND,
        handle,
        origin="frisket.production.import_table",
    )


def register_import_table_analysis_handler(
    registry: HandlerRegistry,
    *,
    run_analysis: TableAnalysisRunner | None,
    workspace_root: str | Path,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    """Run the server-composed table scanner on the ordinary worker queue.

    The engine owns queue admission and replay while the server service owns
    draft storage, source inspection, and preview publication.  Keeping that
    boundary as a typed callback lets standalone and hosted workers compose
    the same handler without an engine-to-server import.
    """

    def handle(payload: dict, context: JobHandlerContext) -> dict[str, object]:
        if context.job_id is None or context.job_attempt is None:
            raise ValueError("table analysis requires a row-backed queue claim")
        if run_analysis is None:
            raise RuntimeError("table analysis runtime is not configured")
        draft_id = payload.get("draft_id")
        settings_revision = payload.get("settings_revision")
        if not isinstance(draft_id, str) or not draft_id:
            raise ValueError("table analysis draft_id is required")
        if (
            isinstance(settings_revision, bool)
            or not isinstance(settings_revision, int)
            or settings_revision < 0
        ):
            raise ValueError("table analysis settings_revision must be non-negative")
        project_id, _project_root, path = claimed_project_location(
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
        try:
            run_analysis(project, project_id, draft_id, settings_revision)
        finally:
            project.close()
        return {
            "draft_id": draft_id,
            "settings_revision": settings_revision,
        }

    return registry.add(
        IMPORT_TABLE_ANALYSIS_KIND,
        handle,
        origin="frisket.production.import_table_analysis",
    )


__all__ = [
    "TableAnalysisRunner",
    "register_import_table_analysis_handler",
    "register_import_table_handler",
    "table_analysis_dedupe_key",
    "table_import_dedupe_key",
]

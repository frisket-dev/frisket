"""Resume reviewed source-page preparation through the ordinary import cursor."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock
from pydantic import JsonValue

from frisket.contracts.http.import_sessions import (
    MAX_IMPORT_ROW_STATE_IDS,
    ImportPreparationRetryEstimate,
)
from frisket.engine.store.import_intake import (
    import_intake_dir,
    read_import_header,
    write_import_header,
)
from frisket.engine.store.local_readings import (
    LocalReading,
    local_reading_receipt_id,
    local_reading_status,
    record_local_reading,
)
from frisket.engine.store.import_sessions import (
    ImportSessionStore,
    import_session_generation_is_applied,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.pdf_split_intake import read_pdf_split_plan
from frisket.engine.store.pdf_split_text import (
    capture_pdf_split_text_identities,
    publish_pdf_split_pages,
)
from frisket.notifications.emit import emit_notification_candidate
from frisket.notifications.producers import import_preparation_notification_candidate
from frisket.redaction import safe_error
from frisket.server.services.pdf_import_ocr_consent import (
    PdfImportOcrConsentError,
    PdfImportOcrGrant,
)
from frisket.server.services.import_child_execution import (
    import_child_port,
    import_execution_context,
)
from frisket.server.services.pdf_split_preparation import (
    CACHED_READING_ONLY,
    PdfSplitChildBinding,
    PdfSplitPageTarget,
    PdfSplitPageWork,
    PdfSplitPreparationAdapter,
    PdfSplitPreparationStore,
    page_attempt_identity,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PdfImportPreparationSummary:
    """Where reading a packet import's page text stands."""

    import_ref: str
    scheduled_through: int
    ocr_total: int
    ocr_completed: int
    semantic_error: str | None
    error: str | None
    children: tuple[Mapping[str, Any], ...]
    stopped: bool
    reading_row_ids: tuple[int, ...]
    waiting_row_ids: tuple[int, ...]
    failed_pages: int


LOG = logging.getLogger(__name__)

# A page is not read when its reading failed, or when a restart interrupted it
# while the person had not stopped the import. A page whose import was undone
# or stopped is neither: nothing is left to read it into.
_NOT_READ_WORK = "(status='failed' OR (status='cancelled' AND interrupted=1))"


def _not_read(state: Mapping[str, Any], *, stopped: bool) -> int:
    return int(state.get("failed", 0)) + (
        0 if stopped else int(state.get("interrupted", 0))
    )


def _settled(state: Mapping[str, Any]) -> bool:
    return not any(
        state.get(key, 0)
        for key in ("pending", "running", "stopping", "pending_bindings")
    )


class PdfSplitPreparationJobs:
    def __init__(self, workspace: Any, *, preview_runs: Any) -> None:
        self.workspace = workspace
        self.preview_runs = preview_runs
        self._owed_logged: set[tuple[str, str]] = set()

    def _log_owed(self, event: str, import_ref: str, exc: Exception) -> None:
        """Warn once per import that its outcome is still owed; recovery retries it."""
        level = (
            logging.DEBUG
            if (event, import_ref) in self._owed_logged
            else logging.WARNING
        )
        self._owed_logged.add((event, import_ref))
        LOG.log(
            level,
            event,
            extra={
                "import_ref": import_ref,
                "error": safe_error(event, exc, max_chars=300).text,
            },
        )

    def _directory(self, project_id: str, import_ref: str) -> Path:
        return import_intake_dir(self.workspace.get(project_id).path, import_ref)

    def _adapter(self, project_id: str, directory: Path) -> PdfSplitPreparationAdapter:
        project = self.workspace.get(project_id)
        return PdfSplitPreparationAdapter(
            self.workspace,
            preview_runs=self.preview_runs,
            inventory_path=directory / "inventory.db",
            project_id=project_id,
            publish_child_pages=lambda bindings, ref: publish_pdf_split_pages(
                project, bindings, ref
            ),
        )

    def on_terminal_import(
        self, project_id: str, receipt_id: str, sheet_id: int
    ) -> None:
        project = self.workspace.get(project_id)
        row = project.db.execute(
            "SELECT id,sheet_id FROM import_sessions WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is not None and int(row[1]) == sheet_id:
            self.progress(project_id, str(row[0]))

    def _admit(
        self, project_id: str, import_ref: str, *, lock_timeout: float = -1
    ) -> str | None:
        project = self.workspace.get(project_id)
        directory = self._directory(project_id, import_ref)
        with FileLock(
            str(directory / ".pdf-split-preparation-admit.lock"), timeout=lock_timeout
        ):
            header = read_import_header(directory)
            facts = header.pdf_preparation
            if facts is None or facts.get("kind") != "pdf_split_source_pages":
                return None
            if (
                header.project_id != project_id
                or header.storage_identity != project.storage_identity
            ):
                raise ValueError("source-page preparation belongs to another project")
            provider = dict(header.provider_state or {})
            if provider.get("preparation_generation"):
                return str(provider["preparation_generation"])
            session = ImportSessionStore(project).get(import_ref)
            if session is None or session.state not in {"kept", "completed"}:
                return None
            if header.cancel_requested or provider.get("preparation_stopped"):
                return None
            if facts.get("ocr_authorization") is None:
                provider.update(
                    preparation_pending=False,
                    preparation_generation=session.writer_authority,
                )
                write_import_header(directory, replace(header, provider_state=provider))
                return session.writer_authority
            grant = PdfImportOcrGrant.from_mapping(facts["ocr_authorization"])
            if (
                grant.scope.get("project_id") != project_id
                or grant.scope.get("plan_token") != facts["plan_token"]
            ):
                raise ValueError("source-page preparation approval changed")
            plan = read_pdf_split_plan(directory)
            sources = {str(source["key"]): source for source in plan["sources"]}
            if session.sheet_id is None:
                return None
            columns = {
                str(column["name"]): int(column["id"])
                for column in project.columns(int(session.sheet_id))
            }
            media_column, text_column = (
                columns[plan["roles"]["media"]],
                columns[plan["roles"]["text"]],
            )
            ledger = project.db.execute(
                "SELECT source_id,child_count,state FROM import_session_pdf_sources WHERE session_id=? ORDER BY ordinal",
                (import_ref,),
            ).fetchall()
            rows_by_source = {}
            through = 0
            for item in ledger:
                if item["state"] != "published":
                    continue
                count = int(item["child_count"])
                rows_by_source[str(item["source_id"])] = [
                    int(row[0])
                    for row in project.db.execute(
                        "SELECT row_id FROM import_session_rows WHERE session_id=? AND ordinal>? AND ordinal<=? ORDER BY ordinal",
                        (import_ref, through, through + count),
                    )
                ]
                through += count
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                # This header is host-owned and was installed only after the
                # exact UI approval. No HTTP request may supply this grant.
                with store.db:
                    store.db.execute(
                        "INSERT OR REPLACE INTO pdf_split_page_grants VALUES(?,?)",
                        (
                            facts["plan_token"],
                            json.dumps(
                                grant.as_mapping(),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                for admitted in grant.scope["targets"]:
                    key, page = (
                        str(admitted["occurrence_key"]),
                        int(admitted["page_number"]),
                    )
                    if key not in rows_by_source:
                        continue
                    source = sources[key]
                    for child, row_id in zip(
                        source["children"], rows_by_source[key], strict=True
                    ):
                        if not int(child["start"]) <= page <= int(child["end"]):
                            continue
                        provenance = project.db.execute(
                            "SELECT span.artifact_id FROM evidence_links link JOIN evidence_link_spans linked ON linked.link_id=link.id "
                            "JOIN source_spans span ON span.id=linked.span_id WHERE link.op_id=? AND link.row_id=? AND link.column_id=? "
                            "AND link.link_role='source_provenance' AND span.page_start<=? AND span.page_end>=? ORDER BY link.id LIMIT 1",
                            (session.op_id, row_id, media_column, page, page),
                        ).fetchone()
                        artifact = project.db.execute(
                            "SELECT id FROM source_artifacts WHERE source_row_id=? AND source_column_id=? AND page_count=? ORDER BY id DESC LIMIT 1",
                            (
                                row_id,
                                media_column,
                                int(child["end"]) - int(child["start"]) + 1,
                            ),
                        ).fetchone()
                        if provenance is None or artifact is None:
                            raise ValueError(
                                "split child lost its retained PDF provenance"
                            )
                        target = PdfSplitPageTarget(
                            key,
                            int(provenance[0]),
                            str(admitted["blob_hash"]),
                            admitted["source_column_id"],
                            page,
                            admitted["ocr_action_params"],
                        )
                        local_page = page - int(child["start"]) + 1
                        existing = store.db.execute(
                            "SELECT 1 FROM pdf_split_page_bindings WHERE work_key=? AND generation=? AND row_id=? AND page_number=?",
                            (
                                target.work_key,
                                session.writer_authority,
                                row_id,
                                local_page,
                            ),
                        ).fetchone()
                        if existing:
                            continue
                        source_identity, text_identity = (
                            capture_pdf_split_text_identities(
                                project,
                                row_id=row_id,
                                source_column_id=media_column,
                                text_column_id=text_column,
                                original_artifact_id=int(provenance[0]),
                                original_page_number=page,
                            )
                        )
                        binding = PdfSplitChildBinding(
                            session.op_id,
                            session.writer_authority,
                            session.id,
                            key,
                            target.work_key,
                            row_id,
                            int(artifact[0]),
                            media_column,
                            text_column,
                            local_page,
                            source_identity,
                            text_identity,
                        )
                        store.admit(
                            operation_id=session.op_id,
                            generation=session.writer_authority,
                            session_id=session.id,
                            targets=[target],
                            bindings=[binding],
                            approved_plan_token=str(facts["plan_token"]),
                        )
            provider.update(
                preparation_pending=False,
                preparation_generation=session.writer_authority,
            )
            write_import_header(directory, replace(header, provider_state=provider))
            return session.writer_authority

    def progress(
        self, project_id: str, import_ref: str, *, lock_timeout: float = -1
    ) -> int:
        generation = self._admit(project_id, import_ref, lock_timeout=lock_timeout)
        project = self.workspace.get(project_id)
        session = ImportSessionStore(project).get(import_ref)
        if generation is None:
            return (
                0
                if session is None
                else (session.cursor if session.state in {"kept", "completed"} else 0)
            )
        directory = self._directory(project_id, import_ref)
        header = read_import_header(directory)
        adapter = self._adapter(project_id, directory)
        if header.cancel_requested or (header.provider_state or {}).get(
            "preparation_stopped"
        ):
            adapter.stop(generation=generation)
        else:
            if import_child_port(self.workspace) is None:
                adapter.start_pending(generation=generation)
            else:
                parent_receipt_id = str(header.envelope["receipt_id"])
                adapter.start_pending(
                    generation=generation,
                    request_context=import_execution_context(
                        self.workspace, project_id, parent_receipt_id
                    ),
                    parent_receipt_id=parent_receipt_id,
                )
        adapter.resume_fanout(generation=generation)
        state = adapter.progress(generation=generation)
        if session is None or not _settled(state):
            return 0
        # Recovery keeps an import whose outcome is still owed and records it
        # on a later tick; the outcome is owed until it is durably recorded.
        recorded = self._record_local_reading(project, directory, generation)
        notified = self._notify_outcome(project, import_ref, directory, session, state)
        return int(session.cursor) if recorded and notified else 0

    def _superseded(self, project, session, generation: str) -> bool:
        """Whether the rows this page work reads into were undone or replaced."""
        return session is None or not import_session_generation_is_applied(
            project.db,
            session_id=session.id,
            writer_authority=generation,
            op_id=int(session.op_id),
        )

    def _record_local_reading(self, project, directory: Path, generation: str) -> bool:
        """Leave one receipt for the pages this pass read on this machine.

        A page read through a provider has its own receipt; a page read
        locally has none, so the settled pass records those pages together.
        Recording never fails the import. Returns whether nothing is left
        to record.
        """
        try:
            header = read_import_header(directory)
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                rows = store.db.execute(
                    "SELECT work_key,target_json,status FROM pdf_split_page_work "
                    "WHERE generation=? AND read_locally=1 AND receipt_id IS NULL "
                    "AND status IN ('completed','failed','cancelled') ORDER BY rowid",
                    (generation,),
                ).fetchall()
                if not rows:
                    return True
                bindings = {
                    str(work_key): [
                        {"row_id": int(row_id), "page_number": int(page)}
                        for row_id, page in store.db.execute(
                            "SELECT row_id,page_number FROM pdf_split_page_bindings "
                            "WHERE work_key=? AND generation=? "
                            "ORDER BY row_id,page_number",
                            (work_key, generation),
                        )
                    ]
                    for work_key, _target, _status in rows
                }
            files = {
                str(source["key"]): str(source["filename"])
                for source in read_pdf_split_plan(directory)["sources"]
            }
            targets = [json.loads(row["target_json"]) for row in rows]
            settings: dict[str, JsonValue] = json.loads(
                json.dumps(targets[0]["ocr_action_params"])
            )
            pages: list[JsonValue] = [
                {
                    "file": files.get(str(target["occurrence_key"])),
                    "file_key": str(target["occurrence_key"]),
                    "page_number": int(target["page_number"]),
                    "status": str(row["status"]),
                    "rows": [dict(item) for item in bindings[str(row["work_key"])]],
                }
                for row, target in zip(rows, targets, strict=True)
            ]
            receipt_id = local_reading_receipt_id(
                "media.ocr",
                str(header.envelope["receipt_id"]),
                generation,
                int((header.provider_state or {}).get("retries", 0)),
            )
            record_local_reading(
                project,
                LocalReading(
                    action_kind="media.ocr",
                    engine=str(settings.get("engine")),
                    service="ocr",
                    settings=settings,
                    pages=pages,
                    status=local_reading_status(str(row["status"]) for row in rows),
                ),
                receipt_id=receipt_id,
                project_id=str(header.project_id),
                import_receipt_id=str(header.envelope["receipt_id"]),
            )
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                with store.db:
                    store.db.executemany(
                        "UPDATE pdf_split_page_work SET receipt_id=? WHERE work_key=? "
                        "AND generation=? AND receipt_id IS NULL",
                        [(receipt_id, row["work_key"], generation) for row in rows],
                    )
        except Exception as exc:
            self._log_owed("import_local_reading_receipt_failed", directory.name, exc)
            if project.db.in_transaction:
                project.db.rollback()
            return False
        return True

    def _notify_outcome(self, project, import_ref, directory, session, state) -> bool:
        """Record the settled outcome once; a failed emission never fails the import.

        Returns whether the outcome is recorded, or there is none to record.
        """
        header = read_import_header(directory)
        provider = dict(header.provider_state or {})
        if header.cancel_requested or provider.get("preparation_stopped"):
            return True
        generation = str(provider.get("preparation_generation") or "")
        if self._superseded(project, session, generation):
            return True
        candidate = import_preparation_notification_candidate(
            project_id=header.project_id,
            import_ref=import_ref,
            sheet_id=int(session.sheet_id),
            read_pages=int(state.get("completed", 0)),
            failed_pages=_not_read(state, stopped=False),
        )
        if (
            candidate is None
            or provider.get("outcome_notified") == candidate["dedupe_key"]
        ):
            return True
        try:
            emit_notification_candidate(project, candidate)
        except Exception as exc:
            self._log_owed("import_notification_emit_failed", import_ref, exc)
            if project.db.in_transaction:
                project.db.rollback()
            return False
        with FileLock(str(directory / ".pdf-split-preparation-admit.lock")):
            header = read_import_header(directory)
            provider = dict(header.provider_state or {})
            provider["outcome_notified"] = candidate["dedupe_key"]
            write_import_header(directory, replace(header, provider_state=provider))
        return True

    def _unread_work(self, store, generation) -> tuple[PdfSplitPageWork, ...]:
        return tuple(
            store._work(row)
            for row in store.db.execute(
                f"SELECT * FROM pdf_split_page_work WHERE generation=? AND {_NOT_READ_WORK} "
                "ORDER BY rowid",
                (generation,),
            )
        )

    def _retry_scope(self, project_id: str, import_ref: str):
        directory = self._directory(project_id, import_ref)
        header = read_import_header(directory)
        provider = header.provider_state or {}
        generation = provider.get("preparation_generation")
        if not generation:
            raise ValueError("This import has no page text to retry")
        if header.cancel_requested or provider.get("preparation_stopped"):
            raise ValueError("Reading this import's text was stopped")
        project = self.workspace.get(project_id)
        session = ImportSessionStore(project).get(import_ref)
        if session is None or self._superseded(project, session, str(generation)):
            raise ValueError("This import's rows are no longer available")
        sheet = project.db.execute(
            "SELECT hidden FROM sheets WHERE id=?", (session.sheet_id,)
        ).fetchone()
        if sheet is None or sheet[0]:
            # Deleting a sheet can be undone; its pages are read again then.
            raise ValueError(
                "This import's sheet was deleted; restore it to read its pages again"
            )
        with PdfSplitPreparationStore(directory / "inventory.db") as store:
            unread = self._unread_work(store, str(generation))
        if not unread:
            raise ValueError("This import has no failed pages to retry")
        # The token names exactly the pages not read and the retry they belong
        # to, so consent to one retry estimate authorizes that retry once.
        plan_token = (
            "retry:"
            + hashlib.sha256(
                json.dumps(
                    [
                        str(generation),
                        int(provider.get("retries", 0)),
                        sorted(work.work_key for work in unread),
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        return directory, str(generation), unread, plan_token

    def _estimate_unread(self, directory, project_id, unread, plan_token, context):
        try:
            return self._adapter(project_id, directory).estimate(
                targets=[work.target for work in unread],
                plan_token=plan_token,
                request_context=context,
                reuse_previews=False,
            )
        except PdfImportOcrConsentError as exc:
            raise ValueError(str(exc)) from exc

    def estimate_retry(
        self, project_id: str, import_ref: str, *, request_context: Any = None
    ) -> ImportPreparationRetryEstimate:
        directory, _generation, unread, plan_token = self._retry_scope(
            project_id, import_ref
        )
        estimated = self._estimate_unread(
            directory, project_id, unread, plan_token, request_context
        )
        return ImportPreparationRetryEstimate.model_validate(
            {
                "plan_token": plan_token,
                "failed_pages": len(unread),
                "pages": int(estimated["pages"]),
                "estimate": dict(estimated["estimate"]),
            }
        )

    def retry(
        self,
        project_id: str,
        import_ref: str,
        *,
        plan_token: str,
        confirmation: str | None,
        request_context: Any = None,
    ) -> PdfImportPreparationSummary:
        directory = self._directory(project_id, import_ref)
        adapter = self._adapter(project_id, directory)
        with FileLock(str(directory / ".pdf-split-preparation-admit.lock")):
            _directory, generation, unread, current_token = self._retry_scope(
                project_id, import_ref
            )
            if plan_token != current_token:
                raise ValueError(
                    "The failed pages changed; review the retry estimate again"
                )
            # Consent is checked against the estimate as it stands now: a
            # changed page set, engine, route or price changes its hash.
            current = self._estimate_unread(
                directory, project_id, unread, plan_token, request_context
            )
            if current["authorization"] is not None:
                adapter.approve(current, confirmation=confirmation)
            consented = (
                set()
                if current["authorization"] is None
                else {
                    str(target["work_key"])
                    for target in current["authorization"]["scope"]["targets"]
                }
            )
            # The retry is spent before any page is reset, so a crash between
            # the two leaves its confirmation void rather than reusable.
            header = read_import_header(directory)
            provider = dict(header.provider_state or {})
            provider["retries"] = int(provider.get("retries", 0)) + 1
            write_import_header(directory, replace(header, provider_state=provider))
            parent = (
                str(header.envelope["receipt_id"])
                if import_child_port(self.workspace) is not None
                else None
            )
            receipts = ReceiptStore(self.workspace.get(project_id))
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                with store.db:
                    for work in unread:
                        status, attempt = store.db.execute(
                            "SELECT status,attempt FROM pdf_split_page_work "
                            "WHERE work_key=? AND generation=?",
                            (work.work_key, generation),
                        ).fetchone()
                        if work.work_key in consented:
                            token = plan_token
                        elif adapter.reading_reference(store, work.target) is not None:
                            token = CACHED_READING_ONLY
                        else:
                            continue
                        # A failed attempt is settled, so reading the page again
                        # is a new attempt. An interrupted one keeps its identity
                        # unless its receipt shows it began: then it cannot be
                        # replayed and reading again is a new attempt too.
                        if status == "failed" or (
                            parent is not None
                            and receipts.find_by_id(
                                page_attempt_identity(
                                    parent, generation, work.work_key, int(attempt)
                                )[1]
                            )
                            is not None
                        ):
                            attempt = int(attempt) + 1
                        store.db.execute(
                            "UPDATE pdf_split_page_work SET status='pending',error=NULL,job_id=NULL,"
                            "receipt_id=NULL,read_locally=0,plan_token=?,attempt=? WHERE work_key=? "
                            f"AND generation=? AND {_NOT_READ_WORK}",
                            (token, attempt, work.work_key, generation),
                        )
                        store.db.execute(
                            "UPDATE pdf_split_page_bindings SET published=0 "
                            "WHERE work_key=? AND generation=? AND published=-1",
                            (work.work_key, generation),
                        )
        self.progress(project_id, import_ref)
        return self.summary(project_id, import_ref)

    def child_page(
        self, project_id: str, import_ref: str, *, after: int, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        header = read_import_header(self._directory(project_id, import_ref))
        generation = (header.provider_state or {}).get("preparation_generation")
        if not generation:
            return ()
        with PdfSplitPreparationStore(
            self._directory(project_id, import_ref) / "inventory.db"
        ) as store:
            return tuple(
                {
                    "rowid": int(row["rowid"]),
                    "child_key": str(row["work_key"]),
                    "action_id": "media.ocr",
                    "row_count": 1,
                    "completed_rows": int(
                        row["status"] in {"completed", "failed", "cancelled"}
                    ),
                    "status": str(row["status"]),
                    "receipt_id": row["receipt_id"],
                    "run_id": None,
                    "job_id": None,
                }
                for row in store.db.execute(
                    "SELECT rowid,* FROM pdf_split_page_work WHERE generation=? AND rowid>? ORDER BY rowid LIMIT ?",
                    (generation, after, min(100, max(1, limit))),
                )
            )

    def summary(self, project_id: str, import_ref: str) -> PdfImportPreparationSummary:
        directory = self._directory(project_id, import_ref)
        header = read_import_header(directory)
        provider = header.provider_state or {}
        generation = provider.get("preparation_generation")
        counts = (
            {}
            if not generation
            else self._adapter(project_id, directory).progress(
                generation=str(generation)
            )
        )
        approved = (header.pdf_preparation or {}).get("ocr_authorization") or {}
        total = int(counts.get("pages", approved.get("scope", {}).get("page_count", 0)))
        done = int(counts.get("completed", 0))
        project = self.workspace.get(project_id)
        session = ImportSessionStore(project).get(import_ref)
        stopped = bool(provider.get("preparation_stopped") or header.cancel_requested)
        unread = (
            0
            if not generation or self._superseded(project, session, str(generation))
            else _not_read(counts, stopped=stopped)
        )
        reading, waiting = (
            ((), ()) if not generation else self._row_states(directory, str(generation))
        )
        return PdfImportPreparationSummary(
            import_ref=import_ref,
            scheduled_through=0
            if session is None or not _settled(counts)
            else session.cursor,
            ocr_total=total,
            ocr_completed=done,
            semantic_error=provider.get("semantic_error"),
            error="Some PDF pages could not be prepared." if unread else None,
            children=self.child_page(project_id, import_ref, after=0),
            stopped=stopped,
            reading_row_ids=reading,
            waiting_row_ids=waiting,
            failed_pages=unread,
        )

    @staticmethod
    def _row_states(
        directory: Path, generation: str
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Rows with a page being read, and rows whose pages wait, front first."""
        with PdfSplitPreparationStore(directory / "inventory.db") as store:
            rows = store.db.execute(
                "SELECT binding.row_id, MAX(work.status='running') AS reading "
                "FROM pdf_split_page_bindings AS binding JOIN pdf_split_page_work AS work "
                "ON work.work_key=binding.work_key AND work.generation=binding.generation "
                "WHERE binding.generation=? AND binding.published=0 "
                "AND work.status IN ('pending','running') "
                "GROUP BY binding.row_id ORDER BY reading DESC, MAX(work.priority) DESC, "
                "MIN(work.rowid) LIMIT ?",
                (generation, MAX_IMPORT_ROW_STATE_IDS),
            ).fetchall()
        return (
            tuple(int(row[0]) for row in rows if row[1]),
            tuple(int(row[0]) for row in rows if not row[1]),
        )

    def stop(self, project_id: str, import_ref: str) -> PdfImportPreparationSummary:
        directory = self._directory(project_id, import_ref)
        with FileLock(str(directory / ".pdf-split-preparation-admit.lock")):
            header = read_import_header(directory)
            provider = dict(header.provider_state or {})
            provider["preparation_stopped"] = True
            write_import_header(directory, replace(header, provider_state=provider))
        generation = provider.get("preparation_generation")
        if generation:
            self._adapter(project_id, directory).stop(generation=str(generation))
        return self.summary(project_id, import_ref)

    def record_semantic_error(
        self,
        project_id: str,
        import_ref: str,
        message: str | None,
        *,
        lock_timeout: float = -1,
    ) -> None:
        directory = self._directory(project_id, import_ref)
        with FileLock(
            str(directory / ".pdf-split-preparation-admit.lock"), timeout=lock_timeout
        ):
            header = read_import_header(directory)
            provider = dict(header.provider_state or {})
            provider["semantic_error"] = message[:500] if message else None
            write_import_header(directory, replace(header, provider_state=provider))

    def coverage(self, project_id: str, import_ref: str) -> dict[str, int]:
        project = self.workspace.get(project_id)
        session = ImportSessionStore(project).get(import_ref)
        if session is None or session.sheet_id is None:
            return {
                "total_pages": 0,
                "covered_pages": 0,
                "available_rows": 0,
                "complete_rows": 0,
            }
        rows = project.db.execute(
            "SELECT artifact.page_count,COUNT(member.page_number) AS covered FROM import_session_rows row "
            "JOIN document_text_column_roles role ON role.source_column_id IN (SELECT id FROM columns WHERE sheet_id=?) "
            "JOIN source_artifacts artifact ON artifact.source_row_id=row.row_id AND artifact.source_column_id=role.source_column_id "
            "AND artifact.page_count IS NOT NULL LEFT JOIN current_document_text_selections selection "
            "ON selection.row_id=row.row_id AND selection.source_column_id=role.source_column_id "
            "LEFT JOIN prepared_content_refs ref ON ref.id=selection.prepared_ref_id "
            "LEFT JOIN prepared_content_set_pages member ON member.set_id=ref.set_id "
            "WHERE row.session_id=? GROUP BY artifact.id",
            (session.sheet_id, import_ref),
        ).fetchall()
        return {
            "total_pages": sum(int(row[0]) for row in rows),
            "covered_pages": sum(int(row[1]) for row in rows),
            "available_rows": sum(int(row[1]) > 0 for row in rows),
            "complete_rows": sum(int(row[1]) == int(row[0]) for row in rows),
        }

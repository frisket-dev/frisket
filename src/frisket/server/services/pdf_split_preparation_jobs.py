"""Resume reviewed source-page preparation through the ordinary import cursor."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock

from frisket.contracts.http.import_sessions import ImportPreparationRetryEstimate
from frisket.engine.store.import_intake import (
    import_intake_dir,
    read_import_header,
    write_import_header,
)
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.pdf_split_intake import read_pdf_split_plan
from frisket.engine.store.pdf_split_text import (
    capture_pdf_split_text_identities,
    publish_pdf_split_pages,
)
from frisket.notifications.emit import emit_notification_candidate
from frisket.notifications.producers import import_preparation_notification_candidate
from frisket.server.services.pdf_import_ocr_consent import (
    PdfImportOcrConsentError,
    PdfImportOcrGrant,
)
from frisket.server.services.pdf_split_preparation import (
    PdfSplitChildBinding,
    PdfSplitPageTarget,
    PdfSplitPageWork,
    PdfSplitPreparationAdapter,
    PdfSplitPreparationStore,
)

_ROW_STATE_LIMIT = 500


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


def _settled(state: Mapping[str, Any]) -> bool:
    return not any(
        state.get(key, 0)
        for key in ("pending", "running", "stopping", "pending_bindings")
    )


class PdfSplitPreparationJobs:
    def __init__(self, workspace: Any, *, preview_runs: Any) -> None:
        self.workspace = workspace
        self.preview_runs = preview_runs

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
            from frisket.server.services.import_child_execution import (
                import_child_port,
                import_execution_context,
            )

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
        if not _settled(state):
            return 0
        self._notify_outcome(project, import_ref, directory, session, state)
        return int(session.cursor)

    def _notify_outcome(self, project, import_ref, directory, session, state) -> None:
        header = read_import_header(directory)
        provider = dict(header.provider_state or {})
        if header.cancel_requested or provider.get("preparation_stopped"):
            return
        candidate = import_preparation_notification_candidate(
            project_id=header.project_id,
            import_ref=import_ref,
            sheet_id=int(session.sheet_id),
            read_pages=int(state.get("completed", 0)),
            failed_pages=int(state.get("failed", 0)),
        )
        if (
            candidate is None
            or provider.get("outcome_notified") == candidate["dedupe_key"]
        ):
            return
        emit_notification_candidate(project, candidate)
        with FileLock(str(directory / ".pdf-split-preparation-admit.lock")):
            header = read_import_header(directory)
            provider = dict(header.provider_state or {})
            provider["outcome_notified"] = candidate["dedupe_key"]
            write_import_header(directory, replace(header, provider_state=provider))

    def _failed_work(self, store, generation) -> tuple[PdfSplitPageWork, ...]:
        return tuple(
            store._work(row)
            for row in store.db.execute(
                "SELECT * FROM pdf_split_page_work WHERE generation=? AND status='failed' ORDER BY rowid",
                (generation,),
            )
        )

    def _retry_scope(self, project_id: str, import_ref: str):
        directory = self._directory(project_id, import_ref)
        generation = (read_import_header(directory).provider_state or {}).get(
            "preparation_generation"
        )
        if not generation:
            raise ValueError("This import has no page text to retry")
        with PdfSplitPreparationStore(directory / "inventory.db") as store:
            failed = self._failed_work(store, str(generation))
        if not failed:
            raise ValueError("This import has no failed pages to retry")
        # The token names exactly the pages that failed, so consent to one
        # retry estimate never re-authorizes pages the import already read.
        plan_token = (
            "retry:"
            + hashlib.sha256(
                json.dumps(
                    [str(generation), sorted(work.work_key for work in failed)],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        return directory, str(generation), failed, plan_token

    def estimate_retry(
        self, project_id: str, import_ref: str, *, request_context: Any = None
    ) -> ImportPreparationRetryEstimate:
        directory, _generation, failed, plan_token = self._retry_scope(
            project_id, import_ref
        )
        try:
            estimated = self._adapter(project_id, directory).estimate(
                targets=[work.target for work in failed],
                plan_token=plan_token,
                request_context=request_context,
            )
        except PdfImportOcrConsentError as exc:
            raise ValueError(str(exc)) from exc
        return ImportPreparationRetryEstimate.model_validate(
            {
                "plan_token": plan_token,
                "failed_pages": len(failed),
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
    ) -> PdfImportPreparationSummary:
        directory, generation, failed, current_token = self._retry_scope(
            project_id, import_ref
        )
        if plan_token != current_token:
            raise ValueError(
                "The failed pages changed; review the retry estimate again"
            )
        adapter = self._adapter(project_id, directory)
        with FileLock(str(directory / ".pdf-split-preparation-admit.lock")):
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                estimated = store.db.execute(
                    "SELECT grant_json FROM pdf_split_page_quotes WHERE plan_token=?",
                    (plan_token,),
                ).fetchone()
            if estimated is not None:
                adapter.approve(
                    {"authorization": json.loads(estimated[0])},
                    confirmation=confirmation,
                )
            with PdfSplitPreparationStore(directory / "inventory.db") as store:
                consented = (
                    set()
                    if estimated is None
                    else {
                        str(target["work_key"])
                        for target in json.loads(estimated[0])["scope"]["targets"]
                    }
                )
                with store.db:
                    for work in failed:
                        if work.work_key in consented:
                            token = plan_token
                        elif adapter.cached_reference(work.target) is not None:
                            token = None
                        else:
                            continue
                        store.db.execute(
                            "UPDATE pdf_split_page_work SET status='pending',error=NULL,job_id=NULL,"
                            "receipt_id=NULL,plan_token=? WHERE work_key=? AND generation=? AND status='failed'",
                            (token, work.work_key, generation),
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
        session = ImportSessionStore(self.workspace.get(project_id)).get(import_ref)
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
            error="Some PDF pages could not be prepared."
            if counts.get("failed")
            else None,
            children=self.child_page(project_id, import_ref, after=0),
            stopped=bool(
                provider.get("preparation_stopped") or header.cancel_requested
            ),
            reading_row_ids=reading,
            waiting_row_ids=waiting,
            failed_pages=int(counts.get("failed", 0)),
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
                (generation, _ROW_STATE_LIMIT),
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

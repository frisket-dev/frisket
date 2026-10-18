"""Durable bounded cursor for reviewed PDF post-import work."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_intake import import_intake_dir, read_import_header
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.server.route_errors import RouteError


@dataclass(frozen=True, slots=True)
class PdfPreparationPage:
    scheduled_through: int
    through: int
    rows: tuple[dict[str, Any], ...]
    pending_confirmation: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PdfImportPreparationSummary:
    import_ref: str
    scheduled_through: int
    native_total: int
    native_completed: int
    ocr_total: int
    ocr_completed: int
    unreadable_total: int
    pending_confirmation: Mapping[str, Any] | None
    children: tuple[Mapping[str, Any], ...]


class PdfImportPreparationStore:
    """Keep preparation state beside the canonical ordinary inventory."""

    def __init__(self, inventory_path: Path) -> None:
        self.inventory = ImportInventory(inventory_path)
        self.db = self.inventory._db
        metadata = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='pdf_commit_occurrences'"
        ).fetchone()
        if metadata is None:
            raise ValueError("PDF commit occurrence metadata is unavailable")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pdf_commit_preparation ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "scheduled_through INTEGER NOT NULL DEFAULT 0,"
            "pending_confirmation_json TEXT,"
            "native_total INTEGER NOT NULL DEFAULT 0,"
            "ocr_total INTEGER NOT NULL DEFAULT 0,"
            "unreadable_total INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {
            str(item["name"])
            for item in self.db.execute("PRAGMA table_info(pdf_commit_preparation)")
        }
        if "pending_confirmation_json" not in columns:
            self.db.execute(
                "ALTER TABLE pdf_commit_preparation "
                "ADD COLUMN pending_confirmation_json TEXT"
            )
        for name in ("native_total", "ocr_total", "unreadable_total"):
            if name not in columns:
                self.db.execute(
                    f"ALTER TABLE pdf_commit_preparation ADD COLUMN {name} "
                    "INTEGER NOT NULL DEFAULT 0"
                )
        counts = self.db.execute(
            "SELECT "
            "SUM(CASE WHEN ocr_selected=0 AND category NOT IN "
            "('missing','unchecked') THEN 1 ELSE 0 END) AS native_total,"
            "SUM(CASE WHEN ocr_selected=1 AND category!='unreadable' "
            "THEN 1 ELSE 0 END) AS ocr_total,"
            "SUM(CASE WHEN category='unreadable' THEN 1 ELSE 0 END) "
            "AS unreadable_total FROM pdf_commit_occurrences"
        ).fetchone()
        self.db.execute(
            "INSERT OR IGNORE INTO pdf_commit_preparation"
            "(singleton,scheduled_through,native_total,ocr_total,unreadable_total) "
            "VALUES(1,0,?,?,?)",
            (
                int(counts["native_total"] or 0),
                int(counts["ocr_total"] or 0),
                int(counts["unreadable_total"] or 0),
            ),
        )
        self.db.execute(
            "CREATE TRIGGER IF NOT EXISTS pdf_commit_preparation_occurrence_insert "
            "AFTER INSERT ON pdf_commit_occurrences BEGIN "
            "UPDATE pdf_commit_preparation SET "
            "native_total=native_total + CASE WHEN NEW.ocr_selected=0 "
            "AND NEW.category NOT IN ('missing','unchecked') THEN 1 ELSE 0 END,"
            "ocr_total=ocr_total + CASE WHEN NEW.ocr_selected=1 "
            "AND NEW.category!='unreadable' THEN 1 ELSE 0 END,"
            "unreadable_total=unreadable_total + CASE "
            "WHEN NEW.category='unreadable' THEN 1 ELSE 0 END WHERE singleton=1; "
            "END"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pdf_commit_preparation_children ("
            "child_key TEXT PRIMARY KEY,"
            "action_id TEXT NOT NULL,"
            "row_count INTEGER NOT NULL,"
            "receipt_id TEXT,run_id INTEGER,job_id INTEGER)"
        )
        self.db.commit()

    def close(self) -> None:
        self.inventory.close()

    def __enter__(self) -> PdfImportPreparationStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def page(
        self,
        project: Any,
        *,
        producer_id: int,
        committed_through: int,
        limit: int = 256,
    ) -> PdfPreparationPage:
        state = self.db.execute(
            "SELECT scheduled_through,pending_confirmation_json "
            "FROM pdf_commit_preparation WHERE singleton=1"
        ).fetchone()
        scheduled = int(state["scheduled_through"])
        pending = (
            None
            if state["pending_confirmation_json"] is None
            else json.loads(str(state["pending_confirmation_json"]))
        )
        metadata = self.db.execute(
            "SELECT ordinal,occurrence_id,category,error,ocr_selected "
            "FROM pdf_commit_occurrences WHERE ordinal>? AND ordinal<=? "
            "ORDER BY ordinal LIMIT ?",
            (scheduled, int(committed_through), int(limit)),
        ).fetchall()
        if not metadata:
            return PdfPreparationPage(scheduled, scheduled, (), pending)
        through = int(metadata[-1]["ordinal"])
        row_map = {
            int(item["ordinal"]): int(item["row_id"])
            for item in project.db.execute(
                "SELECT ordinal,row_id FROM ("
                "SELECT row_id,ROW_NUMBER() OVER (ORDER BY row_id) AS ordinal "
                "FROM (SELECT DISTINCT row_id FROM cells WHERE producer_id=?)) "
                "WHERE ordinal>? AND ordinal<=? ORDER BY ordinal",
                (int(producer_id), scheduled, through),
            ).fetchall()
        }
        if len(row_map) != len(metadata):
            raise ValueError("PDF import row mapping is incomplete")
        rows = tuple(
            {
                "row_id": row_map[int(item["ordinal"])],
                "occurrence_id": str(item["occurrence_id"]),
                "category": str(item["category"]),
                "error": str(item["error"]) if item["error"] else None,
                "ocr_selected": bool(item["ocr_selected"]),
            }
            for item in metadata
        )
        return PdfPreparationPage(scheduled, through, rows, pending)

    def advance(self, *, expected: int, through: int) -> bool:
        updated = self.db.execute(
            "UPDATE pdf_commit_preparation SET scheduled_through=?,"
            "pending_confirmation_json=NULL WHERE singleton=1 "
            "AND scheduled_through=? AND pending_confirmation_json IS NULL",
            (int(through), int(expected)),
        )
        self.db.commit()
        return updated.rowcount == 1

    def record_child(
        self,
        *,
        child_key: str,
        action_id: str,
        row_count: int,
        response: Mapping[str, Any],
    ) -> None:
        """Persist the ordinary action identity used by this import group."""

        receipt_id = response.get("receipt_id")
        run_id = response.get("run_id")
        job_id = response.get("job_id")
        self.db.execute(
            "INSERT INTO pdf_commit_preparation_children"
            "(child_key,action_id,row_count,receipt_id,run_id,job_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(child_key) DO UPDATE SET "
            "receipt_id=COALESCE(receipt_id,excluded.receipt_id),"
            "run_id=COALESCE(run_id,excluded.run_id),"
            "job_id=COALESCE(job_id,excluded.job_id)",
            (
                child_key,
                action_id,
                int(row_count),
                str(receipt_id) if receipt_id else None,
                int(run_id) if isinstance(run_id, int) else None,
                int(job_id) if isinstance(job_id, int) else None,
            ),
        )
        self.db.commit()

    def summary(self, project: Any, *, import_ref: str) -> PdfImportPreparationSummary:
        """Read durable aggregate progress without bounded activity history."""

        state = self.db.execute(
            "SELECT * FROM pdf_commit_preparation WHERE singleton=1"
        ).fetchone()
        pending = (
            None
            if state["pending_confirmation_json"] is None
            else json.loads(str(state["pending_confirmation_json"]))
        )
        completed = {"media.read_pdf_text": 0, "media.ocr": 0}
        children: list[dict[str, Any]] = []
        for child in self.db.execute(
            "SELECT child_key,action_id,row_count,run_id,receipt_id,job_id "
            "FROM pdf_commit_preparation_children ORDER BY rowid"
        ).fetchall():
            action_id = str(child["action_id"])
            run = (
                project.db.execute(
                    "SELECT status,completed_rows,failed_rows FROM runs WHERE id=?",
                    (int(child["run_id"]),),
                ).fetchone()
                if child["run_id"] is not None
                else None
            )
            status = str(run["status"]) if run is not None else "queued"
            completed_rows = 0
            if run is not None:
                completed_rows = min(
                    int(child["row_count"]), int(run["completed_rows"] or 0)
                )
            elif child["receipt_id"] is not None:
                receipt = project.db.execute(
                    "SELECT status FROM receipts WHERE id=?", (str(child["receipt_id"]),)
                ).fetchone()
                if receipt is not None:
                    status = str(receipt["status"])
            if action_id in completed:
                completed[action_id] += completed_rows
            children.append(
                {
                    "child_key": str(child["child_key"]),
                    "action_id": action_id,
                    "row_count": int(child["row_count"]),
                    "completed_rows": completed_rows,
                    "status": status,
                    "receipt_id": str(child["receipt_id"])
                    if child["receipt_id"] is not None
                    else None,
                    "run_id": int(child["run_id"])
                    if child["run_id"] is not None
                    else None,
                    "job_id": int(child["job_id"])
                    if child["job_id"] is not None
                    else None,
                }
            )
        return PdfImportPreparationSummary(
            import_ref=import_ref,
            scheduled_through=int(state["scheduled_through"]),
            native_total=int(state["native_total"]),
            native_completed=min(
                int(state["native_total"]), completed["media.read_pdf_text"]
            ),
            ocr_total=int(state["ocr_total"]),
            ocr_completed=min(int(state["ocr_total"]), completed["media.ocr"]),
            unreadable_total=int(state["unreadable_total"]),
            pending_confirmation=pending,
            children=tuple(children),
        )


class _OutputBusy(Exception):
    pass


class PdfImportPreparationCoordinator:
    """Translate frozen PDF review facts into ordinary text actions."""

    def __init__(
        self,
        workspace: Any,
        *,
        action_runs: Any,
        pending_store: Any,
        document_text_target: Callable[..., Any],
        ensure_semantic_setup: Callable[..., None],
    ) -> None:
        self.workspace = workspace
        self.action_runs = action_runs
        self.pending_store = pending_store
        self.document_text_target = document_text_target
        self.ensure_semantic_setup = ensure_semantic_setup

    def create(
        self,
        project_id: str,
        *,
        import_receipt_id: str,
        sheet_id: int,
        rows: Sequence[Mapping[str, Any]],
        ocr_policy: str,
        ocr_authorization: Mapping[str, Any] | None,
        semantic_search: bool,
        semantic_setup_confirmation: str | None,
        ocr_action_params: Mapping[str, Any],
        on_started: Callable[[str, str, int, Mapping[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if ocr_policy not in {"missing_or_suspicious", "all", "none"}:
            raise ValueError("invalid reviewed PDF OCR policy")
        if not import_receipt_id:
            raise ValueError("reviewed PDF text authority is incomplete")
        reviewed_ocr_params = dict(ocr_action_params)
        if not str(reviewed_ocr_params.get("engine") or ""):
            raise ValueError("reviewed PDF OCR parameters have no engine")
        estimate = (
            ocr_authorization.get("estimate")
            if isinstance(ocr_authorization, Mapping)
            else None
        )
        claims = estimate.get("claims") if isinstance(estimate, Mapping) else None
        if claims:
            raise RouteError(
                503, "Paid PDF OCR needs the draft-scope batch execution adapter."
            )
        project = self.workspace.get(project_id)
        source = next(
            (
                column
                for column in project.columns(sheet_id, include_hidden=False)
                if str(column["name"]).casefold() == "media"
            ),
            None,
        )
        if source is None:
            raise RouteError(409, "PDF import media column is unavailable")
        target = self.document_text_target(project, source_column_id=int(source["id"]))
        native_rows = sorted(
            int(item["row_id"])
            for item in rows
            if not bool(item.get("ocr_selected"))
            and str(item.get("category")) not in {"missing", "unchecked"}
        )
        ocr_rows = sorted(
            int(item["row_id"])
            for item in rows
            if bool(item.get("ocr_selected"))
            and str(item.get("category")) != "unreadable"
        )
        responses: list[dict[str, Any]] = []

        def start(action_id: str, row_ids: Sequence[int], *, ocr: bool) -> None:
            if not row_ids:
                return
            identity = hashlib.sha256(
                json.dumps(
                    {
                        "receipt_id": import_receipt_id,
                        "action_id": action_id,
                        "row_ids": list(row_ids),
                        "ocr_params": reviewed_ocr_params if ocr else None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            body = {
                "action_id": action_id,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": int(sheet_id),
                    "row_ids": list(row_ids),
                },
                "params": {
                    "source": str(source["name"]),
                    **(reviewed_ocr_params if ocr else {}),
                },
                "output_names": {
                    "text": target.name,
                    **(
                        {"blocks": f"Search positions · {source['name']}"}
                        if ocr
                        else {}
                    ),
                },
                "replace_existing": True,
                "idempotency_key": f"pdf-import-text:{identity}",
            }
            current_columns = {
                str(column["name"]): int(column["id"])
                for column in project.columns(sheet_id, include_hidden=True)
            }
            body["expected_output_columns"] = {
                name: current_columns.get(name)
                for name in body["output_names"].values()
            }
            response = self.action_runs.run_action(
                project_id,
                body,
                background_priority="pdf_import_ocr" if ocr else None,
            )
            status = str(response.payload.get("status") or "")
            errors = response.payload.get("errors")
            error_code = (
                str(errors[0].get("code") or "")
                if isinstance(errors, list) and errors and isinstance(errors[0], dict)
                else ""
            )
            if error_code == "output_column_busy":
                raise _OutputBusy
            if status not in {"queued", "running", "completed", "needs_confirmation"}:
                message = "PDF text preparation could not start."
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    message = str(errors[0].get("message") or message)
                raise RouteError(response.status_code, message)
            payload = dict(response.payload)
            if on_started is not None:
                on_started(action_id, identity, len(row_ids), payload)
            responses.append(payload)

        start("media.read_pdf_text", native_rows, ocr=False)
        start("media.ocr", ocr_rows, ocr=True)
        _ = semantic_search, semantic_setup_confirmation
        return tuple(responses)

    def reserve(self, project_id: str, *, receipt_id: str, import_ref: str) -> None:
        self.pending_store.create_pending_import(
            storage_org_id=self.workspace.queue_storage_org_id,
            project_id=project_id,
            receipt_id=receipt_id,
            import_ref=import_ref,
        )

    def progress(self, project_id: str, import_ref: str) -> int:
        project = self.workspace.get(project_id)
        directory = import_intake_dir(project.path, import_ref)
        header = read_import_header(directory)
        facts = header.pdf_preparation
        if facts is None or header.cancel_requested:
            return 0
        session = ImportSessionStore(project).get(import_ref)
        if session is None or session.sheet_id is None or session.producer_id is None:
            return 0
        with PdfImportPreparationStore(directory / "inventory.db") as preparation:
            page = preparation.page(
                project,
                producer_id=session.producer_id,
                committed_through=session.cursor,
            )
            if page.pending_confirmation is not None or not page.rows:
                return page.scheduled_through
            self.ensure_semantic_setup(
                project_id,
                import_receipt_id=str(header.envelope["receipt_id"]),
                enabled=bool(facts.get("semantic_search")),
                confirmation=facts.get("semantic_setup_confirmation"),
            )
            try:
                responses = self.create(
                    project_id,
                    import_receipt_id=str(header.envelope["receipt_id"]),
                    sheet_id=session.sheet_id,
                    rows=page.rows,
                    ocr_policy=str(facts["ocr_policy"]),
                    ocr_authorization=None,
                    semantic_search=bool(facts.get("semantic_search")),
                    semantic_setup_confirmation=facts.get(
                        "semantic_setup_confirmation"
                    ),
                    ocr_action_params=facts["ocr_action_params"],
                    on_started=lambda action_id, child_key, row_count, response: (
                        preparation.record_child(
                            child_key=child_key,
                            action_id=action_id,
                            row_count=row_count,
                            response=response,
                        )
                    ),
                )
            except _OutputBusy:
                return page.scheduled_through
            confirmation = next(
                (
                    response
                    for response in responses
                    if response.get("status") == "needs_confirmation"
                ),
                None,
            )
            if confirmation is not None:
                preparation.block_for_confirmation(
                    expected=page.scheduled_through, response=confirmation
                )
                return page.scheduled_through
            if not preparation.advance(
                expected=page.scheduled_through, through=page.through
            ):
                return self.progress(project_id, import_ref)
            return page.through

    def summary(self, project_id: str, import_ref: str) -> PdfImportPreparationSummary:
        project = self.workspace.get(project_id)
        inventory_path = import_intake_dir(project.path, import_ref) / "inventory.db"
        with PdfImportPreparationStore(inventory_path) as preparation:
            return preparation.summary(project, import_ref=import_ref)

    def block_for_confirmation(
        self, *, expected: int, response: Mapping[str, Any]
    ) -> bool:
        encoded = json.dumps(dict(response), sort_keys=True, allow_nan=False)
        updated = self.db.execute(
            "UPDATE pdf_commit_preparation SET pending_confirmation_json=? "
            "WHERE singleton=1 AND scheduled_through=? "
            "AND pending_confirmation_json IS NULL",
            (encoded, int(expected)),
        )
        self.db.commit()
        return updated.rowcount == 1


__all__ = [
    "PdfImportPreparationStore",
    "PdfImportPreparationSummary",
    "PdfImportPreparationCoordinator",
    "PdfPreparationPage",
]

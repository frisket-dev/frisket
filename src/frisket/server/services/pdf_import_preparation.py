"""Durable bounded cursor for reviewed PDF post-import work."""

from __future__ import annotations

import json
import hashlib
from decimal import Decimal, InvalidOperation
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock

from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_intake import import_intake_dir, read_import_header
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.cells import replay_generated_value_hash
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.runner.confirmation_context import ConsentQuote
from frisket.engine.runner.validation import (
    TrustedConsentApproval,
    TrustedConsentContext,
    TrustedConsentRefusal,
)
from frisket.execution.promises import promise_rows_hash
from frisket.execution.resolve_for_action import promise_rows, target_snapshot
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.route_errors import RouteError
from frisket.server.services.pdf_import_ocr_approval import PdfImportOcrGrant
from frisket.server.services.pdf_imports.inventory import pdf_ocr_selected

_CHILD_STATUS_PAGE = 100
_TERMINAL_CHILD_STATUSES = ("completed", "failed", "cancelled", "partial")


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
    semantic_error: str | None
    children: tuple[Mapping[str, Any], ...]
    stopped: bool
    row_error_summary: Mapping[str, Any] | None


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
            "semantic_error TEXT,"
            "stopped INTEGER NOT NULL DEFAULT 0,"
            "native_total INTEGER NOT NULL DEFAULT 0,"
            "ocr_total INTEGER NOT NULL DEFAULT 0,"
            "unreadable_total INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(pdf_commit_preparation)")
        }
        if "semantic_error" not in columns:
            self.db.execute(
                "ALTER TABLE pdf_commit_preparation ADD COLUMN semantic_error TEXT"
            )
        state = self.db.execute(
            "SELECT 1 FROM pdf_commit_preparation WHERE singleton=1"
        ).fetchone()
        if state is None:
            counts = self.db.execute(
                "SELECT "
                "SUM(CASE WHEN category='unreadable' OR (ocr_selected=0 "
                "AND category NOT IN ('missing','unchecked')) "
                "THEN 1 ELSE 0 END) AS native_total,"
                "SUM(CASE WHEN ocr_selected=1 AND category!='unreadable' "
                "THEN 1 ELSE 0 END) AS ocr_total,"
                "SUM(CASE WHEN category='unreadable' THEN 1 ELSE 0 END) "
                "AS unreadable_total FROM pdf_commit_occurrences"
            ).fetchone()
            self.db.execute(
                "INSERT INTO pdf_commit_preparation"
                "(singleton,scheduled_through,native_total,ocr_total,"
                "unreadable_total) VALUES(1,0,?,?,?)",
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
            "native_total=native_total + CASE WHEN NEW.category='unreadable' "
            "OR (NEW.ocr_selected=0 AND NEW.category NOT IN "
            "('missing','unchecked')) THEN 1 ELSE 0 END,"
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
            "receipt_id TEXT,run_id INTEGER,job_id INTEGER,"
            "status TEXT NOT NULL DEFAULT 'queued',"
            "completed_rows INTEGER NOT NULL DEFAULT 0)"
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
        import_ref: str,
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
            "SELECT ordinal,occurrence_id,selected_ordinal,category,error,"
            "sha256,page_count,ocr_selected "
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
                "SELECT ordinal,row_id FROM import_session_rows "
                "WHERE session_id=? AND ordinal>? AND ordinal<=? ORDER BY ordinal",
                (import_ref, scheduled, through),
            ).fetchall()
        }
        if len(row_map) != len(metadata):
            raise ValueError("PDF import row mapping is incomplete")
        rows = tuple(
            {
                "row_id": row_map[int(item["ordinal"])],
                "ordinal": int(item["ordinal"]),
                "occurrence_id": str(item["occurrence_id"]),
                "selected_ordinal": int(item["selected_ordinal"]),
                "category": str(item["category"]),
                "error": str(item["error"]) if item["error"] else None,
                "sha256": str(item["sha256"]) if item["sha256"] else None,
                "page_count": (
                    int(item["page_count"]) if item["page_count"] is not None else None
                ),
                "ocr_selected": bool(item["ocr_selected"]),
            }
            for item in metadata
        )
        return PdfPreparationPage(scheduled, through, rows, pending)

    def advance(self, *, expected: int, through: int) -> bool:
        updated = self.db.execute(
            "UPDATE pdf_commit_preparation SET scheduled_through=?,"
            "pending_confirmation_json=NULL WHERE singleton=1 "
            "AND scheduled_through=? AND pending_confirmation_json IS NULL "
            "AND stopped=0",
            (int(through), int(expected)),
        )
        self.db.commit()
        return updated.rowcount == 1

    def block_for_confirmation(
        self, *, expected: int, response: Mapping[str, Any]
    ) -> bool:
        encoded = json.dumps(dict(response), sort_keys=True, allow_nan=False)
        updated = self.db.execute(
            "UPDATE pdf_commit_preparation SET pending_confirmation_json=? "
            "WHERE singleton=1 AND scheduled_through=? "
            "AND pending_confirmation_json IS NULL AND stopped=0",
            (encoded, int(expected)),
        )
        self.db.commit()
        return updated.rowcount == 1

    def scheduled_through(self) -> int:
        row = self.db.execute(
            "SELECT scheduled_through FROM pdf_commit_preparation WHERE singleton=1"
        ).fetchone()
        return int(row["scheduled_through"])

    def stopped(self) -> bool:
        row = self.db.execute(
            "SELECT stopped FROM pdf_commit_preparation WHERE singleton=1"
        ).fetchone()
        return bool(row["stopped"])

    def stop(self) -> None:
        self.db.execute(
            "UPDATE pdf_commit_preparation SET stopped=1,"
            "pending_confirmation_json=NULL WHERE singleton=1"
        )
        self.db.commit()

    def record_semantic_error(self, message: str | None) -> None:
        self.db.execute(
            "UPDATE pdf_commit_preparation SET semantic_error=? WHERE singleton=1",
            (message[:500] if message else None,),
        )
        self.db.commit()

    def _row_error_summary(
        self, project: Any, *, import_ref: str
    ) -> Mapping[str, Any] | None:
        total = self.db.execute(
            "SELECT COUNT(*) AS count FROM pdf_commit_occurrences "
            "WHERE category='unreadable' AND error IS NOT NULL AND error<>''"
        ).fetchone()
        if total is None or int(total["count"]) == 0:
            return None
        groups = []
        for group in self.db.execute(
            "SELECT error,COUNT(*) AS count FROM pdf_commit_occurrences "
            "WHERE category='unreadable' AND error IS NOT NULL AND error<>'' "
            "GROUP BY error ORDER BY count DESC,error LIMIT 10"
        ).fetchall():
            rows = self.db.execute(
                "SELECT ordinal FROM pdf_commit_occurrences WHERE error=? "
                "ORDER BY ordinal LIMIT 5",
                (str(group["error"]),),
            ).fetchall()
            ordinals = [int(row["ordinal"]) for row in rows]
            row_ids: list[int] = []
            if ordinals:
                placeholders = ",".join("?" for _ in ordinals)
                row_ids = [
                    int(row["row_id"])
                    for row in project.db.execute(
                        "SELECT row_id FROM import_session_rows "
                        "WHERE session_id=? AND ordinal IN "
                        f"({placeholders}) ORDER BY ordinal",
                        (import_ref, *ordinals),
                    ).fetchall()
                ]
            groups.append(
                {
                    "message": str(group["error"]),
                    "count": int(group["count"]),
                    "code": "pdf_unreadable",
                    "outcome": "row_error",
                    "terminal": False,
                    "row_ids": row_ids,
                }
            )
        return {"total_failed_rows": int(total["count"]), "groups": groups}

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
        status = str(response.get("status") or "queued")
        completed_rows = int(row_count) if status in _TERMINAL_CHILD_STATUSES else 0
        self.db.execute(
            "INSERT INTO pdf_commit_preparation_children"
            "(child_key,action_id,row_count,receipt_id,run_id,job_id,status,"
            "completed_rows) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(child_key) DO UPDATE SET "
            "receipt_id=COALESCE(receipt_id,excluded.receipt_id),"
            "run_id=COALESCE(run_id,excluded.run_id),"
            "job_id=COALESCE(job_id,excluded.job_id),"
            "status=excluded.status,"
            "completed_rows=MAX(completed_rows,excluded.completed_rows)",
            (
                child_key,
                action_id,
                int(row_count),
                str(receipt_id) if receipt_id else None,
                int(run_id) if isinstance(run_id, int) else None,
                int(job_id) if isinstance(job_id, int) else None,
                status,
                completed_rows,
            ),
        )
        self.db.commit()

    def _refresh_child_statuses(self, project: Any) -> None:
        rows = self.db.execute(
            "SELECT child_key,row_count,run_id,receipt_id,status,completed_rows "
            "FROM pdf_commit_preparation_children WHERE status NOT IN "
            "(?,?,?,?) ORDER BY rowid LIMIT ?",
            (*_TERMINAL_CHILD_STATUSES, _CHILD_STATUS_PAGE),
        ).fetchall()
        if not rows:
            return
        run_ids = [int(row["run_id"]) for row in rows if row["run_id"] is not None]
        run_map: dict[int, Any] = {}
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            run_map = {
                int(row["id"]): row
                for row in project.db.execute(
                    "SELECT id,status,completed_rows FROM runs "
                    f"WHERE id IN ({placeholders})",
                    tuple(run_ids),
                ).fetchall()
            }
        receipt_ids = [
            str(row["receipt_id"])
            for row in rows
            if row["run_id"] is None and row["receipt_id"] is not None
        ]
        receipt_map: dict[str, str] = {}
        if receipt_ids:
            placeholders = ",".join("?" for _ in receipt_ids)
            receipt_map = {
                str(row["id"]): str(row["status"])
                for row in project.db.execute(
                    f"SELECT id,status FROM receipts WHERE id IN ({placeholders})",
                    tuple(receipt_ids),
                ).fetchall()
            }
        updates = []
        for child in rows:
            status = str(child["status"])
            completed_rows = int(child["completed_rows"])
            if child["run_id"] is not None:
                run = run_map.get(int(child["run_id"]))
                if run is not None:
                    status = str(run["status"])
                    completed_rows = (
                        int(child["row_count"])
                        if status in _TERMINAL_CHILD_STATUSES
                        else min(
                            int(child["row_count"]),
                            int(run["completed_rows"] or 0),
                        )
                    )
            elif child["receipt_id"] is not None:
                status = receipt_map.get(str(child["receipt_id"]), status)
                if status in _TERMINAL_CHILD_STATUSES:
                    completed_rows = int(child["row_count"])
            updates.append((status, completed_rows, str(child["child_key"])))
        self.db.executemany(
            "UPDATE pdf_commit_preparation_children SET status=?,"
            "completed_rows=? WHERE child_key=?",
            updates,
        )
        self.db.commit()

    @staticmethod
    def _child_payload(child: Any) -> dict[str, Any]:
        return {
            "child_key": str(child["child_key"]),
            "action_id": str(child["action_id"]),
            "row_count": int(child["row_count"]),
            "completed_rows": int(child["completed_rows"]),
            "status": str(child["status"]),
            "receipt_id": (
                str(child["receipt_id"]) if child["receipt_id"] is not None else None
            ),
            "run_id": int(child["run_id"]) if child["run_id"] is not None else None,
            "job_id": int(child["job_id"]) if child["job_id"] is not None else None,
        }

    def child_page(self, *, after: int, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if after < 0 or limit < 1 or limit > 100:
            raise ValueError("invalid PDF preparation child page")
        return tuple(
            {
                "rowid": int(child["rowid"]),
                **self._child_payload(child),
            }
            for child in self.db.execute(
                "SELECT rowid,* FROM pdf_commit_preparation_children "
                "WHERE rowid>? ORDER BY rowid LIMIT ?",
                (int(after), int(limit)),
            ).fetchall()
        )

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
        self._refresh_child_statuses(project)
        completed = {
            str(row["action_id"]): int(row["completed"] or 0)
            for row in self.db.execute(
                "SELECT action_id,SUM(completed_rows) AS completed "
                "FROM pdf_commit_preparation_children GROUP BY action_id"
            ).fetchall()
        }
        active = self.db.execute(
            "SELECT * FROM pdf_commit_preparation_children WHERE status NOT IN "
            "(?,?,?,?) ORDER BY rowid LIMIT ?",
            (*_TERMINAL_CHILD_STATUSES, _CHILD_STATUS_PAGE),
        ).fetchall()
        remaining = _CHILD_STATUS_PAGE - len(active)
        terminal = (
            self.db.execute(
                "SELECT * FROM pdf_commit_preparation_children WHERE status IN "
                "(?,?,?,?) ORDER BY rowid DESC LIMIT ?",
                (*_TERMINAL_CHILD_STATUSES, remaining),
            ).fetchall()
            if remaining
            else ()
        )
        children = [self._child_payload(child) for child in (*active, *terminal)]
        return PdfImportPreparationSummary(
            import_ref=import_ref,
            scheduled_through=int(state["scheduled_through"]),
            native_total=int(state["native_total"]),
            native_completed=min(
                int(state["native_total"]), completed.get("media.read_pdf_text", 0)
            ),
            ocr_total=int(state["ocr_total"]),
            ocr_completed=min(int(state["ocr_total"]), completed.get("media.ocr", 0)),
            unreadable_total=int(state["unreadable_total"]),
            pending_confirmation=pending,
            semantic_error=(
                str(state["semantic_error"]) if state["semantic_error"] else None
            ),
            children=tuple(children),
            stopped=bool(state["stopped"]),
            row_error_summary=self._row_error_summary(project, import_ref=import_ref),
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
        grant = (
            PdfImportOcrGrant.from_mapping(ocr_authorization)
            if ocr_authorization is not None
            else None
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
            if str(item.get("category")) == "unreadable"
            or (
                not bool(item.get("ocr_selected"))
                and str(item.get("category")) not in {"missing", "unchecked"}
            )
        )
        ocr_rows = sorted(
            int(item["row_id"])
            for item in rows
            if bool(item.get("ocr_selected"))
            and pdf_ocr_selected(ocr_policy, str(item.get("category")))
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
            body["expected_output_columns"] = {
                name: (target.column_id if name == target.name else None)
                for name in body["output_names"].values()
            }
            run_kwargs: dict[str, Any] = {
                "background_priority": "pdf_import_ocr" if ocr else None,
            }
            if ocr and grant is not None:
                run_kwargs["trusted_consent_authorizer"] = self._grant_authorizer(
                    project,
                    sheet_id=sheet_id,
                    source_column_id=int(source["id"]),
                    rows=rows,
                    ocr_policy=ocr_policy,
                    reviewed_ocr_params=reviewed_ocr_params,
                    grant=grant,
                )
                run_kwargs["trusted_consent_principal"] = grant.actor
            response = self.action_runs.run_action(project_id, body, **run_kwargs)
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

    @staticmethod
    def _route_snapshot(context: TrustedConsentContext) -> dict[str, Any]:
        facts = context.resolved_execution.resolution.facts
        return {
            "engine": facts.engine,
            "operator": facts.operator,
            "egress_class": facts.egress_class,
            "region": facts.region,
            "cost_posture": facts.cost_posture,
            "cloud_payer": (
                facts.cloud_payer.to_snapshot()
                if facts.cloud_payer is not None
                else None
            ),
        }

    @staticmethod
    def _promises_without_row_coordinates(
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized = json.loads(json.dumps(list(rows), sort_keys=True))
        for row in normalized:
            basis = row.get("basis")
            if isinstance(basis, dict):
                basis.pop("row_quote_quantities", None)
                basis.pop("row_quote_quantities_digest", None)
                basis.pop("row_quote_quantities_count", None)
                basis.pop("work_scope", None)
        return normalized

    def _grant_authorizer(
        self,
        project: Any,
        *,
        sheet_id: int,
        source_column_id: int,
        rows: Sequence[Mapping[str, Any]],
        ocr_policy: str,
        reviewed_ocr_params: Mapping[str, Any],
        grant: PdfImportOcrGrant,
    ) -> Callable[[TrustedConsentContext], Any]:
        ocr_rows = tuple(
            row
            for row in rows
            if bool(row.get("ocr_selected"))
            and pdf_ocr_selected(ocr_policy, str(row.get("category")))
        )
        if not ocr_rows:
            raise ValueError("paid PDF OCR grant has no rows in this child")
        if dict(grant.ocr_action_params) != dict(reviewed_ocr_params):
            raise ValueError("paid PDF OCR parameters changed after approval")
        start = min(int(row["ordinal"]) for row in rows)
        end = max(int(row["ordinal"]) for row in rows)
        child_key = f"commit-ordinals:{start}-{end}"
        children = grant.allocation.get("children")
        child = (
            next(
                (
                    dict(value)
                    for value in children
                    if isinstance(value, Mapping)
                    and value.get("child_key") == child_key
                ),
                None,
            )
            if isinstance(children, list)
            else None
        )
        if child is None:
            raise ValueError("paid PDF OCR child is outside the approved allocation")

        digest = hashlib.sha256()
        page_total = 0
        by_row_id: dict[int, Mapping[str, Any]] = {}
        blobs = MediaBlobStore(project)
        for row in ocr_rows:
            try:
                ordinal = int(row["ordinal"])
                selected_ordinal = int(row["selected_ordinal"])
                row_id = int(row["row_id"])
                sha256 = str(row["sha256"])
                page_count = int(row["page_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "paid PDF OCR occurrence facts are incomplete"
                ) from exc
            if not sha256 or page_count < 1 or row_id in by_row_id:
                raise ValueError("paid PDF OCR occurrence facts are invalid")
            source = resolve_cell_blob_source(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=source_column_id,
            )
            if source is None or source.page_number is not None:
                raise ValueError("paid PDF OCR source cell changed after import")
            if source.blob_hash.removeprefix("sha256:") != sha256.removeprefix(
                "sha256:"
            ):
                raise ValueError("paid PDF OCR source bytes changed after approval")
            probe = blobs.probe_metadata(source.blob_hash)
            if probe.get("pages") != page_count:
                raise ValueError("paid PDF OCR page count changed after approval")
            fact = {
                "commit_ordinal": ordinal,
                "selected_ordinal": selected_ordinal,
                "occurrence_id": str(row["occurrence_id"]),
                "sha256": sha256,
                "page_count": page_count,
                "category": str(row["category"]),
            }
            digest.update(
                json.dumps(fact, sort_keys=True, separators=(",", ":")).encode()
            )
            digest.update(b"\n")
            page_total += page_count
            by_row_id[row_id] = row
        if (
            int(child.get("commit_ordinal_start", -1)) != start
            or int(child.get("commit_ordinal_end", -1)) != end
            or int(child.get("source_count", -1)) != len(ocr_rows)
            or int(child.get("page_count", -1)) != page_total
            or child.get("roster_digest") != digest.hexdigest()
        ):
            raise ValueError("paid PDF OCR child roster changed after approval")
        child_scope = child.get("scope")
        if not isinstance(child_scope, Mapping) or any(
            (
                child_scope.get("parent_identity") != grant.scope.get("identity"),
                child_scope.get("storage_identity")
                != grant.scope.get("storage_identity"),
                child_scope.get("draft_id") != grant.scope.get("draft_id"),
                child_scope.get("child_key") != child_key,
                child_scope.get("roster_digest") != digest.hexdigest(),
                int(child_scope.get("source_count", -1)) != len(ocr_rows),
                int(child_scope.get("page_count", -1)) != page_total,
                child_scope.get("ocr_params_hash")
                != replay_generated_value_hash(dict(reviewed_ocr_params)),
            )
        ):
            raise ValueError("paid PDF OCR child scope changed after approval")
        expected_promises = child.get("promises")
        expected_quote = child.get("quote")
        if not isinstance(expected_promises, list) or not isinstance(
            expected_quote, Mapping
        ):
            raise ValueError("paid PDF OCR child authority is incomplete")
        canonical_quantities = [
            [int(row["ordinal"]), int(row["page_count"])]
            for row in sorted(ocr_rows, key=lambda value: int(value["ordinal"]))
        ]
        encoded_quantities = json.dumps(
            canonical_quantities, sort_keys=True, separators=(",", ":")
        ).encode()
        quantities_digest = hashlib.sha256(encoded_quantities).hexdigest()
        restored_promises = json.loads(
            json.dumps(expected_promises, sort_keys=True, allow_nan=False)
        )
        for promise in restored_promises:
            basis = promise.get("basis")
            if not isinstance(basis, dict):
                continue
            if basis.get("row_quote_quantities_digest") is None:
                continue
            if basis.pop(
                "row_quote_quantities_digest"
            ) != quantities_digest or basis.pop(
                "row_quote_quantities_count", None
            ) != len(canonical_quantities):
                raise ValueError("paid PDF OCR child row quote authority changed")
            basis["row_quote_quantities"] = canonical_quantities
        if promise_rows_hash(restored_promises) != child.get("promise_set_hash"):
            raise ValueError("paid PDF OCR child promise set changed")

        def authorize(context: TrustedConsentContext) -> Any:
            refusal = TrustedConsentRefusal
            if context.principal != grant.actor:
                return refusal("paid PDF OCR approval belongs to another principal")
            if set(context.row_ids) != set(by_row_id):
                return refusal("paid PDF OCR runtime row scope changed")
            resolved = context.resolved_execution
            if target_snapshot(resolved.resolution) != child.get("target"):
                return refusal("paid PDF OCR execution target changed")
            if self._route_snapshot(context) != child.get("route"):
                return refusal("paid PDF OCR route changed")
            try:
                actual_quote = ConsentQuote.from_estimate(context.estimate).model_dump(
                    mode="json"
                )
            except (TypeError, ValueError) as exc:
                return refusal(f"paid PDF OCR runtime quote is invalid: {exc}")
            if actual_quote != dict(expected_quote):
                return refusal("paid PDF OCR price or pricing policy changed")
            actual_promises = promise_rows(resolved.promise_set)
            row_quantities: dict[int, int] = {}
            row_quantities_required = False
            if len(actual_promises) != len(expected_promises):
                return refusal("paid PDF OCR promise set changed")
            for promise_index, promise in enumerate(actual_promises):
                basis = promise.get("basis")
                if not isinstance(basis, Mapping):
                    continue
                if basis.get("row_settlement_mode") == "quoted_successful_rows":
                    row_quantities_required = True
                quantities = basis.get("row_quote_quantities")
                if quantities is None:
                    expected_basis = expected_promises[promise_index].get("basis")
                    if isinstance(expected_basis, Mapping) and (
                        expected_basis.get("row_quote_quantities_digest") is not None
                        or expected_basis.get("row_quote_quantities_count") is not None
                    ):
                        return refusal("paid PDF OCR row quote is incomplete")
                    continue
                if not isinstance(quantities, list):
                    return refusal("paid PDF OCR row quote is invalid")
                canonical_quantities = []
                for pair in quantities:
                    if not isinstance(pair, list | tuple) or len(pair) != 2:
                        return refusal("paid PDF OCR row quote is invalid")
                    try:
                        row_id = int(pair[0])
                        quantity = Decimal(str(pair[1]))
                    except (InvalidOperation, TypeError, ValueError):
                        return refusal("paid PDF OCR row quote is invalid")
                    expected_row = by_row_id.get(row_id)
                    if (
                        expected_row is None
                        or row_id in row_quantities
                        or quantity != Decimal(int(expected_row["page_count"]))
                    ):
                        return refusal("paid PDF OCR per-row page quote changed")
                    row_quantities[row_id] = int(quantity)
                    canonical_quantities.append(
                        [int(expected_row["ordinal"]), int(quantity)]
                    )
                expected_basis = expected_promises[promise_index].get("basis")
                if not isinstance(expected_basis, Mapping):
                    return refusal("paid PDF OCR row quote authority is missing")
                encoded = json.dumps(
                    canonical_quantities, sort_keys=True, separators=(",", ":")
                ).encode()
                if expected_basis.get("row_quote_quantities_digest") != hashlib.sha256(
                    encoded
                ).hexdigest() or expected_basis.get(
                    "row_quote_quantities_count"
                ) != len(canonical_quantities):
                    return refusal("paid PDF OCR row quote allocation changed")
            if row_quantities_required and set(row_quantities) != set(by_row_id):
                return refusal("paid PDF OCR per-row page quote is incomplete")
            if self._promises_without_row_coordinates(
                actual_promises
            ) != self._promises_without_row_coordinates(expected_promises):
                return refusal("paid PDF OCR promise terms changed")
            return TrustedConsentApproval("pdf_import_grant_derived")

        return authorize

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
        with FileLock(str(directory / ".pdf-preparation.lock")):
            header = read_import_header(directory)
            facts = header.pdf_preparation
            if facts is None or header.cancel_requested:
                return 0
            session = ImportSessionStore(project).get(import_ref)
            if (
                session is None
                or session.sheet_id is None
                or session.producer_id is None
            ):
                return 0
            with PdfImportPreparationStore(directory / "inventory.db") as preparation:
                if preparation.stopped():
                    return preparation.scheduled_through()
                page = preparation.page(
                    project,
                    import_ref=import_ref,
                    committed_through=session.cursor,
                )
                if page.pending_confirmation is not None or not page.rows:
                    return page.scheduled_through
                try:
                    self.ensure_semantic_setup(
                        project_id,
                        import_receipt_id=str(header.envelope["receipt_id"]),
                        enabled=bool(facts.get("semantic_search")),
                        confirmation=facts.get("semantic_setup_confirmation"),
                    )
                except Exception as exc:  # noqa: BLE001 - semantic setup is isolated
                    preparation.record_semantic_error(str(exc))
                else:
                    preparation.record_semantic_error(None)
                try:
                    responses = self.create(
                        project_id,
                        import_receipt_id=str(header.envelope["receipt_id"]),
                        sheet_id=session.sheet_id,
                        rows=page.rows,
                        ocr_policy=str(facts["ocr_policy"]),
                        ocr_authorization=facts.get("ocr_authorization"),
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
                    return page.scheduled_through
                return page.through

    def summary(self, project_id: str, import_ref: str) -> PdfImportPreparationSummary:
        project = self.workspace.get(project_id)
        inventory_path = import_intake_dir(project.path, import_ref) / "inventory.db"
        with PdfImportPreparationStore(inventory_path) as preparation:
            return preparation.summary(project, import_ref=import_ref)

    def child_page(
        self, project_id: str, import_ref: str, *, after: int, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        project = self.workspace.get(project_id)
        inventory_path = import_intake_dir(project.path, import_ref) / "inventory.db"
        with PdfImportPreparationStore(inventory_path) as preparation:
            return preparation.child_page(after=after, limit=limit)

    def stop(self, project_id: str, import_ref: str) -> PdfImportPreparationSummary:
        project = self.workspace.get(project_id)
        directory = import_intake_dir(project.path, import_ref)
        with FileLock(str(directory / ".pdf-preparation.lock")):
            header = read_import_header(directory)
            with PdfImportPreparationStore(directory / "inventory.db") as preparation:
                preparation.stop()
                summary = preparation.summary(project, import_ref=import_ref)
            receipt_id = header.envelope.get("receipt_id")
            if isinstance(receipt_id, str) and receipt_id:
                pending = self.pending_store.for_receipt(
                    storage_org_id=self.workspace.queue_storage_org_id,
                    project_id=project_id,
                    receipt_id=receipt_id,
                    include_pending=True,
                )
                if pending is not None:
                    self.pending_store.mark_settled(
                        pending.id,
                        project_id=project_id,
                        storage_org_id=self.workspace.queue_storage_org_id,
                    )
            return summary


__all__ = [
    "PdfImportPreparationStore",
    "PdfImportPreparationSummary",
    "PdfImportPreparationCoordinator",
    "PdfPreparationPage",
]

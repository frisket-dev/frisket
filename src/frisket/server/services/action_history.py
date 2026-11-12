"""One history reader for submitted settings and existing execution runs."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from frisket.contracts.http.action_history import (
    ActionHistoryDetail,
    ActionHistoryDraft,
    ActionHistoryItem,
    ActionHistoryPage,
)
from frisket.engine.store.action_submissions import record_submission, link_submission
from frisket.server.route_errors import RouteError
from frisket.server.services.run_action_drafts import reconstruct_run_action_draft


def editable_action_draft(body: dict[str, Any]) -> ActionHistoryDraft:
    """Keep authored settings, never execution context or consent authority."""
    return ActionHistoryDraft.model_validate(
        {key: body[key] for key in ActionHistoryDraft.model_fields if key in body}
    )


def save_action_submission(project: Any, body: dict[str, Any]) -> int | None:
    try:
        draft = editable_action_draft(body)
    except ValidationError:
        return None
    key = body.get("idempotency_key")
    return record_submission(
        project.db,
        draft.model_dump(mode="json"),
        request_key=key if isinstance(key, str) and key else None,
    )


def finish_action_submission(
    project: Any, submission_id: int, payload: dict[str, Any]
) -> None:
    link_submission(
        project.db,
        submission_id,
        receipt_id=payload.get("receipt_id"),
        run_id=payload.get("run_id"),
    )


_HISTORY = """
SELECT 'submission:' || s.id AS id, s.action_id, s.sheet_id,
       sh.name AS sheet_name, s.submitted_at,
       COALESCE(rc.status,r.status,'not_started') AS status, COALESCE(s.run_id,rc.run_id) AS run_id
FROM action_submissions s
LEFT JOIN sheets sh ON sh.id=s.sheet_id AND sh.hidden=0
LEFT JOIN receipts rc ON rc.id=s.receipt_id
LEFT JOIN runs r ON r.id=COALESCE(s.run_id,rc.run_id)
UNION ALL
SELECT 'run:' || r.id, r.action_kind, r.sheet_id, sh.name,
       r.started_at, r.status, r.id
FROM runs r
LEFT JOIN sheets sh ON sh.id=r.sheet_id AND sh.hidden=0
WHERE NOT EXISTS (
    SELECT 1 FROM action_submissions s
    LEFT JOIN receipts rc ON rc.id=s.receipt_id
    WHERE COALESCE(s.run_id,rc.run_id)=r.id
)
"""


class ActionHistoryService:
    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace

    def page(
        self, project_id: str, *, action_id: str, offset: int, limit: int
    ) -> ActionHistoryPage:
        project = self._workspace.get(project_id)
        query = f"SELECT * FROM ({_HISTORY}) WHERE action_id=?"
        total = project.db.execute(
            f"SELECT COUNT(*) FROM ({query})", (action_id,)
        ).fetchone()[0]
        rows = project.db.execute(
            query
            + " ORDER BY submitted_at DESC, CAST(substr(id,instr(id,':')+1) AS INTEGER) DESC, id DESC LIMIT ? OFFSET ?",
            (action_id, limit, offset),
        ).fetchall()
        return ActionHistoryPage(
            items=[ActionHistoryItem.model_validate(dict(row)) for row in rows],
            total=total,
            offset=offset,
            limit=limit,
        )

    def detail(self, project_id: str, history_id: str) -> ActionHistoryDetail:
        project = self._workspace.get(project_id)
        row = project.db.execute(
            f"SELECT * FROM ({_HISTORY}) WHERE id=?",
            (history_id,),
        ).fetchone()
        if row is None:
            raise RouteError(404, "These action settings are no longer available.")
        item = ActionHistoryItem.model_validate(dict(row))
        draft = None
        reason = None
        try:
            if history_id.startswith("submission:"):
                stored = project.db.execute(
                    "SELECT draft_json FROM action_submissions WHERE id=?",
                    (history_id.removeprefix("submission:"),),
                ).fetchone()
                draft = editable_action_draft(json.loads(stored[0]))
            else:
                run = project.db.execute(
                    "SELECT * FROM runs WHERE id=?", (item.run_id,)
                ).fetchone()
                draft = editable_action_draft(
                    reconstruct_run_action_draft(project, run).draft
                )
        except (ValueError, TypeError, KeyError):
            reason = "These settings cannot be restored with the current action."
        if draft is not None and draft.scope.kind == "sheet_rows":
            sheet = project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0",
                (draft.scope.sheet_id,),
            ).fetchone()
            if sheet is None:
                reason = "The source sheet is no longer available."
            elif draft.scope.row_ids is not None:
                # Never replace an empty or missing selection with all rows.
                ids = draft.scope.row_ids
                count = project.db.execute(
                    "SELECT COUNT(*) FROM rows r JOIN json_each(?) wanted ON r.id=wanted.value "
                    "WHERE r.sheet_id=? AND r.hidden=0",
                    (json.dumps(ids), draft.scope.sheet_id),
                ).fetchone()[0]
                if not ids or count != len(ids):
                    reason = "The original row selection is no longer available."
        return ActionHistoryDetail(
            item=item,
            draft=draft if reason is None else None,
            unavailable_reason=reason,
        )

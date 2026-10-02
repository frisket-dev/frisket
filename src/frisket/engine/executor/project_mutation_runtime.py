"""Shared transaction runtime for typed project-mutation capability families."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from frisket.contracts.action import ActionError
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.store.edit_overlays import (
    write_edit_overlay as write_store_edit_overlay,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore


class Refusal(Exception):
    def __init__(self, error: ActionError):
        self.error = error
        super().__init__(error.message)


def refuse(
    code: str,
    message: str,
    *,
    action_kind: str,
    field: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    raise Refusal(
        ActionError(
            code=code,
            message=message,
            action_kind=action_kind,
            field=field,
            details=dict(details or {}),
        )
    )


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def op_spec(action: _TypedProjectEnvelope, params_hash: str) -> dict[str, Any]:
    return {
        "action_id": action.kind,
        "scope": {"kind": "project"},
        "params": {**action.params, "params_hash": params_hash},
        "idempotency_key": action.idempotency_key,
    }


def write_op(
    cur: Any,
    *,
    kind: str,
    label: str,
    spec: Mapping[str, Any],
    undo_info: Mapping[str, Any],
) -> int:
    cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
        "VALUES (?, ?, ?, ?, 0)",
        (
            kind,
            label,
            json.dumps(spec, sort_keys=True),
            json.dumps(undo_info, sort_keys=True),
        ),
    )
    op_id = int(cur.lastrowid)
    cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    return op_id


def write_edit_overlay(
    project: Any,
    cur: Any,
    *,
    label: str,
    spec: Mapping[str, Any],
    targets: list[dict[str, Any]],
    stale_reason: str = "manual_cell_edit",
    undo_info: Mapping[str, Any] | None = None,
    kind: str = "edit",
) -> int:
    del cur
    return write_store_edit_overlay(
        project,
        kind=kind,
        label=label,
        spec=spec,
        targets=targets,
        stale_reason=stale_reason,
        undo_info=undo_info,
    )


def claimed_column(
    project: Any,
    column_id: int,
    *,
    action_kind: str,
    field: str = "params.column_id",
) -> None:
    claim = OutputColumnClaimStore(project).active_for_columns({column_id})
    if claim is None:
        return
    refuse(
        "output_column_busy",
        "The target output column is claimed by a running action.",
        action_kind=action_kind,
        field=field,
        details={
            "column_id": claim["column_id"],
            "output_name": claim["output_name"],
            "run_id": claim["run_id"],
            "receipt_id": claim["receipt_id"],
            "job_id": claim["job_id"],
            "claim_id": claim["id"],
            "claim_action_kind": claim["action_kind"],
            "requires_recovery": True,
        },
    )


class CallOnce:
    def __init__(
        self, project: Any, cur: Any, action: _TypedProjectEnvelope, params_hash: str
    ):
        self._project = project
        self._cur = cur
        self._action = action
        self._params_hash = params_hash
        self._called = False
        self.result: Any = None

    def _begin(self) -> None:
        if self._called:
            raise RuntimeError("project mutation capability may be called only once")
        self._called = True

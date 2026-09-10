"""Review and regenerated-value mutation capabilities and receipt facts."""

from __future__ import annotations

import json
from typing import Any, Literal

from frisket.actions.types import (
    AcceptedReplayColumn,
    AcceptedReplayValue,
    DismissedReplayValue,
    ReplayColumnAcceptor,
    ReplayValueAcceptor,
    ReplayValueDismissor,
    ReviewDecider,
    ReviewDecision,
)
from frisket.contracts.action import (
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.project_mutation_runtime import (
    CallOnce as _CallOnce,
    claimed_column as _claimed_column,
    op_spec as _op_spec,
    refuse as _refuse,
    text_hash as _text_hash,
    write_edit_overlay as _write_edit_overlay,
    write_op as _write_op,
)
from frisket.engine.store.cells import (
    MixedOriginReplayUnsupported,
    replay_generated_snapshot,
    replay_generated_value_hash,
    replay_origin_run_id,
)
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.evidence import mark_evidence_stale_for_cell_refs


def _visible_current_review_target(
    project: Any, *, run_id: int, row_id: int, column_id: int, action_kind: str
) -> dict[str, Any]:
    row = project.db.execute(
        """
        SELECT res.run_id, res.row_id, res.column_id, res.value,
               res.confidence, res.justification, res.error, res.review_state,
               res.review_decision, res.review_note,
               c.sheet_id, c.name AS column_name
        FROM results res
        JOIN columns c ON c.id=res.column_id
        LEFT JOIN cell_result_heads active_head
          ON active_head.column_id=res.column_id
          AND active_head.row_id=res.row_id AND active_head.run_id=res.run_id
        JOIN rows ON rows.id=res.row_id AND rows.sheet_id=c.sheet_id
        WHERE res.run_id=? AND res.row_id=? AND res.column_id=? AND rows.hidden=0
          AND (active_head.run_id IS NOT NULL OR (
            c.current_run_id=res.run_id AND NOT EXISTS (
              SELECT 1 FROM run_output_generations generation
              WHERE generation.column_id=c.id
            )
          ))
        """,
        (run_id, row_id, column_id),
    ).fetchone()
    if row is None:
        _refuse(
            "review_target_not_found",
            "review.decision target result cell was not found on a visible row",
            action_kind=action_kind,
            field="params",
            details={"run_id": run_id, "row_id": row_id, "column_id": column_id},
        )
    return dict(row)


def _replay_origin_run(
    project: Any, *, sheet_id: int, column_id: int, action_kind: str
) -> int | None:
    try:
        return replay_origin_run_id(project, sheet_id, column_id)
    except MixedOriginReplayUnsupported:
        _refuse(
            "mixed_origin_column_unsupported",
            "Replay acceptance is unavailable for a mixed-origin column.",
            action_kind=action_kind,
            field="params.column_id",
            details={"sheet_id": sheet_id, "column_id": column_id},
        )
    except ValueError:
        _refuse(
            "invalid_replay_target",
            "Replay target must identify a visible generated column.",
            action_kind=action_kind,
            field="params.column_id",
            details={"sheet_id": sheet_id, "column_id": column_id},
        )


def _exact_replay_value(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    expected_run_id: int,
    expected_hash: str,
    action_kind: str,
) -> dict[str, Any]:
    _replay_origin_run(
        project, sheet_id=sheet_id, column_id=column_id, action_kind=action_kind
    )
    snapshot = replay_generated_snapshot(project, sheet_id, row_id, column_id)
    current_run_id = snapshot["run_id"]
    if current_run_id != expected_run_id:
        _refuse(
            "stale_replay",
            "The regenerated result run changed before the replay decision.",
            action_kind=action_kind,
            field="params.run_id",
            details={
                "expected_run_id": expected_run_id,
                "current_run_id": current_run_id,
            },
        )
    if "value" not in snapshot:
        _refuse(
            "invalid_replay_target",
            "The current regenerated result cell is unavailable.",
            action_kind=action_kind,
            field="params",
            details={
                "sheet_id": sheet_id,
                "row_id": row_id,
                "column_id": column_id,
                "run_id": expected_run_id,
            },
        )
    fresh_value = snapshot["value"]
    current_hash = str(snapshot["generated_value_hash"])
    if current_hash != expected_hash:
        _refuse(
            "stale_replay",
            "The regenerated value changed before the replay decision.",
            action_kind=action_kind,
            field="params.generated_value_hash",
            details={
                "expected_generated_value_hash": expected_hash,
                "current_generated_value_hash": current_hash,
            },
        )
    return {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "run_id": expected_run_id,
        "fresh_value": fresh_value,
        "generated_value_hash": current_hash,
    }


class _ReviewDecider(_CallOnce):
    def decide(
        self,
        *,
        run_id: int,
        row_id: int,
        column_id: int,
        decision: Literal["accept", "reject", "reject_clear", "edit"],
        value: Any,
        value_supplied: bool,
        note: str | None,
    ) -> ReviewDecision:
        self._begin()
        if decision == "edit" and not value_supplied:
            _refuse(
                "review_value_required",
                "review.decision edit requires a replacement value (which may be null)",
                action_kind=self._action.kind,
                field="params.value",
            )
        target = _visible_current_review_target(
            self._project,
            run_id=run_id,
            row_id=row_id,
            column_id=column_id,
            action_kind=self._action.kind,
        )
        _claimed_column(self._project, column_id, action_kind=self._action.kind)
        state_before = str(target["review_state"])
        state_after = (
            "rejected" if decision in {"reject", "reject_clear"} else "verified"
        )
        current_ref: dict[str, Any] | None = None
        if decision in {"edit", "reject_clear"}:
            _values, refs = self._project.get_values_with_refs(
                int(target["sheet_id"]), column_id, row_ids=[row_id]
            )
            ref = refs.get(row_id)
            current_ref = dict(ref) if isinstance(ref, dict) else None
        op_id = _write_op(
            self._cur,
            kind=self._action.kind,
            label=f"review {decision} {target['column_name']} row {row_id}",
            spec=_op_spec(self._action, self._params_hash),
            undo_info={
                "review_states": {f"{run_id}:{row_id}:{column_id}": state_before},
                "review_states_after": {f"{run_id}:{row_id}:{column_id}": state_after},
                "review_metadata": {
                    f"{run_id}:{row_id}:{column_id}": {
                        "decision": target["review_decision"],
                        "note": target["review_note"],
                    }
                },
                "review_metadata_after": {
                    f"{run_id}:{row_id}:{column_id}": {
                        "decision": decision,
                        "note": note,
                    }
                },
            },
        )
        from frisket.engine.store.runs import RunResultStore

        store = RunResultStore(self._project)
        updated = store.set_result_review_state(
            run_id, row_id, column_id, state_after, commit=False
        )
        if updated != 1:
            _refuse(
                "review_target_not_found",
                "review.decision target result cell was not found",
                action_kind=self._action.kind,
                field="params",
                details={"run_id": run_id, "row_id": row_id, "column_id": column_id},
            )
        store.set_result_review_metadata(
            run_id, row_id, column_id, decision, note, commit=False
        )
        edit_ref = None
        if decision in {"edit", "reject_clear"}:
            edit_value = value if decision == "edit" else None
            insert_edits(
                self._project.db,
                op_id=op_id,
                edits=[
                    EditCellWrite(
                        row_id=row_id,
                        column_id=column_id,
                        value=edit_value,
                    )
                ],
            )
            edit_ref = {
                "kind": "edit_overlay",
                "op_id": op_id,
                "row_id": row_id,
                "column_id": column_id,
                "value_hash": replay_generated_value_hash(edit_value),
            }
            if current_ref is not None:
                mark_evidence_stale_for_cell_refs(
                    self._project,
                    [
                        {
                            "row_id": row_id,
                            "column_id": column_id,
                            "current_value_ref": current_ref,
                        }
                    ],
                    reason=f"review_decision_{decision}",
                )
        result = ReviewDecision(
            run_id=run_id,
            row_id=row_id,
            column_id=column_id,
            decision=decision,
            review_state_before=state_before,
            review_state_after=state_after,
            note=note,
            op_id=op_id,
        )
        self.target = target
        self.edit_ref = edit_ref
        self.result = result
        return result


class _ReplayValueAcceptor(_CallOnce):
    def accept(
        self,
        *,
        sheet_id: int,
        row_id: int,
        column_id: int,
        run_id: int,
        generated_value_hash: str,
    ) -> AcceptedReplayValue:
        self._begin()
        _claimed_column(self._project, column_id, action_kind=self._action.kind)
        generated = _exact_replay_value(
            self._project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            expected_run_id=run_id,
            expected_hash=generated_value_hash,
            action_kind=self._action.kind,
        )
        entry = self._project.pending_replay_values(
            sheet_id, column_id, row_ids=[row_id]
        ).get(row_id)
        current_ref = None
        op_id = None
        if entry is not None:
            if (
                int(entry["run_id"]) != run_id
                or str(entry["generated_value_hash"]) != generated_value_hash
            ):
                _refuse(
                    "stale_replay",
                    "The pending regenerated value changed before acceptance.",
                    action_kind=self._action.kind,
                    field="params.generated_value_hash",
                )
            _values, refs = self._project.get_values_with_refs(
                sheet_id, column_id, row_ids=[row_id]
            )
            ref = refs.get(row_id)
            current_ref = dict(ref) if isinstance(ref, dict) else None
            spec = _op_spec(self._action, self._params_hash)
            spec.update(
                {
                    "count": 1,
                    "source_run_id": run_id,
                    "from_replay_accept": True,
                    "value_hash": generated_value_hash,
                }
            )
            op_id = _write_edit_overlay(
                self._project,
                self._cur,
                label="accept regenerated value",
                spec=spec,
                targets=[
                    {
                        "row_id": row_id,
                        "column_id": column_id,
                        "value_after": generated["fresh_value"],
                        **(
                            {"current_value_ref": current_ref}
                            if current_ref is not None
                            else {}
                        ),
                    }
                ],
                stale_reason="replay_accept",
            )
        pending_count = self._project.pending_replay_count(sheet_id, column_id)
        result = AcceptedReplayValue(
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            run_id=run_id,
            generated_value_hash=generated_value_hash,
            accepted=op_id is not None,
            pending_count=pending_count,
            op_id=op_id,
        )
        self.result = result
        return result


class _ReplayColumnAcceptor(_CallOnce):
    def accept_column(self, *, sheet_id: int, column_id: int) -> AcceptedReplayColumn:
        self._begin()
        run_id = _replay_origin_run(
            self._project,
            sheet_id=sheet_id,
            column_id=column_id,
            action_kind=self._action.kind,
        )
        _claimed_column(self._project, column_id, action_kind=self._action.kind)
        entries = self._project.pending_replay_values(sheet_id, column_id)
        row_ids = tuple(sorted(entries))
        op_id = None
        current_refs: dict[int, dict[str, Any]] = {}
        if row_ids:
            _values, refs = self._project.get_values_with_refs(
                sheet_id, column_id, row_ids=list(row_ids)
            )
            current_refs = {
                row_id: dict(ref)
                for row_id, ref in refs.items()
                if isinstance(ref, dict)
            }
            spec = _op_spec(self._action, self._params_hash)
            spec.update(
                {
                    "count": len(row_ids),
                    "source_run_id": run_id,
                    "from_replay_accept": True,
                }
            )
            op_id = _write_edit_overlay(
                self._project,
                self._cur,
                label="accept regenerated values (column)",
                spec=spec,
                targets=[
                    {
                        "row_id": row_id,
                        "column_id": column_id,
                        "value_after": entries[row_id]["fresh_value"],
                        **(
                            {"current_value_ref": current_refs[row_id]}
                            if row_id in current_refs
                            else {}
                        ),
                    }
                    for row_id in row_ids
                ],
                stale_reason="replay_accept_column",
            )
        pending_count = self._project.pending_replay_count(sheet_id, column_id)
        result = AcceptedReplayColumn(
            sheet_id=sheet_id,
            column_id=column_id,
            accepted=len(row_ids),
            pending_count=pending_count,
            op_id=op_id,
        )
        self.run_id = run_id
        self.row_ids = row_ids
        self.value_hashes = tuple(
            str(entries[row_id]["generated_value_hash"]) for row_id in row_ids
        )
        self.result = result
        return result


class _ReplayValueDismissor(_CallOnce):
    def dismiss(
        self,
        *,
        sheet_id: int,
        row_id: int,
        column_id: int,
        run_id: int,
        generated_value_hash: str,
    ) -> DismissedReplayValue:
        self._begin()
        _exact_replay_value(
            self._project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            expected_run_id=run_id,
            expected_hash=generated_value_hash,
            action_kind=self._action.kind,
        )
        existing = self._cur.execute(
            "SELECT 1 FROM replay_edit_dismissals "
            "WHERE row_id=? AND column_id=? AND generated_value_hash=?",
            (row_id, column_id, generated_value_hash),
        ).fetchone()
        if existing is None:
            pending = self._project.pending_replay_values(
                sheet_id, column_id, row_ids=[row_id]
            ).get(row_id)
            if pending is None:
                _refuse(
                    "invalid_replay_target",
                    "There is no matching pending human edit to dismiss.",
                    action_kind=self._action.kind,
                    field="params",
                    details={
                        "sheet_id": sheet_id,
                        "row_id": row_id,
                        "column_id": column_id,
                    },
                )
            if (
                int(pending["run_id"]) != run_id
                or str(pending["generated_value_hash"]) != generated_value_hash
            ):
                _refuse(
                    "stale_replay",
                    "The pending regenerated value changed before dismissal.",
                    action_kind=self._action.kind,
                    field="params.generated_value_hash",
                )
        self._cur.execute(
            "INSERT INTO replay_edit_dismissals "
            "(row_id,column_id,generated_value_hash,run_id) VALUES (?,?,?,?) "
            "ON CONFLICT(row_id,column_id,generated_value_hash) "
            "DO UPDATE SET run_id=excluded.run_id",
            (row_id, column_id, generated_value_hash, run_id),
        )
        pending_count = self._project.pending_replay_count(sheet_id, column_id)
        result = DismissedReplayValue(
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            run_id=run_id,
            generated_value_hash=generated_value_hash,
            pending_count=pending_count,
        )
        self.result = result
        return result


CAPABILITY_IMPL = {
    ReviewDecider: _ReviewDecider,
    ReplayValueAcceptor: _ReplayValueAcceptor,
    ReplayColumnAcceptor: _ReplayColumnAcceptor,
    ReplayValueDismissor: _ReplayValueDismissor,
}


def _review_result_and_receipt(
    returned: ReviewDecision,
    capability: _ReviewDecider,
    *,
    action: _TypedProjectEnvelope,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
) -> tuple[ActionResult, Receipt]:
    target = capability.target
    target_value = json.loads(target["value"]) if target["value"] is not None else None
    target_ref = {
        "kind": "target_result_cell",
        "run_id": returned.run_id,
        "row_id": returned.row_id,
        "column_id": returned.column_id,
        "sheet_id": int(target["sheet_id"]),
        "column_name": str(target["column_name"]),
        "value_hash": replay_generated_value_hash(target_value),
        "confidence": target["confidence"],
        "justification_hash": (
            _text_hash(str(target["justification"]))
            if target["justification"] is not None
            else None
        ),
    }
    review_ref = {
        "kind": "review_decision",
        "decision": returned.decision,
        "review_state_before": returned.review_state_before,
        "review_state_after": returned.review_state_after,
        "note": returned.note,
        "op_id": returned.op_id,
        "target": {
            "kind": "result_cell",
            "run_id": returned.run_id,
            "row_id": returned.row_id,
            "column_id": returned.column_id,
        },
    }
    state_ref = {
        "kind": "review_state_transition",
        "run_id": returned.run_id,
        "row_id": returned.row_id,
        "column_id": returned.column_id,
        "before": returned.review_state_before,
        "after": returned.review_state_after,
        "decision": returned.decision,
        "op_id": returned.op_id,
    }
    evidence = [
        ReceiptEvidence(ref=target_ref, retention="pinned"),
        ReceiptEvidence(ref=state_ref, retention="pinned"),
    ]
    outputs = [ReceiptIO(name="decision", ref=review_ref)]
    if capability.edit_ref is not None:
        evidence.append(ReceiptEvidence(ref=capability.edit_ref, retention="pinned"))
        outputs.append(ReceiptIO(name="edit_overlay", ref=capability.edit_ref))
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[returned.op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[ReceiptIO(name="target", ref=target_ref)],
        outputs=outputs,
        evidence=evidence,
        review={
            "decision": returned.decision,
            "review_state_before": returned.review_state_before,
            "review_state_after": returned.review_state_after,
            "note": returned.note,
        },
    )
    return _result_from_receipt(receipt), receipt


def _replay_result_and_receipt(
    returned: AcceptedReplayValue | AcceptedReplayColumn | DismissedReplayValue,
    capability: _CallOnce,
    *,
    action: _TypedProjectEnvelope,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
) -> tuple[ActionResult, Receipt]:
    if isinstance(returned, AcceptedReplayValue):
        op_ids = [returned.op_id] if returned.op_id is not None else []
        ref = {
            "kind": "replay_accept",
            "sheet_id": returned.sheet_id,
            "row_id": returned.row_id,
            "column_id": returned.column_id,
            "run_id": returned.run_id,
            "generated_value_hash": returned.generated_value_hash,
            "accepted": returned.accepted,
            "pending_count": returned.pending_count,
            "op_ids": op_ids,
        }
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "replay_generated_value",
                    "sheet_id": returned.sheet_id,
                    "row_id": returned.row_id,
                    "column_id": returned.column_id,
                    "run_id": returned.run_id,
                    "value_hash": returned.generated_value_hash,
                },
                retention="pinned",
            )
        ]
    elif isinstance(returned, AcceptedReplayColumn):
        assert isinstance(capability, _ReplayColumnAcceptor)
        op_ids = [returned.op_id] if returned.op_id is not None else []
        membership = [
            {"row_id": row_id, "value_hash": value_hash}
            for row_id, value_hash in zip(
                capability.row_ids, capability.value_hashes, strict=True
            )
        ]
        membership_hash = replay_generated_value_hash(membership)
        ref = {
            "kind": "replay_accept_column",
            "sheet_id": returned.sheet_id,
            "column_id": returned.column_id,
            "run_id": capability.run_id,
            "accepted": returned.accepted,
            "pending_count": returned.pending_count,
            "membership_hash": membership_hash,
            "op_ids": op_ids,
        }
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "replay_accept_column_membership",
                    "sheet_id": returned.sheet_id,
                    "column_id": returned.column_id,
                    "run_id": capability.run_id,
                    "accepted": returned.accepted,
                    "membership_hash": membership_hash,
                },
                retention="pinned",
            )
        ]
    else:
        op_ids = []
        ref = {
            "kind": "replay_dismiss",
            "sheet_id": returned.sheet_id,
            "row_id": returned.row_id,
            "column_id": returned.column_id,
            "run_id": returned.run_id,
            "generated_value_hash": returned.generated_value_hash,
            "pending_count": returned.pending_count,
        }
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "replay_dismissal",
                    "sheet_id": returned.sheet_id,
                    "row_id": returned.row_id,
                    "column_id": returned.column_id,
                    "run_id": returned.run_id,
                    "generated_value_hash": returned.generated_value_hash,
                },
                retention="pinned",
            )
        ]
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=op_ids,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[ReceiptIO(name="target", ref=evidence[0].ref)],
        outputs=[ReceiptIO(name=action.kind, ref=ref)],
        evidence=evidence,
    )
    return _result_from_receipt(receipt), receipt


def result_and_receipt(
    returned: (
        ReviewDecision
        | AcceptedReplayValue
        | AcceptedReplayColumn
        | DismissedReplayValue
    ),
    capability: _CallOnce,
    *,
    action: _TypedProjectEnvelope,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
) -> tuple[ActionResult, Receipt]:
    if isinstance(returned, ReviewDecision):
        result, receipt = _review_result_and_receipt(
            returned,
            capability,
            action=action,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
        )
    else:
        result, receipt = _replay_result_and_receipt(
            returned,
            capability,
            action=action,
            project_id=project_id,
            action_id=action_id,
            receipt_id=receipt_id,
            params_hash=params_hash,
        )
    return result, receipt

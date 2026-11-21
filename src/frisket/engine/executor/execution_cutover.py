"""One-time project retirement while admissions and all writers are stopped.

This is a maintenance command, never startup recovery or an online cancellation
API. The operator has authorized absorbing unfinished legacy spending. Existing
results, usage, completed work and project edits remain historical facts.
"""

from __future__ import annotations

from typing import Any

from frisket.contracts.action import ActionError
from frisket.engine.executor.project_run_terminalization import (
    AbandonedRunRecoveryAuthority,
    UnclaimedRunTerminalAuthority,
    terminalize_project_run,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import set_attempt_state


class ProjectCutoverRefused(RuntimeError):
    """A project needs explicit repair before the stopped-writer cutover resumes."""


def retire_legacy_project_work(project: Any) -> dict[str, int]:
    """Retire unfinished work atomically; the caller must have stopped writers.

    Repeating this command is safe before admissions restart. It deliberately
    does not distinguish old/new work: the global cutover marker belongs to the
    orchestrator, which must never run this sweep after new work is admitted.
    """
    db = project.db
    if db.in_transaction:
        raise ProjectCutoverRefused("project cutover requires its own transaction")
    counts = dict(runs=0, receipts=0, attempts=0, claims=0, checkpoints=0, turns=0)
    receipts = ReceiptStore(project)
    claims = OutputColumnClaimStore(project)
    runs = RunResultStore(project)
    db.execute("BEGIN IMMEDIATE")
    try:
        open_receipts = [
            receipts.require(row["id"])
            for row in db.execute(
                "SELECT id FROM receipts WHERE status IN ('queued','running')"
            ).fetchall()
        ]
        # Validate before changing anything; never repair only the status column
        # while leaving an unreadable or contradictory receipt document behind.
        for stored in open_receipts:
            parsed = stored.parsed()
            if (
                parsed.receipt_id != stored.id
                or parsed.run_id != stored.run_id
                or parsed.status != stored.status
            ):
                raise ProjectCutoverRefused(f"receipt {stored.id} identity mismatch")

        active_claims = db.execute(
            "SELECT * FROM output_column_claims WHERE status='active'"
        ).fetchall()
        run_ids = {
            int(row[0])
            for row in db.execute("SELECT id FROM runs WHERE status='running'")
        }
        run_ids.update(r.run_id for r in open_receipts if r.run_id is not None)
        run_ids.update(
            int(c["run_id"]) for c in active_claims if c["run_id"] is not None
        )
        attempts = db.execute(
            "SELECT id,run_id FROM execution_attempts "
            "WHERE state IN ('created','admitted','dispatching')"
        ).fetchall()
        run_ids.update(int(a["run_id"]) for a in attempts if a["run_id"] is not None)

        # Same operator-absorption decision as EffectCheckpointStore.operator_discard,
        # joined to this transaction. Never discard a returned result/audit payload.
        counts["checkpoints"] = db.execute(
            "DELETE FROM effect_checkpoints WHERE state='reserved'"
        ).rowcount
        for attempt in attempts:
            set_attempt_state(project, attempt["id"], "abandoned", commit=False)
        counts["attempts"] = len(attempts)
        # Repair stale writer pointers too. set_attempt_state leaves already
        # terminal attempt history unchanged while clearing its run pointer.
        for row in db.execute(
            "SELECT current_attempt_id FROM runs WHERE current_attempt_id IS NOT NULL"
        ).fetchall():
            set_attempt_state(project, row[0], "abandoned", commit=False)

        for run_id in sorted(run_ids):
            run = runs.get_run(run_id)
            if run is None:
                raise ProjectCutoverRefused(
                    f"unfinished work references missing run {run_id}"
                )
            owned_claims = [c for c in active_claims if c["run_id"] == run_id]
            claim_receipts = {c["receipt_id"] for c in owned_claims}
            if len(claim_receipts) > 1:
                raise ProjectCutoverRefused(f"run {run_id} has multiple claim owners")
            stored = (
                receipts.find_by_id(next(iter(claim_receipts)))
                if claim_receipts and None not in claim_receipts
                else receipts.latest_for_run_action_statuses(
                    run_id, run["action_kind"], ("queued", "running")
                )
            )
            target = "cancelled" if run["status"] == "running" else run["status"]
            if target not in {"completed", "failed", "cancelled"}:
                raise ProjectCutoverRefused(
                    f"run {run_id} has unsupported status {target}"
                )
            generations = ResultGenerationStore(project)
            bindings = generations.bindings_for_run(run_id)
            open_states = {b.state for b in bindings if b.state != "sealed"}
            if "staged" in open_states:
                # A cutover must not promote an unfinished replacement into the
                # dataset. Keep its saved results, and abort staged publication
                # before the ordinary terminalizer sees sealed generations.
                if open_states != {"staged"}:
                    raise ProjectCutoverRefused(
                        f"run {run_id} mixes staged and published output generations"
                    )
                tokens = {c["claim_token"] for c in owned_claims}
                if len(tokens) != 1:
                    raise ProjectCutoverRefused(
                        f"run {run_id} staged outputs lack one exact claim"
                    )
                generations.seal(
                    run_id,
                    [b.column_id for b in bindings],
                    claim_token=tokens.pop(),
                    terminal_disposition=target,
                    allow_stale_output_name=True,
                    publish=False,
                    commit=False,
                )
                # A wholly staged family never became the visible replacement.
                # Retire its Change through the ordinary reversible history
                # transition so canonical readers fall back to the prior
                # published heads. Keep the operation, run, generations, and
                # result facts for audit; this maintenance-only retirement is
                # not a user redo candidate.
                from frisket.engine.store.changes import ChangeStore

                ChangeStore(project).revert(int(run["op_id"]))
                db.execute(
                    "UPDATE ops SET redo_eligible=0 WHERE id=?",
                    (int(run["op_id"]),),
                )
            if stored is None:
                # Direct local work may have no receipt or output-generation owner.
                if owned_claims or any(b.state != "sealed" for b in bindings):
                    raise ProjectCutoverRefused(
                        f"run {run_id} has outputs without a receipt"
                    )
                if run["status"] == "running":
                    runs.finish_run(
                        run_id, "cancelled", commit=False, rebuild_review_stats=False
                    )
            else:
                abandoned = db.execute(
                    "SELECT 1 FROM execution_attempts WHERE run_id=? AND state='abandoned'",
                    (run_id,),
                ).fetchone()
                authority_type = (
                    AbandonedRunRecoveryAuthority
                    if abandoned
                    else UnclaimedRunTerminalAuthority
                )
                outcome = terminalize_project_run(
                    project,
                    run_id=run_id,
                    receipt_id=stored.id,
                    status="cancelled"
                    if stored.status in {"queued", "running"}
                    else stored.status,
                    run_status=target,
                    authority=authority_type(
                        claimless_direct_effect=not owned_claims,
                        require_cancel_intent=False,
                    ),
                    errors=[_cutover_error(stored.action_kind)],
                    commit=False,
                )
                if outcome.disposition not in {"terminalized", "already_terminal"}:
                    raise ProjectCutoverRefused(
                        f"run {run_id}: {outcome.disposition}: {outcome.reason}"
                    )
            counts["runs"] += int(run["status"] == "running")

        # Includes runless actions and auxiliary receipts attached to a run.
        for stored in open_receipts:
            current = receipts.require(stored.id)
            if current.status in {"queued", "running"}:
                cancelled = current.parsed().model_copy(
                    update={
                        "status": "cancelled",
                        "errors": [_cutover_error(current.action_kind)],
                    }
                )
                if not receipts.update_if_status_in(cancelled, ("queued", "running")):
                    raise ProjectCutoverRefused(
                        f"receipt {stored.id} refused cancellation"
                    )
                claims.release_for_receipt(receipt_id=stored.id, status="cancelled")
        counts["receipts"] = len(open_receipts)
        if db.execute(
            "SELECT 1 FROM output_column_claims WHERE status='active'"
        ).fetchone():
            raise ProjectCutoverRefused("unowned active output claims remain")
        counts["claims"] = len(active_claims)
        counts["turns"] = len(
            ProjectQAStore(project).reconcile_abandoned_turns(live_turn_ids=())
        )
        db.commit()
        return counts
    except BaseException:
        db.rollback()
        raise


def _cutover_error(action_kind: str) -> ActionError:
    return ActionError(
        code="execution_cutover",
        message="Unfinished work was stopped for an execution upgrade. Run the action again to continue.",
        action_kind=action_kind,
    )

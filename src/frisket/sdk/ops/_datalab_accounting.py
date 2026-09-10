"""Shared acceptance-boundary accounting for Datalab-backed recipes."""

from __future__ import annotations

import copy
from typing import Any

from frisket.execution.attempt import attempt_in_scope, routed_admission_in_scope
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.ops.base import OpContext
from frisket.engine.store.runs import RunResultStore


def persist_datalab_accepted_accounting(
    ctx: OpContext,
    accounting: dict[str, Any],
) -> None:
    """Persist the provider's accepted job before its first poll.

    Direct client/recipe calls have no run ledger; MapRunner contexts always
    carry the run and row ids. A ROUTED capability additionally owns a routed
    execution epoch, so bind the acceptance fact at the same observation seam
    as the completed fact (``bind_fact_to_route`` is capability-neutral —
    every field it derives comes from the route row, not the fact's own
    capability). Gated on ``RunResultStore._ROUTED_FACT_CAPABILITIES`` — the
    same ground truth the epoch invariant checks at write time — rather than
    a single hardcoded capability, so a second Datalab-backed capability
    (document.convert, alongside OCR) binds here too instead of writing a
    silent unbound fact under a routed run. The writer receives a deep copy
    because it consumes the transient route-observation payload; the live
    envelope must retain it for the later completed/error row write of the
    same fact id.
    """
    run_id = (ctx.extras or {}).get("run_id")
    row_id = (ctx.extras or {}).get("row_id")
    attempt = attempt_in_scope(ctx.extras)
    receipt_id = attempt.receipt_id if attempt is not None else None
    if (type(run_id) is not int and receipt_id is None) or type(row_id) is not int:
        return
    calls = accounting.get("model_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise RuntimeError("Datalab acceptance accounting requires one provider fact")

    admission = routed_admission_in_scope(ctx.extras)
    if admission is not None and (
        calls[0].get("capability") in RunResultStore._ROUTED_FACT_CAPABILITIES
    ):
        calls[0] = bind_fact_to_route(admission.route, calls[0])

    RunResultStore(ctx.project).write_returned_call_accounting(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": None,
                "model_calls": copy.deepcopy(calls),
            }
        ],
        writer_attempt_id=attempt.attempt_id if attempt is not None else None,
        claim_token=(ctx.extras or {}).get("claim_token"),
        authorized_attempt_id=attempt.attempt_id if attempt is not None else None,
        receipt_id=receipt_id,
    )

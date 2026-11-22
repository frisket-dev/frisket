"""Shared acceptance-boundary accounting for remote document providers."""

from __future__ import annotations

import copy
from typing import Any

from frisket.execution.attempt import attempt_in_scope, routed_admission_in_scope
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.ops.base import ActionContext
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.writer_fence import WriterFence


def persist_remote_accepted_accounting(
    ctx: ActionContext,
    accounting: dict[str, Any],
) -> None:
    """Persist the provider's accepted job before its first poll.

    Direct calls may have no run ledger. Routed calls bind the fact to their
    execution epoch before writing it. The writer consumes transient route
    observations, so pass a deep copy and retain the original envelope for the
    later returned-row write that attaches its final charge and generated column.
    """
    run_id = (ctx.extras or {}).get("run_id")
    row_id = (ctx.extras or {}).get("row_id")
    attempt = attempt_in_scope(ctx.extras)
    receipt_id = attempt.receipt_id if attempt is not None else None
    if (type(run_id) is not int and receipt_id is None) or type(row_id) is not int:
        return
    calls = accounting.get("model_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise RuntimeError("Remote acceptance accounting requires one provider fact")

    admission = routed_admission_in_scope(ctx.extras)
    if admission is not None and (
        calls[0].get("capability") in RunResultStore._ROUTED_FACT_CAPABILITIES
    ):
        calls[0] = bind_fact_to_route(admission.route, calls[0])

    attempt_id = attempt.attempt_id if attempt is not None else None
    RunResultStore(ctx.project).write_returned_call_accounting(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": None,
                "model_calls": copy.deepcopy(calls),
            }
        ],
        fence=WriterFence(
            run_id=run_id,
            writer_attempt_id=attempt_id,
            claim_token=(ctx.extras or {}).get("claim_token"),
            authorized_attempt_id=attempt_id,
        ),
        receipt_id=receipt_id,
    )

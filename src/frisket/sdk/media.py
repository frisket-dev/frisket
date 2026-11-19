"""Receipt and replay helpers shared by the media actions.

`media_output_column_value_hash` is deliberately NOT `sdk.replay.
output_column_value_hash`: media receipts at rest carry this hash shape, so
replaying them must keep computing it byte-identically.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.contracts.action import ActionError, ActionSpec, Receipt
from frisket.engine.store import Project
from frisket.engine.store.runs import FAILURE_OUTCOMES, outcome_sql_list


def media_text_hash(text: str) -> str:
    """Stable ``sha256:`` digest of a text value (source URLs, cell text)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def media_halt_error(action: ActionSpec | str, run: Any) -> ActionError | None:
    """The typed receipt error for a run the PROGRAM halted, or ``None`` when
    the run carries no halt marker (an operator cancellation).

    One construction, shared by every routed media action. A terminal
    generation is immutable, and a halt before any cell publication has no
    exact source head from which ``run.backfill`` can reconstruct an action.
    The receipt therefore reports the halt honestly without advertising the
    deleted in-place recovery contract.
    """
    from frisket.ops.base import persisted_action_program_invocation_halt

    halt = persisted_action_program_invocation_halt(run["params"])
    if halt is None:
        return None
    code, detail = halt
    return ActionError(
        code=code,
        message=detail,
        action_kind=action if isinstance(action, str) else action.kind,
        details={
            "run_id": int(run["id"]),
            "completed_rows": int(run["completed_rows"] or 0),
            "total_rows": int(run["total_rows"] or 0),
        },
    )


def media_successful_result_row_ids(
    project: Project,
    *,
    run_id: int,
    column_id: int,
    row_ids: list[int],
) -> list[int]:
    """Row ids that produced a non-error, non-null result for a column."""
    if not row_ids:
        return []
    placeholders = ",".join("?" for _ in row_ids)
    successful = {
        int(row["row_id"])
        for row in project.db.execute(
            "SELECT row_id FROM results "
            f"WHERE run_id=? AND column_id=? AND row_id IN ({placeholders}) "
            f"AND outcome NOT IN ({outcome_sql_list(FAILURE_OUTCOMES)}) "
            "AND value IS NOT NULL",
            [run_id, column_id, *row_ids],
        ).fetchall()
    }
    return [row_id for row_id in row_ids if row_id in successful]


def media_output_column_value_hash(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
) -> str:
    """Deterministic value hash of a generated column's cells for replay checks."""
    values = project.get_values(sheet_id, column_id, row_ids=row_ids)
    payload = {str(row_id): values.get(row_id) for row_id in row_ids}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def media_replay_row_ids(
    ref: dict[str, Any],
    *,
    receipt: Receipt,
    output_name: str,
) -> list[int] | ActionError:
    """Validate a receipt output ref's stored row ids for value-hash replay."""
    row_ids = ref.get("row_ids")
    if (
        not isinstance(row_ids, list)
        or not row_ids
        or any(
            not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0
            for row_id in row_ids
        )
        or len(set(row_ids)) != len(row_ids)
    ):
        return ActionError(
            code="stale_replay",
            message=f"{receipt.action_kind} replay receipt has invalid row refs",
            action_kind=receipt.action_kind,
            details={"receipt_id": receipt.receipt_id, "output": output_name},
        )
    return row_ids

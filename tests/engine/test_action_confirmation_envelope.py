"""One confirmation wire contract: deterministic guards emit the SAME HTTP 402
``needs_confirmation`` envelope the model-cost gate uses.

Deterministic guards and model-cost guards both use HTTP 402 so callers have
one confirmation protocol.

Before this slice the model-cost gate rode HTTP 402 ``needs_confirmation`` while
the deterministic child-sheet body returned ``status=failed`` (HTTP 400) for a
``resolve_fn`` ActionError, so ``derive.join``'s fan-out guard surfaced as a
plain 400 with a bespoke frontend branch. The unification: a GENERIC
``needs_confirmation`` marker on ActionError (not a code allowlist) makes the
deterministic-write body emit the same 402 envelope; the cost-gate details
(estimated_rows/max_output_rows/top_fanout_keys) survive on the envelope body;
non-confirmation ActionErrors still fail 400.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from frisket.contracts.actions.schemas._base import ActionError
from frisket.engine.executor import actions as executor_actions
from frisket.engine.executor.action_support import _failed_result
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.engine.store import Project

PROJECT_ID = "project-confirmation-envelope"
_KEYGEN = itertools.count(1)


def _seed_fanout(project_path: Path) -> dict[str, Any]:
    project = Project.create(project_path, name="Fanout")
    try:
        left_id = project.add_sheet("Left")
        left_cols = {"k": project.add_column(left_id, "k", type="integer")}
        # key 6 appears twice, key 1 once
        project.add_rows(left_id, [{"k": 6}, {"k": 6}, {"k": 1}], left_cols)
        right_id = project.add_sheet("Right")
        right_cols = {"k": project.add_column(right_id, "k", type="integer")}
        # key 6 appears 3x, key 1 twice -> inner fan-out == 2*3 + 1*2 = 8
        project.add_rows(
            right_id, [{"k": 6}, {"k": 6}, {"k": 6}, {"k": 1}, {"k": 1}], right_cols
        )
        return {"left_sheet_id": left_id, "right_sheet_id": right_id}
    finally:
        project.close()


def _join_action(
    *,
    left_sheet_id: int,
    right_sheet_id: int,
    target_sheet_name: str,
    idempotency_key: str | None = None,
    confirmation: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if idempotency_key is None:
        idempotency_key = f"confirm_envelope@sha256:{next(_KEYGEN)}"
    params: dict[str, Any] = {
        "right": {"sheet_id": right_sheet_id},
        "join_keys": [{"left_column": "k", "right_column": "k"}],
        "how": "inner",
    }
    params.update(extra)
    return {
        "action_id": "derive.join",
        "scope": {"kind": "sheet_rows", "sheet_id": left_sheet_id},
        "sheet_name": target_sheet_name,
        "confirmation": confirmation,
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _run(project: Project, action: dict[str, Any]):
    return executor_actions.run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
    )


# ---------------------------------------------------------------------------
# The generic marker: any ActionError may carry needs_confirmation semantics;
# it is a marker on the error, NOT a code allowlist in the status mapper.
# ---------------------------------------------------------------------------


def test_action_error_carries_generic_needs_confirmation_marker() -> None:
    marked = ActionError(
        code="anything_at_all",
        message="please confirm",
        needs_confirmation=True,
    )
    assert marked.needs_confirmation is True
    # Default is off: a plain validation error is not a confirmation gate.
    plain = ActionError(code="invalid_params", message="bad")
    assert plain.needs_confirmation is False


def test_failed_result_marker_maps_deterministic_write_onto_needs_confirmation() -> (
    None
):
    """A marked resolve_fn error becomes a needs_confirmation result (HTTP 402),
    an unmarked one stays a failed result (HTTP 400) — same helper, one seam."""
    marked = _failed_result(
        project_id=PROJECT_ID,
        action_kind="derive.join",
        error=ActionError(
            code="join_fanout_requires_confirmation",
            message="confirm",
            needs_confirmation=True,
        ),
    )
    assert marked.status == "needs_confirmation"
    assert marked.receipt_id is None
    assert v1_action_result_http_status(marked) == 402

    unmarked = _failed_result(
        project_id=PROJECT_ID,
        action_kind="derive.join",
        error=ActionError(code="invalid_params", message="bad"),
    )
    assert unmarked.status == "failed"
    assert unmarked.receipt_id is None
    assert v1_action_result_http_status(unmarked) == 400


# ---------------------------------------------------------------------------
# derive.join fan-out through the real executor: the deterministic-write body
# emits the unified 402 envelope, body details preserved, confirm completes.
# ---------------------------------------------------------------------------


def test_derive_join_fanout_emits_unified_402_envelope(tmp_path: Path) -> None:
    seed = _seed_fanout(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        gated = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                target_sheet_name="Guarded",
                max_output_rows=5,
            ),
        )
        # UNIFIED: needs_confirmation (was "failed"/400 before this slice).
        assert gated.status == "needs_confirmation", gated.status
        assert v1_action_result_http_status(gated) == 402
        err = gated.errors[0]
        assert err.needs_confirmation is True
        assert err.code == "join_fanout_requires_confirmation"
        assert err.field == "confirmation"
        # Envelope body preserves the cost-gate detail shape.
        assert err.details.get("estimated_rows") == 8
        assert err.details.get("max_output_rows") == 5
        assert err.details.get("top_fanout_keys")
        promise_set_hash = err.details.get("promise_set_hash")
        assert isinstance(promise_set_hash, str)
        assert len(promise_set_hash) == 64
        # Nothing materialized on the gate.
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Guarded'"
            ).fetchone()[0]
            == 0
        )

        bare_confirmed = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                target_sheet_name="Guarded",
                max_output_rows=5,
                confirmation="wrong-token",
            ),
        )
        assert bare_confirmed.status == "needs_confirmation"

        confirmed = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                target_sheet_name="Guarded",
                max_output_rows=5,
                confirmation=promise_set_hash,
            ),
        )
        assert confirmed.status == "completed", confirmed.errors
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Guarded' AND hidden=0"
            ).fetchone()[0]
            == 1
        )
    finally:
        project.close()


def test_plain_validation_error_still_400(tmp_path: Path) -> None:
    """A non-confirmation ActionError must NOT start returning 402: unify does
    not mean every validation failure becomes a confirmation gate."""
    seed = _seed_fanout(tmp_path / "p.frisket")
    project = Project(tmp_path / "p.frisket")
    try:
        # Self-join on the same sheet id is an invalid input ref (validation).
        result = _run(
            project,
            _join_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["left_sheet_id"],
                target_sheet_name="Invalid",
            ),
        )
        assert result.status == "failed", result.status
        assert v1_action_result_http_status(result) == 400
        assert result.errors[0].needs_confirmation is False
    finally:
        project.close()

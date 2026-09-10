"""Seam tests for the unified replay checker.

`output_columns_replay_error`'s `scope` keyword replaces the old light/strict
split (`output_columns_replay_error_light` is gone): "expected" scope (today's
strict behavior) checks the receipt's row set against a caller-computed
expected row set; "receipt" scope (new) trusts the receipt ref's OWN recorded
`row_ids` instead, so a partial run's replay is not penalized for rows outside
its scope. Both scopes keep every column-identity check and the
REPLAY_EDIT_POLICY == "surface" edit-forgiveness; a ref with no `value_hash`
(a receipt written before receipts always carried one) skips only the value
comparison.

Modeled on tests/test_replay_accept_surface.py's fixture style: direct checker
calls over hand-built receipts, no action-execution plumbing.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from frisket.contracts.action import Receipt, ReceiptIO
from frisket.engine.runner.publication import PUBLISH_VALUE
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.sdk.replay import output_column_value_hash, output_columns_replay_error
from frisket.sdk.replay_policy import REPLAY_EDIT_POLICY
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from helpers import run_writer_authority_fixture

_OUTPUT_KIND = "test_map_output"


class _Fixture:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.sheet = project.add_sheet("stories")
        self.src = project.add_column(self.sheet, "story")
        self.col = project.add_column(
            self.sheet, "label", type="text", ai_generated=True
        )
        rows = [{"story": f"story {i}"} for i in range(6)]
        project.add_rows(self.sheet, rows, {"story": self.src})
        self.row_ids = project.visible_row_ids(self.sheet)

    def run_with_results(self, values: dict[int, Any]) -> int:
        """Create a run that generated `values` per row and point the column
        at it. Passing a strict subset of `self.row_ids` models a partial run:
        the sheet has more rows than this run produced results for."""
        store = RunResultStore(self.project)
        op_id = self.project.append_op("run", {"kind": "map"}, label="generate labels")
        run_id = store.start_run(
            op_id,
            self.sheet,
            "map.classify",
            model="anthropic/claude-haiku-4-5",
            row_ids=list(values),
        )
        claim_token = f"output-claim:test-replay:{run_id}"
        claims, conflict = OutputColumnClaimStore(self.project).acquire(
            sheet_id=self.sheet,
            output_names=["label"],
            action_kind="map.classify",
            run_id=run_id,
            op_id=op_id,
            claim_token=claim_token,
            lease_seconds=6 * 60 * 60,
        )
        assert conflict is None and len(claims) == 1
        OutputColumnClaimStore(self.project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
            expected_output_names=["label"],
        )
        generations = ResultGenerationStore(self.project)
        field = {"name": "label", "column_type": "text"}
        generations.declare(
            run_id,
            self.col,
            output_role="label",
            compatibility_key=_compatibility_key(field=field),
            write_mode=(
                "replace_scope"
                if generations.is_generation_managed(self.col)
                else "create"
            ),
            claim_token=claim_token,
        )
        authority = run_writer_authority_fixture(
            self.project, run_id, output_column_ids={self.col}
        )
        store.write_results(
            run_id,
            [
                {
                    "row_id": rid,
                    "column_id": self.col,
                    "value": val,
                    "outcome": "ok",
                    "publication_effect": PUBLISH_VALUE,
                }
                for rid, val in values.items()
            ],
            **authority.kwargs(),
        )
        generations.seal(
            run_id,
            [self.col],
            claim_token=claim_token,
            terminal_disposition="completed",
        )
        store.finish_run(run_id)
        OutputColumnClaimStore(self.project).finish_current_writer(
            run_id=run_id,
            writer_attempt_id=authority.writer_attempt_id,
            claim_token=claim_token,
            attempt_state="effected",
        )
        return run_id

    def edit(self, row_id: int, value: Any) -> int:
        return self.project.apply_edits(
            [{"row_id": row_id, "column_id": self.col, "value": value}],
            label="hand-fix label",
        )

    def receipt(
        self,
        *,
        run_id: int,
        ref_row_ids: list[int],
        value_hash: str | None,
    ) -> Receipt:
        ref: dict[str, Any] = {
            "kind": _OUTPUT_KIND,
            "column_id": self.col,
            "sheet_id": self.sheet,
            "name": "label",
            "type": "text",
            "run_id": run_id,
            "row_ids": list(ref_row_ids),
        }
        if value_hash is not None:
            ref["value_hash"] = value_hash
        return Receipt(
            receipt_id="rcpt-1",
            project_id="proj-1",
            action_id="act-1",
            action_kind="map.classify",
            run_id=run_id,
            op_ids=[],
            status="completed",
            outputs=[ReceiptIO(name="label", kind=_OUTPUT_KIND, ref=ref)],
        )


@pytest.fixture
def fx(tmp_path):
    project = Project.create(tmp_path / "p.frisket")
    try:
        yield _Fixture(project)
    finally:
        project.close()


def _require_surface_policy() -> None:
    assert REPLAY_EDIT_POLICY == "surface", (
        "these seam tests assume the decided uniform replay-edit policy "
        "('surface': preserve the edit, surface the fresh value)"
    )


# ---------------------------------------------------------------------------
# (a) receipt-scoped: partial run passes replay on its own recorded rows
# ---------------------------------------------------------------------------
def test_receipt_scope_passes_partial_run_on_its_own_recorded_rows(fx):
    # A partial run only produced results for the first three of the sheet's
    # six rows (rows 4-6 have none). Under scope="receipt" the checker must
    # not penalize the receipt for rows outside its own recorded scope.
    partial_row_ids = fx.row_ids[:3]
    values = {rid: f"gen-{i}" for i, rid in enumerate(partial_row_ids)}
    run_id = fx.run_with_results(values)
    value_hash = output_column_value_hash(
        fx.project, sheet_id=fx.sheet, column_id=fx.col, row_ids=partial_row_ids
    )
    receipt = fx.receipt(
        run_id=run_id, ref_row_ids=partial_row_ids, value_hash=value_hash
    )

    err = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="receipt",
    )
    assert err is None, (
        "receipt scope must pass a partial run's replay even though rows "
        "4-6 have no results at all"
    )


def test_receipt_scope_chunks_large_row_membership(fx):
    extra = fx.project.add_rows(
        fx.sheet,
        [{"story": f"extra {index}"} for index in range(895)],
        {"story": fx.src},
    )
    fx.row_ids.extend(extra)
    run_id = fx.run_with_results({row_id: "value" for row_id in fx.row_ids})
    receipt = fx.receipt(
        run_id=run_id,
        ref_row_ids=fx.row_ids,
        value_hash=None,
    )
    fx.project.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 902)

    error = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="receipt",
    )

    assert error is None


def test_expected_scope_rejects_the_same_partial_run(fx):
    # The same partial-run receipt fails under scope="expected" when the
    # caller's expected row set is the full (current) sheet scope — this is
    # exactly the row-scope check receipt-scope exists to sidestep for
    # partial-tolerant ops.
    partial_row_ids = fx.row_ids[:3]
    values = {rid: f"gen-{i}" for i, rid in enumerate(partial_row_ids)}
    run_id = fx.run_with_results(values)
    value_hash = output_column_value_hash(
        fx.project, sheet_id=fx.sheet, column_id=fx.col, row_ids=partial_row_ids
    )
    receipt = fx.receipt(
        run_id=run_id, ref_row_ids=partial_row_ids, value_hash=value_hash
    )

    err = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="expected",
        expected_row_ids=list(fx.row_ids),
    )
    assert err is not None and err.code == "stale_replay"


# ---------------------------------------------------------------------------
# (b) published result bytes are immutable; a pure human edit is forgiven via
#     the surface policy
# ---------------------------------------------------------------------------
def test_receipt_scope_published_value_cannot_mutate(fx):
    partial_row_ids = fx.row_ids[:3]
    values = {rid: f"gen-{i}" for i, rid in enumerate(partial_row_ids)}
    run_id = fx.run_with_results(values)
    value_hash = output_column_value_hash(
        fx.project, sheet_id=fx.sheet, column_id=fx.col, row_ids=partial_row_ids
    )
    receipt = fx.receipt(
        run_id=run_id, ref_row_ids=partial_row_ids, value_hash=value_hash
    )

    with pytest.raises(sqlite3.IntegrityError, match="semantics are immutable"):
        fx.project.db.execute(
            "UPDATE results SET value=? WHERE run_id=? AND row_id=? AND column_id=?",
            ('"mutated"', run_id, partial_row_ids[0], fx.col),
        )

    err = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="receipt",
        error_code="model_run_failed",
    )
    assert err is None


def test_receipt_scope_forgives_a_pure_human_edit(fx):
    _require_surface_policy()
    partial_row_ids = fx.row_ids[:3]
    values = {rid: f"gen-{i}" for i, rid in enumerate(partial_row_ids)}
    run_id = fx.run_with_results(values)
    value_hash = output_column_value_hash(
        fx.project, sheet_id=fx.sheet, column_id=fx.col, row_ids=partial_row_ids
    )
    receipt = fx.receipt(
        run_id=run_id, ref_row_ids=partial_row_ids, value_hash=value_hash
    )

    # A human edit changes the LIVE (overlay) value but not the underlying
    # run result -- REPLAY_EDIT_POLICY == "surface" forgives this.
    fx.edit(partial_row_ids[0], "hand-fixed")

    err = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="receipt",
        error_code="model_run_failed",
    )
    assert err is None, (
        "a pure human edit over recorded rows must be forgiven under "
        "REPLAY_EDIT_POLICY == 'surface', receipt scope included"
    )


# ---------------------------------------------------------------------------
# (c) a hashless ref still runs identity checks under both scopes
# ---------------------------------------------------------------------------
def test_hashless_ref_still_fails_on_renamed_column_both_scopes(fx):
    partial_row_ids = fx.row_ids[:3]
    values = {rid: f"gen-{i}" for i, rid in enumerate(partial_row_ids)}
    run_id = fx.run_with_results(values)
    receipt = fx.receipt(run_id=run_id, ref_row_ids=partial_row_ids, value_hash=None)

    fx.project.db.execute("UPDATE columns SET name='renamed' WHERE id=?", (fx.col,))
    fx.project.db.commit()

    err_receipt_scope = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="receipt",
    )
    assert err_receipt_scope is not None and err_receipt_scope.code == "stale_replay"

    err_expected_scope = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="expected",
        expected_row_ids=partial_row_ids,
    )
    assert err_expected_scope is not None and err_expected_scope.code == "stale_replay"


def test_hashless_ref_still_fails_on_hidden_column_both_scopes(fx):
    partial_row_ids = fx.row_ids[:3]
    values = {rid: f"gen-{i}" for i, rid in enumerate(partial_row_ids)}
    run_id = fx.run_with_results(values)
    receipt = fx.receipt(run_id=run_id, ref_row_ids=partial_row_ids, value_hash=None)

    fx.project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (fx.col,))
    fx.project.db.commit()

    err_receipt_scope = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="receipt",
    )
    assert err_receipt_scope is not None and err_receipt_scope.code == "stale_replay"

    err_expected_scope = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        scope="expected",
        expected_row_ids=partial_row_ids,
    )
    assert err_expected_scope is not None and err_expected_scope.code == "stale_replay"


# ---------------------------------------------------------------------------
# (d) expected-scope unchanged: a full smoke check that the default scope
#     ("expected") still behaves exactly as before this lane. The full pinned
#     behavior lives in tests/test_replay_accept_surface.py, kept untouched.
# ---------------------------------------------------------------------------
def test_expected_scope_is_still_the_default(fx):
    run_id = fx.run_with_results({rid: f"gen-{i}" for i, rid in enumerate(fx.row_ids)})
    value_hash = output_column_value_hash(
        fx.project, sheet_id=fx.sheet, column_id=fx.col, row_ids=list(fx.row_ids)
    )
    receipt = fx.receipt(run_id=run_id, ref_row_ids=fx.row_ids, value_hash=value_hash)

    # No `scope=` kwarg passed -- must behave exactly like scope="expected".
    err = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        expected_row_ids=list(fx.row_ids),
    )
    assert err is None

    err_default_code = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind=_OUTPUT_KIND,
        action_kind="map.classify",
        expected_row_ids=fx.row_ids[:-1],  # scope mismatch
    )
    assert err_default_code is not None and err_default_code.code == "stale_replay"

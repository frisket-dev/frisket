from __future__ import annotations

import hashlib
import importlib
import json
from importlib.util import find_spec
from typing import Any

import pytest

from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results

_REPLAY_MODULE = "frisket.sdk.replay"
_POLICY_MODULE = "frisket.sdk.replay_policy"


# --------------------------------------------------------------------------
# tonight-standard probes: presence checks that red as clean AssertionErrors
# --------------------------------------------------------------------------
def _module_present(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def _require_policy_is_surface() -> None:
    assert _module_present(_POLICY_MODULE), (
        f"missing {_POLICY_MODULE}: the uniform REPLAY_EDIT_POLICY constant is "
        "not implemented yet (op-sdk-replay-edit-policy-v1)"
    )
    policy = importlib.import_module(_POLICY_MODULE)
    assert getattr(policy, "REPLAY_EDIT_POLICY", None) == "surface", (
        "the decided uniform replay-edit policy must be 'surface' (preserve the "
        "edit, surface the fresh value)"
    )


def _require_replay_export(name: str) -> Any:
    module = importlib.import_module(_REPLAY_MODULE)
    assert hasattr(module, name), (
        f"{_REPLAY_MODULE} lacks {name}(...): the raw-result value hash / "
        "edit-attributable replay seam is not implemented yet"
    )
    return getattr(module, name)


def _table_exists(project: Project, table: str) -> bool:
    row = project.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _value_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# fixture: an ai_generated column with a current run + a human edit overlay
# --------------------------------------------------------------------------
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
        at it — the "fresh generated value" home."""
        store = RunResultStore(self.project)
        op_id = self.project.append_op("run", {"kind": "map"}, label="generate labels")
        run_id = store.start_run(
            op_id,
            self.sheet,
            "map.classify",
            model="anthropic/claude-haiku-4-5",
            row_ids=list(values),
        )
        write_claimed_test_results(
            self.project,
            run_id,
            [
                {"row_id": rid, "column_id": self.col, "value": val, "outcome": "ok"}
                for rid, val in values.items()
            ],
        )
        self.project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (run_id, self.col)
        )
        self.project.db.commit()
        return run_id

    def edit(self, row_id: int, value: Any) -> int:
        return self.project.apply_edits(
            [{"row_id": row_id, "column_id": self.col, "value": value}],
            label="hand-fix label",
        )


@pytest.fixture
def fx(tmp_path):
    project = Project.create(tmp_path / "p.frisket")
    try:
        yield _Fixture(project)
    finally:
        project.close()


# --------------------------------------------------------------------------
# 1. pending-set derivation
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 2. full-column-accurate aggregate count (not page-derived)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 3. durable dismissal table (value-identity keyed, project-global)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 4. replay parity + stale_replay behavior change
# --------------------------------------------------------------------------
def _minimal_receipt(fx, run_id: int, value_hash: str):
    from frisket.contracts.action import Receipt, ReceiptIO

    return Receipt(
        receipt_id="rcpt-1",
        project_id="proj-1",
        action_id="act-1",
        action_kind="map.classify",
        run_id=run_id,
        op_ids=[],
        status="completed",
        outputs=[
            ReceiptIO(
                name="label",
                kind="test_map_output",
                ref={
                    "kind": "test_map_output",
                    "column_id": fx.col,
                    "sheet_id": fx.sheet,
                    "name": "label",
                    "type": "text",
                    "run_id": run_id,
                    "row_ids": list(fx.row_ids),
                    "value_hash": value_hash,
                },
            )
        ],
    )


def test_edited_cell_no_longer_hard_errors_but_structural_staleness_does(fx):
    _require_policy_is_surface()
    from frisket.sdk.replay import (
        output_column_value_hash,
        output_columns_replay_error,
    )

    fresh = {rid: f"gen-{i}" for i, rid in enumerate(fx.row_ids)}
    run_id = fx.run_with_results(fresh)
    # value_hash captured at write time (no edits yet) == the raw-result hash.
    expected = output_column_value_hash(
        fx.project, sheet_id=fx.sheet, column_id=fx.col, row_ids=list(fx.row_ids)
    )
    receipt = _minimal_receipt(fx, run_id, expected)

    # A human edit changes the LIVE (overlay) hash but not the underlying result.
    fx.edit(fx.row_ids[0], "hand-fixed")
    result_hash_fn = _require_replay_export("output_column_result_value_hash")
    assert (
        result_hash_fn(
            fx.project,
            sheet_id=fx.sheet,
            column_id=fx.col,
            row_ids=list(fx.row_ids),
        )
        == expected
    ), "the raw current-run result hash bypasses the edit overlay"

    # Under REPLAY_EDIT_POLICY == "surface" the edited-cell value mismatch is
    # surfaced, NOT a hard stale_replay error.
    err = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind="test_map_output",
        action_kind="map.classify",
        expected_row_ids=list(fx.row_ids),
    )
    assert err is None, (
        "an edited-generated-cell value mismatch must no longer hard-error "
        "stale_replay under the surface policy"
    )

    # STRUCTURAL staleness (a renamed output column) stays a real stale_replay.
    fx.project.db.execute("UPDATE columns SET name='renamed' WHERE id=?", (fx.col,))
    fx.project.db.commit()
    structural = output_columns_replay_error(
        fx.project,
        receipt,
        output_kind="test_map_output",
        action_kind="map.classify",
        expected_row_ids=list(fx.row_ids),
    )
    assert structural is not None and structural.code == "stale_replay", (
        "structural staleness (column renamed/retyped/rebuilt, missing rows) "
        "stays a hard stale_replay error"
    )


def test_replay_parity_every_rerun_leaves_a_results_row(fx):
    # The read-time pending derivation depends on a
    # (current_run_id, row_id, column_id) -> results.value row existing after
    # ANY re-run flavor (dispositions #4). A re-run that short-circuits without
    # writing results would silently kill surfacing and must not exist.
    fresh = {rid: f"gen-{i}" for i, rid in enumerate(fx.row_ids)}
    run_id = fx.run_with_results(fresh)
    for rid in fx.row_ids:
        row = fx.project.db.execute(
            "SELECT value FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (run_id, rid, fx.col),
        ).fetchone()
        assert row is not None, (
            "each produced cell of a re-run has a comparable results row so the "
            "pending derivation is uniform across re-run flavors (dispositions #4)"
        )

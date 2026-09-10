from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from frisket.engine.executor import actions as executor_actions
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results

_PROJECT_ID = "proj-replay-chrome"


def _value_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# fixture: an ai_generated column with a current run + human-edit overlays
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

    def run_action(self, spec: dict[str, Any]):
        return executor_actions.run_action_spec(
            self.project, spec, project_id=_PROJECT_ID
        )


@pytest.fixture
def fx(tmp_path):
    project = Project.create(tmp_path / "p.frisket")
    try:
        yield _Fixture(project)
    finally:
        project.close()


def _accept_spec(fx: _Fixture, row_id: int, *, key: str) -> dict[str, Any]:
    pending = fx.project.pending_replay_values(fx.sheet, fx.col, row_ids=[row_id])[
        row_id
    ]
    return {
        "action_id": "replay.accept",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": fx.sheet,
            "row_id": row_id,
            "column_id": fx.col,
            "run_id": pending["run_id"],
            "generated_value_hash": pending["generated_value_hash"],
        },
        "idempotency_key": key,
    }


def _accept_column_spec(fx: _Fixture, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "replay.accept_column",
        "scope": {"kind": "project"},
        "params": {"sheet_id": fx.sheet, "column_id": fx.col},
        "idempotency_key": key,
    }


def _dismiss_spec(
    fx: _Fixture, row_id: int, value_hash: str, run_id: int | None, *, key: str
) -> dict[str, Any]:
    return {
        "action_id": "replay.dismiss",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": fx.sheet,
            "row_id": row_id,
            "column_id": fx.col,
            "generated_value_hash": value_hash,
            "run_id": run_id,
        },
        "idempotency_key": key,
    }


# --------------------------------------------------------------------------
# 1. replay.accept — accept one pending cell as a new provenance-stamped op
# --------------------------------------------------------------------------
def test_replay_accept_writes_a_new_edit_op_with_provenance_spec(fx):
    r = fx.row_ids
    run_id = fx.run_with_results({r[0]: "cat", r[1]: "dog"})
    fx.edit(r[0], "feline")
    fx.edit(r[1], "canine")
    # full-column count starts at 2 (both edited to a differing value).
    assert fx.project.pending_replay_count(fx.sheet, fx.col) == 2

    result = fx.run_action(_accept_spec(fx, r[0], key="accept-one@1"))
    assert result.status == "completed", (
        "replay.accept must complete as a v1 action over the typed wire contract "
        f"(got {result.status}: {[e.code for e in result.errors]})"
    )

    # the overlay now equals the fresh value -> the cell self-clears from pending
    # and the full-column count decrements.
    vals = fx.project.get_values(fx.sheet, fx.col, row_ids=[r[0]])
    assert vals[r[0]] == "cat", "accept takes the fresh generated value as a new edit"
    assert r[0] not in fx.project.pending_replay_values(fx.sheet, fx.col), (
        "accept self-clears: overlay == result -> zero diff"
    )
    assert fx.project.pending_replay_count(fx.sheet, fx.col) == 1, (
        "accepting one cell decrements the full-column chip count"
    )

    assert result.op_ids, "accept reports the new edit op id"
    op = fx.project.db.execute(
        "SELECT kind, spec FROM ops WHERE id=?", (result.op_ids[-1],)
    ).fetchone()
    assert op["kind"] == "edit", "accept is a normal, undoable edit op"
    spec = json.loads(op["spec"])
    assert spec.get("from_replay_accept") is True, (
        "the accept op distinguishes itself from a hand-typed edit"
    )
    assert spec.get("source_run_id") == run_id, "records the run it accepted from"
    assert spec.get("value_hash") == _value_hash("cat"), (
        "records the accepted value_hash for auditability (dispositions #4)"
    )


# --------------------------------------------------------------------------
# 2. replay.accept_column — accept every pending cell in one batched op
# --------------------------------------------------------------------------
def test_replay_accept_column_is_one_batched_op(fx):
    r = fx.row_ids
    fx.run_with_results({r[0]: "cat", r[1]: "dog", r[2]: "fish"})
    fx.edit(r[0], "feline")
    fx.edit(r[1], "canine")
    fx.edit(r[2], "fish")  # edited to the same value -> not pending, not accepted

    result = fx.run_action(_accept_column_spec(fx, key="accept-all@1"))
    assert result.status == "completed", (
        "replay.accept_column must complete as a v1 action "
        f"(got {result.status}: {[e.code for e in result.errors]})"
    )
    assert result.op_ids, "accept-all reports the single batch op id"

    edited_rows = {
        int(row["row_id"])
        for row in fx.project.db.execute(
            "SELECT row_id FROM edits WHERE op_id=?", (result.op_ids[-1],)
        )
    }
    assert edited_rows == {r[0], r[1]}, (
        "accept-all takes the fresh value for EVERY pending cell in the column in "
        "one op / one undo, skipping cells edited to the same value"
    )
    assert fx.project.pending_replay_values(fx.sheet, fx.col) == {}, (
        "accept-all clears the column's pending set"
    )
    assert fx.project.pending_replay_count(fx.sheet, fx.col) == 0


# --------------------------------------------------------------------------
# 3. replay.dismiss — durable, value-identity-keyed, project-global keep-edit
# --------------------------------------------------------------------------
def test_replay_dismiss_is_durable_and_value_identity_keyed(fx):
    r = fx.row_ids
    run_id = fx.run_with_results({r[0]: "cat"})
    fx.edit(r[0], "feline")
    assert set(fx.project.pending_replay_values(fx.sheet, fx.col)) == {r[0]}

    result = fx.run_action(
        _dismiss_spec(fx, r[0], _value_hash("cat"), run_id, key="dismiss@1")
    )
    assert result.status == "completed", (
        "replay.dismiss must complete as a v1 action "
        f"(got {result.status}: {[e.code for e in result.errors]})"
    )

    # a dismissal row lands in the dedicated, project-global table, keyed by
    # (row_id, column_id, generated_value_hash) (dispositions #3).
    row = fx.project.db.execute(
        "SELECT row_id, column_id, generated_value_hash, run_id "
        "FROM replay_edit_dismissals WHERE row_id=? AND column_id=?",
        (r[0], fx.col),
    ).fetchone()
    assert row is not None, "dismiss writes a durable replay_edit_dismissals row"
    assert row["generated_value_hash"] == _value_hash("cat"), (
        "the dismissal is keyed by the fresh value's identity hash"
    )

    assert fx.project.pending_replay_values(fx.sheet, fx.col) == {}, (
        "a dismissed cell drops from pending"
    )
    assert fx.project.pending_replay_count(fx.sheet, fx.col) == 0

    # durable across a fresh Project connection (persisted, not in-memory).
    path = fx.project.path
    fx.project.close()
    reopened = Project(path)
    try:
        assert reopened.pending_replay_values(fx.sheet, fx.col) == {}, (
            "the keep-edit dismissal is durable across reload"
        )
        store = RunResultStore(reopened)
        same_op = reopened.append_op("run", {"kind": "map"}, label="same re-run")
        same_run = store.start_run(
            same_op,
            fx.sheet,
            "map.classify",
            model="anthropic/claude-haiku-4-5",
            row_ids=[r[0]],
        )
        write_claimed_test_results(
            reopened,
            same_run,
            [{"row_id": r[0], "column_id": fx.col, "value": "cat", "outcome": "ok"}],
        )
        reopened.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (same_run, fx.col)
        )
        reopened.db.commit()
        assert reopened.pending_replay_values(fx.sheet, fx.col) == {}, (
            "the same generated value stays suppressed across future runs"
        )
        # a genuinely-newer generated value (new hash) re-surfaces the cell.
        op_id = reopened.append_op("run", {"kind": "map"}, label="re-run")
        new_run = store.start_run(
            op_id,
            fx.sheet,
            "map.classify",
            model="anthropic/claude-haiku-4-5",
            row_ids=[r[0]],
        )
        write_claimed_test_results(
            reopened,
            new_run,
            [{"row_id": r[0], "column_id": fx.col, "value": "lion", "outcome": "ok"}],
        )
        reopened.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (new_run, fx.col)
        )
        reopened.db.commit()
        assert set(reopened.pending_replay_values(fx.sheet, fx.col)) == {r[0]}, (
            "a newer, different fresh value re-surfaces past a value-identity "
            "dismissal (dispositions #3)"
        )
    finally:
        reopened.close()
    # reassign so the fixture teardown's second close is a harmless no-op path.
    fx.project = reopened


def test_replay_dismiss_requires_a_pending_human_edit(fx):
    row_id = fx.row_ids[0]
    run_id = fx.run_with_results({row_id: "cat"})
    request = _dismiss_spec(
        fx, row_id, _value_hash("cat"), run_id, key="dismiss-without-edit@1"
    )

    refused = fx.run_action(request)

    assert [error.code for error in refused.errors] == ["invalid_replay_target"]
    assert (
        fx.project.db.execute(
            "SELECT COUNT(*) FROM replay_edit_dismissals WHERE row_id=? AND column_id=?",
            (row_id, fx.col),
        ).fetchone()[0]
        == 0
    )

    fx.edit(row_id, "feline")
    assert set(fx.project.pending_replay_values(fx.sheet, fx.col)) == {row_id}


def test_managed_replay_requires_the_target_exact_head(fx):
    row_id, sibling_id = fx.row_ids[:2]
    run_id = fx.run_with_results({row_id: "cat", sibling_id: "dog"})
    fx.edit(row_id, "feline")
    accept = _accept_spec(fx, row_id, key="accept-missing-head@1")
    dismiss = _dismiss_spec(
        fx, row_id, _value_hash("cat"), run_id, key="dismiss-missing-head@1"
    )
    fx.project.db.execute(
        "DELETE FROM cell_result_heads WHERE column_id=? AND row_id=?",
        (fx.col, row_id),
    )
    fx.project.db.commit()

    assert (
        fx.project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE column_id=? AND row_id=?",
            (fx.col, sibling_id),
        ).fetchone()["run_id"]
        == run_id
    )
    assert fx.project.pending_replay_values(fx.sheet, fx.col) == {}
    assert [error.code for error in fx.run_action(accept).errors] == [
        "invalid_replay_target"
    ]
    assert [error.code for error in fx.run_action(dismiss).errors] == [
        "invalid_replay_target"
    ]
    assert (
        fx.project.db.execute(
            "SELECT COUNT(*) FROM replay_edit_dismissals WHERE row_id=? AND column_id=?",
            (row_id, fx.col),
        ).fetchone()[0]
        == 0
    )


def test_replay_accept_checks_claim_identity_and_supports_undo_redo(fx):
    row_id = fx.row_ids[0]
    run_id = fx.run_with_results({row_id: "cat"})
    fx.edit(row_id, "feline")
    request = _accept_spec(fx, row_id, key="accept-guarded@1")

    wrong_sheet = _accept_column_spec(fx, key="accept-column-wrong-sheet@1")
    wrong_sheet["params"]["sheet_id"] = fx.project.add_sheet("other")
    assert [error.code for error in fx.run_action(wrong_sheet).errors] == [
        "invalid_replay_target"
    ]

    _claims, conflict = OutputColumnClaimStore(fx.project).acquire(
        sheet_id=fx.sheet,
        output_names=["label"],
        action_kind="map.classify",
        claim_token="claim:replay-test",
        lease_seconds=None,
    )
    assert conflict is None
    blocked = fx.run_action(request)
    assert [error.code for error in blocked.errors] == ["output_column_busy"]
    OutputColumnClaimStore(fx.project).release(
        claim_token="claim:replay-test", status="released"
    )

    stale = json.loads(json.dumps(request))
    stale["params"]["run_id"] = run_id + 100
    stale["idempotency_key"] = "accept-stale-run@1"
    assert [error.code for error in fx.run_action(stale).errors] == ["stale_replay"]

    accepted = fx.run_action(request)
    assert accepted.status == "completed"
    assert fx.project.get_values(fx.sheet, fx.col, row_ids=[row_id])[row_id] == "cat"
    assert fx.project.undo() == accepted.op_ids[0]
    assert fx.project.get_values(fx.sheet, fx.col, row_ids=[row_id])[row_id] == "feline"
    assert fx.project.redo() == accepted.op_ids[0]
    assert fx.project.get_values(fx.sheet, fx.col, row_ids=[row_id])[row_id] == "cat"


def test_replay_dismiss_validates_target_converges_and_rolls_back(fx, monkeypatch):
    row_id = fx.row_ids[0]
    run_id = fx.run_with_results({row_id: "cat"})
    fx.edit(row_id, "feline")

    forged = _dismiss_spec(
        fx, row_id, f"sha256:{'b' * 64}", run_id, key="dismiss-forged@1"
    )
    assert [error.code for error in fx.run_action(forged).errors] == ["stale_replay"]

    wrong_sheet = _dismiss_spec(
        fx, row_id, _value_hash("cat"), run_id, key="dismiss-sheet@1"
    )
    wrong_sheet["params"]["sheet_id"] = fx.project.add_sheet("other")
    assert [error.code for error in fx.run_action(wrong_sheet).errors] == [
        "invalid_replay_target"
    ]

    first_request = _dismiss_spec(
        fx, row_id, _value_hash("cat"), run_id, key="dismiss-convergent@1"
    )
    first = fx.run_action(first_request)
    replayed = fx.run_action(first_request)
    assert replayed == first
    repeated = json.loads(json.dumps(first_request))
    repeated["idempotency_key"] = "dismiss-convergent@2"
    assert fx.run_action(repeated).status == "completed"
    assert (
        fx.project.db.execute(
            "SELECT COUNT(*) FROM replay_edit_dismissals WHERE row_id=? AND column_id=?",
            (row_id, fx.col),
        ).fetchone()[0]
        == 1
    )

    next_row = fx.row_ids[1]
    fx.run_with_results({row_id: "cat", next_row: "dog"})
    fx.edit(next_row, "canine")
    rollback_request = _dismiss_spec(
        fx, next_row, _value_hash("dog"), run_id + 1, key="dismiss-rollback@1"
    )
    monkeypatch.setattr(
        ReceiptStore,
        "insert_completed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("receipt fail")),
    )
    failed = fx.run_action(rollback_request)
    assert [error.code for error in failed.errors] == ["project_write_failed"]
    assert (
        fx.project.db.execute(
            "SELECT COUNT(*) FROM replay_edit_dismissals WHERE row_id=? AND column_id=?",
            (next_row, fx.col),
        ).fetchone()[0]
        == 0
    )

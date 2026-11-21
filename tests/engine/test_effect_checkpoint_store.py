"""Shared durable lifecycle for paid effect checkpoints.

One state machine (reserved/returned/consumed, with retire and
proven-no-egress discard as deletions), one transaction discipline, one JSON
canonicalization, and one refusal vocabulary.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from frisket.contracts.action import Receipt
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import (
    AmbiguousReserved,
    CheckpointLost,
    EffectCheckpointRef,
    EffectCheckpointStore,
    EffectCheckpointUnit,
    IdentityMismatch,
    InvalidCheckpointState,
    ModelCallIdMissing,
    canonical_json,
    model_call_ref,
    require_model_call_ids,
    row_effect_ref,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction
from frisket.engine.store.writer_fence import WriterFence
from frisket.execution.attempt import StaleAttemptWriter

UNIT = EffectCheckpointUnit(
    family="test_family",
    group_key="group-1",
    unit_key="unit-1",
    action_kind="classify",
    identity="identity-digest-1",
)


@pytest.fixture()
def project(make_project):
    p = make_project("cp")
    yield p


@pytest.fixture()
def store(project):
    return EffectCheckpointStore(project.db)


def _ref_for(checkpoint_id: str, unit: EffectCheckpointUnit) -> EffectCheckpointRef:
    return EffectCheckpointRef(
        checkpoint_id=checkpoint_id,
        family=unit.family,
        group_key=unit.group_key,
        unit_key=unit.unit_key,
        action_kind=unit.action_kind,
        identity=unit.identity,
    )


def _reserved(store, **overrides) -> EffectCheckpointRef:
    unit = replace(UNIT, **overrides)
    ref = _ref_for(f"cp-{unit.unit_key}", unit)
    assert store.reserve(ref) is True
    return ref


def _claimless_attempt(project, attempt_id: str, *, state: str) -> int:
    project.db.commit()
    sheet = project.db.execute("SELECT id FROM sheets LIMIT 1").fetchone()
    if sheet is None:
        sheet_id = project.add_sheet("recovery")
    else:
        sheet_id = int(sheet[0])
    op_id = project.append_change("map", {"recipe": "recovery"})
    run_id = RunResultStore(project).start_run(op_id, sheet_id, "map.recovery")
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 0, ?, 'recovery', '[]', datetime('now'))",
        (attempt_id, run_id, state),
    )
    if state == "dispatching":
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?", (attempt_id, run_id)
        )
    project.db.commit()
    return run_id


def _recovery_ref(
    checkpoint_id: str,
    *,
    group: str,
    identity: str = "same-paid-intent",
) -> EffectCheckpointRef:
    return EffectCheckpointRef(
        checkpoint_id=checkpoint_id,
        family="row_effect",
        group_key=group,
        unit_key="row-1",
        action_kind="map.recovery",
        identity=identity,
    )


def _reserve_claimless(
    store, checkpoint_id: str, *, run_id: int, attempt_id: str, group: str
) -> EffectCheckpointRef:
    ref = _recovery_ref(checkpoint_id, group=group)
    assert store.reserve(
        ref,
        fence=WriterFence.claimless(run_id=run_id, attempt_id=attempt_id),
    )
    return ref


def test_checkpoint_value_objects_are_frozen_and_project_unit() -> None:
    row_ref = row_effect_ref(
        "cp-row-1",
        run_id=14,
        row_id=9,
        action_kind="map.classify",
        identity="digest-row",
    )
    assert row_ref.family == "row_effect"
    assert row_ref.group_key == "14"
    assert row_ref.unit_key == "9"
    assert row_ref.unit == EffectCheckpointUnit(
        family="row_effect",
        group_key="14",
        unit_key="9",
        action_kind="map.classify",
        identity="digest-row",
    )

    call_ref = model_call_ref(
        "cp-call-1",
        group_key="map:req-1",
        unit_key="hash-1",
        action_kind="map.extract",
        identity="digest-call",
    )
    assert call_ref.family == "model_call"
    assert call_ref.unit == EffectCheckpointUnit(
        family="model_call",
        group_key="map:req-1",
        unit_key="hash-1",
        action_kind="map.extract",
        identity="digest-call",
    )

    with pytest.raises(FrozenInstanceError):
        setattr(row_ref, "identity", "mutated")
    with pytest.raises(FrozenInstanceError):
        setattr(row_ref.unit, "identity", "mutated")


def test_reserve_is_first_writer_wins(store) -> None:
    ref = _reserved(store)
    assert store.reserve(ref) is False
    # A different id for the SAME logical unit also loses: uniqueness is
    # (family, group_key, unit_key), so a changed request cannot evade an
    # older reservation by minting a new id.
    assert store.reserve(replace(ref, checkpoint_id="cp-other-id")) is False
    found = store.find_unit(
        family=ref.family,
        group_key=ref.group_key,
        unit_key=ref.unit_key,
    )
    assert found is not None
    assert found["id"] == ref.checkpoint_id
    assert found["state"] == "reserved"
    assert found["payload"] is None
    assert found["accounting_persisted"] is False


def test_adopt_reserved_recovery_moves_stable_id_to_current_writer(
    project, store
) -> None:
    old_run = _claimless_attempt(project, "attempt-old", state="dispatching")
    source_ref = _reserve_claimless(
        store, "stable-cloud-key", run_id=old_run, attempt_id="attempt-old", group="old"
    )
    project.db.execute(
        "UPDATE execution_attempts SET state='halted' WHERE id='attempt-old'"
    )
    project.db.execute("UPDATE runs SET current_attempt_id=NULL WHERE id=?", (old_run,))
    project.db.commit()
    new_run = _claimless_attempt(project, "attempt-new", state="dispatching")
    current_ref = _reserve_claimless(
        store,
        "replacement-no-egress",
        run_id=new_run,
        attempt_id="attempt-new",
        group="new",
    )
    new_fence = WriterFence.claimless(run_id=new_run, attempt_id="attempt-new")

    adopted = store.adopt_reserved_for_recovery(
        source_ref,
        current=current_ref,
        fence=new_fence,
    )

    assert adopted["id"] == "stable-cloud-key"
    assert adopted["group_key"] == "new"
    assert adopted["authorized_attempt_id"] == "attempt-new"
    assert store.get("replacement-no-egress") is None
    store.complete(
        _recovery_ref("stable-cloud-key", group="new"),
        payload={"response": "recovered"},
        fence=new_fence,
    )


@pytest.mark.parametrize(
    (
        "source_state",
        "source_checkpoint_state",
        "identity",
        "replacement_state",
        "error",
    ),
    [
        (
            "dispatching",
            "reserved",
            "same-paid-intent",
            "reserved",
            StaleAttemptWriter,
        ),
        ("halted", "reserved", "different-intent", "reserved", IdentityMismatch),
        (
            "halted",
            "returned",
            "same-paid-intent",
            "reserved",
            InvalidCheckpointState,
        ),
        (
            "halted",
            "reserved",
            "same-paid-intent",
            "returned",
            InvalidCheckpointState,
        ),
    ],
)
def test_adopt_reserved_recovery_rejects_unsafe_state_without_mutation(
    tmp_path,
    source_state,
    source_checkpoint_state,
    identity,
    replacement_state,
    error,
) -> None:
    project = Project.create(
        tmp_path / f"adopt-{source_state}-{replacement_state}.frisket"
    )
    store = EffectCheckpointStore(project.db)
    try:
        old_run = _claimless_attempt(project, "attempt-old", state="dispatching")
        source_ref = _reserve_claimless(
            store,
            "stable-cloud-key",
            run_id=old_run,
            attempt_id="attempt-old",
            group="old",
        )
        if source_checkpoint_state == "returned":
            store.complete(
                source_ref,
                payload={"response": "immutable"},
                fence=WriterFence.claimless(run_id=old_run, attempt_id="attempt-old"),
            )
        if source_state != "dispatching":
            project.db.execute(
                "UPDATE execution_attempts SET state=? WHERE id='attempt-old'",
                (source_state,),
            )
            project.db.execute(
                "UPDATE runs SET current_attempt_id=NULL WHERE id=?", (old_run,)
            )
            project.db.commit()
        new_run = _claimless_attempt(project, "attempt-new", state="dispatching")
        current_ref = _reserve_claimless(
            store,
            "replacement-no-egress",
            run_id=new_run,
            attempt_id="attempt-new",
            group="new",
        )
        new_fence = WriterFence.claimless(run_id=new_run, attempt_id="attempt-new")
        if replacement_state == "returned":
            store.complete(
                current_ref,
                payload={"response": "must-stay"},
                fence=new_fence,
            )
        before = store.list_all()

        with pytest.raises(error):
            store.adopt_reserved_for_recovery(
                _recovery_ref("stable-cloud-key", group="old", identity=identity),
                current=_recovery_ref(
                    "replacement-no-egress", group="new", identity=identity
                ),
                fence=new_fence,
            )
        assert store.list_all() == before
    finally:
        project.close()


def test_adopt_reserved_recovery_rejects_noncurrent_target_owner(project, store):
    old_run = _claimless_attempt(project, "attempt-old", state="dispatching")
    source_ref = _reserve_claimless(
        store, "stable-cloud-key", run_id=old_run, attempt_id="attempt-old", group="old"
    )
    project.db.execute(
        "UPDATE execution_attempts SET state='halted' WHERE id='attempt-old'"
    )
    project.db.execute("UPDATE runs SET current_attempt_id=NULL WHERE id=?", (old_run,))
    project.db.commit()
    new_run = _claimless_attempt(project, "attempt-new", state="dispatching")
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES ('attempt-sibling', ?, 1, 'halted', 'recovery', '[]', datetime('now'))",
        (new_run,),
    )
    project.db.commit()
    current_ref = _recovery_ref("replacement-no-egress", group="new")
    assert store.reserve(
        current_ref,
        fence=WriterFence(
            run_id=new_run,
            writer_attempt_id="attempt-new",
            claimless_direct_effect=True,
            authorized_attempt_id="attempt-sibling",
        ),
    )
    before = store.list_all()

    with pytest.raises(StaleAttemptWriter):
        store.adopt_reserved_for_recovery(
            source_ref,
            current=current_ref,
            fence=WriterFence.claimless(run_id=new_run, attempt_id="attempt-new"),
        )
    assert store.list_all() == before


def test_receipt_checkpoint_requires_its_exact_active_claimless_writer(
    project, store
) -> None:
    receipt_id = "receipt-checkpoint-owner"
    attempt_id = "attempt-checkpoint-owner"
    ReceiptStore(project).insert_running(
        Receipt(
            receipt_id=receipt_id,
            project_id="project",
            action_id="preview",
            action_kind="map.extract",
            status="running",
        )
    )
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, receipt_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 0, 'dispatching', 'identity', '[1]', datetime('now'))",
        (attempt_id, receipt_id),
    )
    project.db.commit()
    owned_fence = WriterFence.claimless(run_id=None, attempt_id=attempt_id)

    with pytest.raises(StaleAttemptWriter):
        store.reserve(
            _ref_for("receipt-wrong-writer", UNIT),
            fence=replace(owned_fence, writer_attempt_id="attempt-sibling"),
        )
    with pytest.raises(StaleAttemptWriter):
        store.reserve(
            _ref_for("receipt-run-mode", UNIT),
            fence=replace(owned_fence, claimless_direct_effect=False),
        )

    owned_ref = _ref_for("receipt-owned", UNIT)
    assert store.reserve(owned_ref, fence=owned_fence) is True
    assert (
        store.complete(
            owned_ref,
            payload={"ok": True},
            fence=owned_fence,
        )
        == 0.0
    )
    store.consume_and_retire(owned_ref, fence=owned_fence)
    assert store.get("receipt-owned") is None


def test_complete_runs_accounting_in_the_same_transaction(store) -> None:
    ref = _reserved(store)
    seen: list[dict] = []

    def accrue(checkpoint: dict) -> float:
        seen.append(checkpoint)
        return 0.25

    cost = store.complete(ref, payload={"b": 1, "a": [2, 3]}, accrue=accrue)
    assert cost == pytest.approx(0.25)
    assert seen and seen[0]["state"] == "reserved"
    assert seen[0]["authorized_attempt_id"] is None
    after = store.get(ref.checkpoint_id)
    assert after is not None
    assert after["state"] == "returned"
    assert after["payload"] == {"a": [2, 3], "b": 1}
    assert after["accounting_persisted"] is True


def test_complete_repairs_returned_payload_for_live_read_and_replay(store) -> None:
    ref = _reserved(store)
    calls: list[dict] = []

    def accrue(checkpoint: dict) -> float:
        calls.append(checkpoint)
        return 0.25

    payload = {
        "quote": "before \ud800 after",
        "nested": {"key\udcff": ["\ud83d\ude00", "ok"]},
    }
    assert store.complete(ref, payload=payload, accrue=accrue) == (pytest.approx(0.25))

    expected = {
        "quote": "before \ufffd after",
        "nested": {"key\ufffd": ["😀", "ok"]},
    }
    after = store.get(ref.checkpoint_id)
    assert after is not None
    assert after["payload"] == expected
    assert after["accounting_persisted"] is True
    assert len(calls) == 1
    assert store.returned_for_replay(ref.unit)["payload"] == expected


def test_complete_rolls_back_when_accounting_raises(store) -> None:
    ref = _reserved(store)

    def accrue(_checkpoint: dict) -> float:
        raise RuntimeError("provider fact write failed")

    with pytest.raises(RuntimeError, match="provider fact write failed"):
        store.complete(ref, payload={"x": 1}, accrue=accrue)
    after = store.get(ref.checkpoint_id)
    assert after is not None
    assert after["state"] == "reserved"
    assert after["payload"] is None
    assert after["accounting_persisted"] is False


def test_complete_without_accrue_leaves_accounting_unpersisted(store) -> None:
    ref = _reserved(store)
    assert store.complete(ref, payload={"ok": True}) == 0.0
    after = store.get(ref.checkpoint_id)
    assert after is not None
    assert after["state"] == "returned"
    assert after["accounting_persisted"] is False


def test_returned_for_replay_names_each_refusal(store) -> None:
    # Missing entirely -> lost.
    with pytest.raises(CheckpointLost):
        store.returned_for_replay(UNIT)

    ref = _reserved(store)
    # Still reserved -> ambiguous: a provider may have been reached.
    with pytest.raises(AmbiguousReserved):
        store.returned_for_replay(UNIT)
    # A different request or source value -> identity mismatch, checked
    # BEFORE state so a drifted request can never read ambiguity as replay.
    with pytest.raises(IdentityMismatch):
        store.returned_for_replay(replace(UNIT, identity="other"))

    store.complete(ref, payload={"answer": 42})
    replayed = store.returned_for_replay(UNIT)
    assert replayed["payload"] == {"answer": 42}

    # Retained-consumed is not replayable-returned.
    store.consume_group_retained(
        family=ref.family,
        group_key=ref.group_key,
        action_kind=ref.action_kind,
        audit=lambda raw: {"audited": raw is not None},
    )
    with pytest.raises(InvalidCheckpointState):
        store.returned_for_replay(UNIT)


def test_consume_and_retire_finalizes_and_deletes_in_one_transaction(
    store,
) -> None:
    ref = _reserved(store)
    store.complete(ref, payload={"answer": 42})
    seen: list[dict] = []

    def failing_finalize(checkpoint: dict) -> None:
        seen.append(checkpoint)
        raise RuntimeError("result insert failed")

    # The caller's consumption work fails -> the returned row survives; the
    # replay authority is not lost with the aborted transaction.
    with pytest.raises(RuntimeError, match="result insert failed"):
        store.consume_and_retire(ref, finalize=failing_finalize)
    still = store.get(ref.checkpoint_id)
    assert still is not None and still["state"] == "returned"
    assert seen and seen[0]["payload"] == {"answer": 42}

    store.consume_and_retire(ref, finalize=lambda cp: None)
    assert store.get(ref.checkpoint_id) is None


def test_consume_and_retire_refuses_ambiguous_reserved(store) -> None:
    ref = _reserved(store)
    with pytest.raises(AmbiguousReserved):
        store.consume_and_retire(ref)
    assert store.get(ref.checkpoint_id) is not None


def test_account_returned_is_idempotent(store) -> None:
    ref = _reserved(store)
    store.complete(ref, payload={"cost": 0.5})
    calls: list[dict] = []

    def accrue(checkpoint: dict) -> float:
        calls.append(checkpoint)
        return 0.5

    with write_transaction(store.db):
        first = store.account_returned(
            ref,
            payload={"cost": 0.0, "quote": "replay \ud800"},
            accrue=accrue,
        )
    with write_transaction(store.db):
        second = store.account_returned(
            ref,
            payload={"cost": 0.0, "quote": "replay \ud800"},
            accrue=accrue,
        )
    assert first == pytest.approx(0.5)
    assert second == 0.0
    assert len(calls) == 1
    after = store.get(ref.checkpoint_id)
    assert after is not None
    assert after["payload"] == {"cost": 0.0, "quote": "replay \ufffd"}
    assert after["accounting_persisted"] is True


def test_consume_group_retained_rewrites_only_returned_units(store) -> None:
    returned_ref = _reserved(store, unit_key="unit-a")
    store.complete(returned_ref, payload={"envelope": "raw"})
    reserved_ref = _reserved(store, unit_key="unit-b")

    consumed = store.consume_group_retained(
        family=UNIT.family,
        group_key=UNIT.group_key,
        action_kind=UNIT.action_kind,
        audit=lambda raw: {"sha_of": raw},
    )
    assert consumed == 1
    retained = store.get(returned_ref.checkpoint_id)
    assert retained is not None
    assert retained["state"] == "consumed"
    assert retained["payload"] == {"sha_of": canonical_json({"envelope": "raw"})}
    # The reserved unit stays an explicit possible-effect record.
    ambiguous = store.get(reserved_ref.checkpoint_id)
    assert ambiguous is not None and ambiguous["state"] == "reserved"


def test_discard_reserved_never_touches_returned(store) -> None:
    ref = _reserved(store)
    assert store.discard_reserved(ref) is True
    assert store.get(ref.checkpoint_id) is None

    replay_ref = _reserved(store, unit_key="unit-r")
    store.complete(replay_ref, payload={"kept": True})
    assert store.discard_reserved(replay_ref) is False
    assert store.get(replay_ref.checkpoint_id) is not None


def test_reserve_may_carry_caller_recovery_payload(store) -> None:
    # The model-call family stores its reconstruction payload at reserve
    # time; the shared shape allows it without weakening returned/consumed
    # (which always require a payload, per the table CHECK).
    ref = _ref_for("cp-with-payload", replace(UNIT, unit_key="unit-p"))
    assert store.reserve(ref, payload={"recover": 1})
    row = store.get(ref.checkpoint_id)
    assert row is not None
    assert row["state"] == "reserved"
    assert row["payload"] == {"recover": 1}


def test_canonical_json_is_deterministic_and_refuses_nan() -> None:
    assert canonical_json({"b": 1, "a": {"d": 2, "c": 3}}) == (
        '{"a":{"c":3,"d":2},"b":1}'
    )
    # ensure_ascii=False keeps stored UTF-8 byte-stable.
    assert canonical_json({"t": "café"}) == '{"t":"café"}'
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_require_model_call_ids_names_the_refusal() -> None:
    ok = [
        {"row_id": 1, "model_calls": [{"id": "call-1"}]},
        {"row_id": 2, "model_calls": None},
        {"row_id": 3},
        "not-a-dict",
        {"row_id": 4, "model_calls": ["not-a-dict-call"]},
    ]
    require_model_call_ids(ok)  # skips the writer-ignored shapes

    for bad_id in (None, "", "   ", 7):
        with pytest.raises(ModelCallIdMissing) as refusal:
            require_model_call_ids([{"model_calls": [{"id": bad_id}]}])
        assert refusal.value.code == "model_call_id_missing"


@pytest.mark.parametrize("retiring_writer", ["original", "recovery"])
def test_returned_checkpoint_retirement_preserves_owner_across_recovery(
    project, store, retiring_writer
):
    run_id = _claimless_attempt(project, "original", state="dispatching")
    ref = _ref_for("returned", replace(UNIT, group_key=str(run_id)))
    original_fence = WriterFence.claimless(run_id=run_id, attempt_id="original")
    assert store.reserve(ref, fence=original_fence)
    accounted = []

    def accrue(checkpoint):
        accounted.append(checkpoint["authorized_attempt_id"])
        return 0.5

    store.complete(
        ref,
        payload={"cost": 0.5},
        accrue=accrue,
        fence=original_fence,
    )
    project.db.execute(
        "UPDATE execution_attempts SET state='abandoned' WHERE id='original'"
    )
    for seq, name in enumerate(("replaced", "recovery"), start=1):
        project.db.execute(
            "INSERT INTO execution_attempts "
            "(id,run_id,seq,state,action_identity_hash,scope_json,created_at) "
            "VALUES (?,?,?,?,'recovery','[]',datetime('now'))",
            (name, run_id, seq, "abandoned" if name == "replaced" else "dispatching"),
        )
    project.db.execute(
        "UPDATE runs SET current_attempt_id='recovery' WHERE id=?", (run_id,)
    )
    other_run = _claimless_attempt(project, "other-run", state="dispatching")
    assert other_run != run_id
    project.db.commit()
    for refused in ("missing", "replaced", "other-run"):
        with pytest.raises(StaleAttemptWriter):
            store.consume_and_retire(
                ref,
                fence=WriterFence.claimless(run_id=run_id, attempt_id=refused),
            )
        assert store.get("returned")["authorized_attempt_id"] == "original"
    # Original fact authority cannot run a publication callback after losing
    # its writer; only retirement/accounting remains allowed under that owner.
    with pytest.raises(StaleAttemptWriter):
        store.consume_and_retire(
            ref,
            finalize=lambda _checkpoint: None,
            fence=original_fence,
        )
    store.consume_and_retire(
        ref,
        fence=WriterFence.claimless(run_id=run_id, attempt_id=retiring_writer),
    )
    assert store.get("returned") is None
    assert accounted == ["original"]

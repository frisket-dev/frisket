"""Lane 2.2: the shared durable lifecycle for paid effect checkpoints.

One state machine (reserved/returned/consumed, with retire and
proven-no-egress discard as deletions), one transaction discipline, one JSON
canonicalization, one refusal vocabulary.  The four family-specific copies
migrate onto this store in Lane 2.4; these tests pin the shared semantics
they will map onto.
"""

from __future__ import annotations

import pytest

from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import (
    AmbiguousReserved,
    CheckpointLost,
    EffectCheckpointStore,
    IdentityMismatch,
    InvalidCheckpointState,
    ModelCallIdMissing,
    canonical_json,
    require_model_call_ids,
)

UNIT = {
    "family": "test_family",
    "group_key": "group-1",
    "unit_key": "unit-1",
    "action_kind": "classify",
    "identity": "identity-digest-1",
}


@pytest.fixture()
def project(tmp_path):
    p = Project.create(tmp_path / "checkpoints.frisket", name="cp")
    try:
        yield p
    finally:
        p.close()


@pytest.fixture()
def store(project):
    return EffectCheckpointStore(project.db)


def _reserved(store, **overrides):
    unit = {**UNIT, **overrides}
    assert store.reserve(f"cp-{unit['unit_key']}", **unit) is True
    return f"cp-{unit['unit_key']}", unit


def test_reserve_is_first_writer_wins(store) -> None:
    checkpoint_id, unit = _reserved(store)
    assert store.reserve(checkpoint_id, **unit) is False
    # A different id for the SAME logical unit also loses: uniqueness is
    # (family, group_key, unit_key), so a changed request cannot evade an
    # older reservation by minting a new id.
    assert store.reserve("cp-other-id", **unit) is False
    found = store.find_unit(
        family=unit["family"],
        group_key=unit["group_key"],
        unit_key=unit["unit_key"],
    )
    assert found is not None
    assert found["id"] == checkpoint_id
    assert found["state"] == "reserved"
    assert found["payload"] is None
    assert found["accounting_persisted"] is False


def test_complete_runs_accounting_in_the_same_transaction(store) -> None:
    checkpoint_id, unit = _reserved(store)
    seen: list[dict] = []

    def accrue(checkpoint: dict) -> float:
        seen.append(checkpoint)
        return 0.25

    cost = store.complete(
        checkpoint_id, **unit, payload={"b": 1, "a": [2, 3]}, accrue=accrue
    )
    assert cost == pytest.approx(0.25)
    assert seen and seen[0]["state"] == "reserved"
    assert seen[0]["authorized_attempt_id"] is None
    after = store.get(checkpoint_id)
    assert after is not None
    assert after["state"] == "returned"
    assert after["payload"] == {"a": [2, 3], "b": 1}
    assert after["accounting_persisted"] is True


def test_complete_rolls_back_when_accounting_raises(store) -> None:
    checkpoint_id, unit = _reserved(store)

    def accrue(_checkpoint: dict) -> float:
        raise RuntimeError("provider fact write failed")

    with pytest.raises(RuntimeError, match="provider fact write failed"):
        store.complete(checkpoint_id, **unit, payload={"x": 1}, accrue=accrue)
    after = store.get(checkpoint_id)
    assert after is not None
    assert after["state"] == "reserved"
    assert after["payload"] is None
    assert after["accounting_persisted"] is False


def test_complete_without_accrue_leaves_accounting_unpersisted(store) -> None:
    checkpoint_id, unit = _reserved(store)
    assert store.complete(checkpoint_id, **unit, payload={"ok": True}) == 0.0
    after = store.get(checkpoint_id)
    assert after is not None
    assert after["state"] == "returned"
    assert after["accounting_persisted"] is False


def test_returned_for_replay_names_each_refusal(store) -> None:
    lookup = {
        "family": UNIT["family"],
        "group_key": UNIT["group_key"],
        "unit_key": UNIT["unit_key"],
    }
    expected = {"action_kind": UNIT["action_kind"], "identity": UNIT["identity"]}

    # Missing entirely -> lost.
    with pytest.raises(CheckpointLost):
        store.returned_for_replay(**lookup, **expected)

    checkpoint_id, unit = _reserved(store)
    # Still reserved -> ambiguous: a provider may have been reached.
    with pytest.raises(AmbiguousReserved):
        store.returned_for_replay(**lookup, **expected)
    # A different request or source value -> identity mismatch, checked
    # BEFORE state so a drifted request can never read ambiguity as replay.
    with pytest.raises(IdentityMismatch):
        store.returned_for_replay(**lookup, **{**expected, "identity": "other"})

    store.complete(checkpoint_id, **unit, payload={"answer": 42})
    replayed = store.returned_for_replay(**lookup, **expected)
    assert replayed["payload"] == {"answer": 42}

    # Retained-consumed is not replayable-returned.
    store.consume_group_retained(
        family=unit["family"],
        group_key=unit["group_key"],
        action_kind=unit["action_kind"],
        audit=lambda raw: {"audited": raw is not None},
    )
    with pytest.raises(InvalidCheckpointState):
        store.returned_for_replay(**lookup, **expected)


def test_consume_and_retire_finalizes_and_deletes_in_one_transaction(
    store,
) -> None:
    checkpoint_id, unit = _reserved(store)
    store.complete(checkpoint_id, **unit, payload={"answer": 42})
    seen: list[dict] = []

    def failing_finalize(checkpoint: dict) -> None:
        seen.append(checkpoint)
        raise RuntimeError("result insert failed")

    # The caller's consumption work fails -> the returned row survives; the
    # replay authority is not lost with the aborted transaction.
    with pytest.raises(RuntimeError, match="result insert failed"):
        store.consume_and_retire(checkpoint_id, **unit, finalize=failing_finalize)
    still = store.get(checkpoint_id)
    assert still is not None and still["state"] == "returned"
    assert seen and seen[0]["payload"] == {"answer": 42}

    store.consume_and_retire(checkpoint_id, **unit, finalize=lambda cp: None)
    assert store.get(checkpoint_id) is None


def test_consume_and_retire_refuses_ambiguous_reserved(store) -> None:
    checkpoint_id, unit = _reserved(store)
    with pytest.raises(AmbiguousReserved):
        store.consume_and_retire(checkpoint_id, **unit)
    assert store.get(checkpoint_id) is not None


def test_account_returned_is_idempotent(store) -> None:
    checkpoint_id, unit = _reserved(store)
    store.complete(checkpoint_id, **unit, payload={"cost": 0.5})
    calls: list[dict] = []

    def accrue(checkpoint: dict) -> float:
        calls.append(checkpoint)
        return 0.5

    first = store.account_returned(
        checkpoint_id, **unit, payload={"cost": 0.0}, accrue=accrue
    )
    second = store.account_returned(
        checkpoint_id, **unit, payload={"cost": 0.0}, accrue=accrue
    )
    assert first == pytest.approx(0.5)
    assert second == 0.0
    assert len(calls) == 1
    after = store.get(checkpoint_id)
    assert after is not None
    assert after["payload"] == {"cost": 0.0}
    assert after["accounting_persisted"] is True


def test_consume_group_retained_rewrites_only_returned_units(store) -> None:
    returned_id, returned_unit = _reserved(store, unit_key="unit-a")
    store.complete(returned_id, **returned_unit, payload={"envelope": "raw"})
    reserved_id, _reserved_unit = _reserved(store, unit_key="unit-b")

    consumed = store.consume_group_retained(
        family=UNIT["family"],
        group_key=UNIT["group_key"],
        action_kind=UNIT["action_kind"],
        audit=lambda raw: {"sha_of": raw},
    )
    assert consumed == 1
    retained = store.get(returned_id)
    assert retained is not None
    assert retained["state"] == "consumed"
    assert retained["payload"] == {"sha_of": canonical_json({"envelope": "raw"})}
    # The reserved unit stays an explicit possible-effect record.
    ambiguous = store.get(reserved_id)
    assert ambiguous is not None and ambiguous["state"] == "reserved"


def test_discard_reserved_never_touches_returned(store) -> None:
    checkpoint_id, unit = _reserved(store)
    assert store.discard_reserved(checkpoint_id, **unit) is True
    assert store.get(checkpoint_id) is None

    replay_id, replay_unit = _reserved(store, unit_key="unit-r")
    store.complete(replay_id, **replay_unit, payload={"kept": True})
    assert store.discard_reserved(replay_id, **replay_unit) is False
    assert store.get(replay_id) is not None


def test_reserve_may_carry_caller_recovery_payload(store) -> None:
    # The model-call family stores its reconstruction payload at reserve
    # time; the shared shape allows it without weakening returned/consumed
    # (which always require a payload, per the table CHECK).
    checkpoint_id = "cp-with-payload"
    assert store.reserve(
        checkpoint_id, **{**UNIT, "unit_key": "unit-p"}, payload={"recover": 1}
    )
    row = store.get(checkpoint_id)
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

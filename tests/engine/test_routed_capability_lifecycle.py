from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from frisket.actions.core import RegisteredAction
from frisket.actions.registry import ACTION_REGISTRY
from tests.engine import test_typed_geocode, test_census_demographics_executor

geocode_env = test_typed_geocode.env
census_env = test_census_demographics_executor.env


@pytest.fixture(autouse=True)
def _always_challenge_metered_actions(monkeypatch):
    # The imported provider fixtures create their Project before each test;
    # pin the local consent posture before those fixtures run.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")


def _raise_after_handler(monkeypatch, action_id, error):
    registered = ACTION_REGISTRY.get(action_id)
    terminal = registered.definition.run

    async def handler(params, source, capability):
        await terminal.handler(params, source, capability)
        raise error

    modified = RegisteredAction(
        action_id,
        replace(registered.definition, run=replace(terminal, handler=handler)),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY, "_actions", {**ACTION_REGISTRY._actions, action_id: modified}
    )


@pytest.mark.parametrize("cancel", [False, True])
def test_returned_paid_row_facts_survive_author_failure_and_cancellation(
    geocode_env, monkeypatch, cancel
):
    project, sheet_id, calls, run = geocode_env
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")
    _raise_after_handler(
        monkeypatch,
        "enrich.geocode",
        asyncio.CancelledError() if cancel else ValueError("author projection failed"),
    )
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "address", "engine": "opencage"},
        "idempotency_key": "geo-failed-author",
    }
    first = run(body)
    assert first.status == "needs_confirmation", first.errors
    body["confirmation"] = first.errors[0].details["promise_set_hash"]
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            run(body)
    else:
        result = run(body)
        assert result.status == "failed", result.errors
    assert len(calls) == 1
    facts = project.db.execute("SELECT * FROM model_calls").fetchall()
    assert len(facts) == 1
    assert facts[0]["provider_cost_usd"] > 0
    assert project.db.execute("SELECT cost_actual FROM runs").fetchone()[0] > 0
    if cancel:
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_returned_batch_facts_survive_author_failure_and_cancellation(
    census_env, monkeypatch, cancel
):
    project, sheet_id, _, calls, run = census_env
    _raise_after_handler(
        monkeypatch,
        "enrich.census_demographics",
        asyncio.CancelledError() if cancel else ValueError("author projection failed"),
    )
    body = {
        "action_id": "enrich.census_demographics",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "point"},
        "idempotency_key": "census-failed-author",
    }
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            run(body)
    else:
        result = run(body)
        assert result.status == "failed", result.errors
    assert len(calls) == 2
    facts = project.db.execute("SELECT * FROM model_calls").fetchall()
    assert len(facts) == 5
    assert sum(fact["row_id"] is None for fact in facts) == 2
    assert all(fact["provider_cost_usd"] == 0 for fact in facts)
    if cancel:
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_accounting_drain_failure_does_not_replace_cancellation(
    geocode_env, monkeypatch
):
    from frisket.engine.store.runs import RunResultStore

    project, sheet_id, calls, run = geocode_env
    _raise_after_handler(monkeypatch, "enrich.geocode", asyncio.CancelledError())
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "address"},
        "idempotency_key": "geo-cancel-drain",
    }
    drains = []

    def fail_drain(*args, **kwargs):
        drains.append(args)
        raise RuntimeError("accounting writer unavailable")

    monkeypatch.setattr(RunResultStore, "write_returned_call_accounting", fail_drain)
    with pytest.raises(asyncio.CancelledError):
        run(body)
    assert len(drains) == 1
    assert len(calls) == 1
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE status='completed'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("fail_first", [False, True])
def test_store_owns_sanitization_and_preserves_reusable_routed_facts(
    geocode_env, monkeypatch, fail_first
):
    from frisket.engine.store.runs import RunResultStore

    project, sheet_id, calls, run = geocode_env
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")
    _raise_after_handler(monkeypatch, "enrich.geocode", ValueError("after lookup"))
    original = RunResultStore.write_returned_call_accounting
    writes = []

    def repeated_write(store, run_id, batch, **kwargs):
        if not any(result.get("model_calls") for result in batch):
            return original(store, run_id, batch, **kwargs)
        snapshot = deepcopy(batch)
        if fail_first:
            invalid = {
                **batch[0],
                "model_calls": [
                    {
                        **batch[0]["model_calls"][0],
                        "id": "invalid-attempt",
                        "run_id": -1,
                    }
                ],
            }
            with pytest.raises(RuntimeError, match="run_id override"):
                original(store, run_id, [*batch, invalid], **kwargs)
            assert batch == snapshot
            assert (
                project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
                == 0
            )
            assert (
                project.db.execute("SELECT COUNT(*) FROM binding_epochs").fetchone()[0]
                == 0
            )
        cost = original(store, run_id, batch, **kwargs)
        original(store, run_id, batch, **kwargs)
        assert batch == snapshot
        writes.append(snapshot)
        return cost

    monkeypatch.setattr(
        RunResultStore, "write_returned_call_accounting", repeated_write
    )
    body = test_typed_geocode._request(sheet_id)
    result, _ = test_typed_geocode._confirmed(run, body)
    assert result.status == "failed"
    assert len(calls) == len(writes) == 1
    facts = project.db.execute("SELECT * FROM model_calls").fetchall()
    assert len(facts) == 1
    assert facts[0]["provider_cost_usd"] == pytest.approx(0.01)
    assert facts[0]["epoch_id"] is not None
    assert project.db.execute("SELECT COUNT(*) FROM binding_epochs").fetchone()[0] == 1
    assert project.db.execute("SELECT cost_actual FROM runs").fetchone()[
        0
    ] == pytest.approx(0.01)

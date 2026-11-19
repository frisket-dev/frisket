"""Owned independent #635 cancellation review; not a production change."""

import asyncio

import pytest

from frisket.ai.embeddings import EmbeddingStore
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store.receipts import ReceiptStore
from test_embedding_effect_checkpoint import (
    PaidRemoteGateway,
    _create_remote_index,
    _refresh_action,
    _run,
    project as project,
)
from test_embedding_lifecycle_replay_hardening import (
    FakeProbeGateway,
    _OpenRouterRouter,
    _create_action,
)


INTERRUPTIONS = [KeyboardInterrupt, SystemExit, asyncio.CancelledError]


def state(project):
    return {
        "transaction_open": project.db.in_transaction,
        "indexes": [
            dict(r) for r in project.db.execute("SELECT * FROM embedding_indexes")
        ],
        "receipts": [
            dict(r) for r in project.db.execute("SELECT status,body FROM receipts")
        ],
        "runs": [
            dict(r)
            for r in project.db.execute("SELECT id,status,cost_actual FROM runs")
        ],
        "calls": [dict(r) for r in project.db.execute("SELECT * FROM model_calls")],
    }


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_create_cancel_after_paid_probe_before_receipt(
    project, monkeypatch, interruption
):
    gateway = FakeProbeGateway(dim=1024)
    request = _create_action(
        project._sheet_id,
        key="cancel-create",
        provider="openrouter",
        model="openai/text-embedding-3-large",
        provider_policy={"allow_remote": True},
    )
    real_create = EmbeddingStore.create_index

    def interrupt_after_create(self, *args, **kwargs):
        real_create(self, *args, **kwargs)
        raise interruption("cancelled metadata publication")

    monkeypatch.setattr(EmbeddingStore, "create_index", interrupt_after_create)
    with pytest.raises(interruption):
        run_action_spec(
            project,
            request,
            project_id="p",
            deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
        )
    observed = state(project)
    print("CREATE AFTER CANCEL", observed)
    assert len(gateway.calls) == 1
    assert len(observed["calls"]) == 1
    assert not observed["transaction_open"], observed
    assert observed["indexes"] == [], observed
    project.db.commit()
    assert not project.db.execute("SELECT 1 FROM embedding_indexes").fetchall()


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_refresh_cancel_after_counts_before_terminal_receipt(
    project, monkeypatch, interruption
):
    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)
    real_update = EmbeddingStore.update_index_counts

    def interrupt_after_counts(self, *args, **kwargs):
        result = real_update(self, *args, **kwargs)
        if kwargs.get("commit") is False:
            raise interruption("cancelled terminal publication")
        return result

    monkeypatch.setattr(EmbeddingStore, "update_index_counts", interrupt_after_counts)
    with pytest.raises(interruption):
        _run(project, _refresh_action(index_id, key="cancel-refresh"), gateway=gateway)
    observed = state(project)
    print("REFRESH AFTER CANCEL", observed)
    assert len(gateway.calls) == 1
    assert len(observed["calls"]) == 1
    assert not observed["transaction_open"], observed
    index = EmbeddingStore(project).get_index(index_id)
    assert index["refresh_claim_token"] is None, observed
    assert index["last_refresh_receipt_id"] is None, observed
    assert ReceiptStore(project).find_by_id(index["last_refresh_receipt_id"]) is None


def test_create_paid_probe_respects_network_off(project):
    project.set_network_policy(mode="off")
    gateway = FakeProbeGateway(dim=1024)
    request = _create_action(
        project._sheet_id,
        key="network-off-create",
        provider="openrouter",
        model="openai/text-embedding-3-large",
        provider_policy={"allow_remote": True},
    )
    result = run_action_spec(
        project,
        request,
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
    )
    assert gateway.calls == [], (result.status, gateway.calls)
    assert result.status == "failed"
    assert result.errors[0].code == "network_disabled"


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
def test_interrupted_early_repair_releases_claim_without_committing_counts(
    project,
    monkeypatch,
    interruption,
):
    from frisket.engine.executor.action_families import embeddings

    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)
    before = dict(EmbeddingStore(project).get_index(index_id))

    def interrupt_repair(project, index_id):
        # Leave a pending update to prove claim cleanup cannot commit it.
        project.db.execute(
            "UPDATE embedding_indexes SET total_items=999, ready_items=999 WHERE id=?",
            (index_id,),
        )
        raise interruption("cancelled early repair")

    monkeypatch.setattr(embeddings, "repair_unfinalized_refresh", interrupt_repair)
    with pytest.raises(interruption):
        _run(project, _refresh_action(index_id, key="cancel-repair"), gateway=gateway)
    index = EmbeddingStore(project).get_index(index_id)
    assert index["refresh_claim_token"] is None
    assert index["total_items"] == before["total_items"]
    assert index["ready_items"] == before["ready_items"]
    assert not project.db.in_transaction
    assert gateway.calls == []


def test_network_off_still_allows_metadata_only_remote_creation(project):
    project.set_network_policy(mode="off")
    gateway = PaidRemoteGateway()
    index_id = _create_remote_index(project, gateway=gateway)
    assert EmbeddingStore(project).get_index(index_id) is not None
    assert gateway.calls == []


def test_refresh_cancel_after_return_keeps_paid_fact_without_publication(project):
    from frisket.engine.store.runs import RunResultStore
    from unified_embedding_probe import UnifiedEmbeddingProbe

    class CancelAfterReturn(PaidRemoteGateway):
        def embed(self, *args, **kwargs):
            result = super().embed(*args, **kwargs)
            [run] = project.db.execute(
                "SELECT id FROM runs WHERE status='running'"
            ).fetchall()
            RunResultStore(project).request_cancel(run["id"])
            return result

    gateway = CancelAfterReturn()
    index_id = _create_remote_index(project, gateway=gateway)
    with pytest.raises(asyncio.CancelledError):
        _run(
            project,
            _refresh_action(index_id, key="cancel-after-return"),
            gateway=gateway,
        )
    assert len(gateway.calls) == 1
    [run] = project.db.execute("SELECT status,cost_actual FROM runs").fetchall()
    assert run["status"] == "cancelled"
    assert run["cost_actual"] == pytest.approx(0.001)
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    [attempt] = project.db.execute("SELECT state FROM execution_attempts").fetchall()
    assert attempt["state"] == "halted"
    backend = UnifiedEmbeddingProbe(project)
    backend.ensure_schema()
    assert backend.item_counts(index_id) == {}
    backend.close()
    assert not project.db.in_transaction


def test_probe_cancel_after_return_keeps_fact_and_closes_without_index(project):
    from frisket.engine.store.runs import RunResultStore

    class CancelAfterProbe(FakeProbeGateway):
        def embed(self, *args, **kwargs):
            result = super().embed(*args, **kwargs)
            [run] = project.db.execute(
                "SELECT id FROM runs WHERE status='running'"
            ).fetchall()
            RunResultStore(project).request_cancel(run["id"])
            return result

    gateway = CancelAfterProbe(dim=1024)
    request = _create_action(
        project._sheet_id,
        key="cancel-probe-return",
        provider="openrouter",
        model="openai/text-embedding-3-large",
        provider_policy={"allow_remote": True},
    )
    with pytest.raises(asyncio.CancelledError):
        run_action_spec(
            project,
            request,
            project_id="p",
            deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
        )
    assert len(gateway.calls) == 1
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    [run] = project.db.execute("SELECT status FROM runs").fetchall()
    assert run["status"] == "cancelled"
    assert not project.db.execute("SELECT 1 FROM embedding_indexes").fetchall()
    assert not project.db.in_transaction


def test_probe_cancel_before_dispatch_closes_current_attempt(project, monkeypatch):
    from frisket.engine.executor.action_families import embeddings
    from frisket.engine.store.runs import RunResultStore

    mint = embeddings.mint_direct_effect_attempt

    def cancel_after_mint(current, **kwargs):
        attempt = mint(current, **kwargs)
        RunResultStore(current).request_cancel(kwargs["run_id"])
        return attempt

    monkeypatch.setattr(embeddings, "mint_direct_effect_attempt", cancel_after_mint)
    gateway = FakeProbeGateway(dim=1024)
    request = _create_action(
        project._sheet_id,
        key="cancel-probe-dispatch",
        provider="openrouter",
        model="openai/text-embedding-3-large",
        provider_policy={"allow_remote": True},
    )
    with pytest.raises(asyncio.CancelledError):
        run_action_spec(
            project,
            request,
            project_id="p",
            deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
        )
    assert gateway.calls == []
    [run] = project.db.execute("SELECT status FROM runs").fetchall()
    assert run["status"] == "cancelled"
    [attempt] = project.db.execute("SELECT state FROM execution_attempts").fetchall()
    assert attempt["state"] == "halted"
    assert not project.db.in_transaction

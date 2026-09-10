"""Independent review of provider-return checkpoint cancellation at 0fd7ef58."""

import asyncio
import sqlite3

import pytest

from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store.receipts import ReceiptStore
from test_embedding_effect_checkpoint import project as project
from test_embedding_lifecycle_replay_hardening import (
    FakeProbeGateway,
    _OpenRouterRouter,
    _create_action,
)


@pytest.mark.parametrize(
    "interruption", [KeyboardInterrupt, SystemExit, asyncio.CancelledError]
)
def test_paid_fact_survives_checkpoint_publication_cancellation(
    project, monkeypatch, interruption
):
    gateway = FakeProbeGateway(dim=1024)
    request = _create_action(
        project._sheet_id,
        key="cancel-probe-checkpoint",
        provider="openrouter",
        model="openai/text-embedding-3-large",
        provider_policy={"allow_remote": True},
    )
    real_update = ReceiptStore.update_body_status

    def interrupt_checkpoint(self, receipt, **kwargs):
        if any(
            e.ref.get("kind") == "embedding_dimension_probe_checkpoint"
            for e in receipt.evidence
        ):
            raise interruption(
                "cancelled checkpoint publication after paid fact insert"
            )
        return real_update(self, receipt, **kwargs)

    monkeypatch.setattr(ReceiptStore, "update_body_status", interrupt_checkpoint)
    with pytest.raises(interruption):
        run_action_spec(
            project,
            request,
            project_id="p",
            deps=ExecutorDeps(embedding_gateway=gateway, router=_OpenRouterRouter()),
        )
    database_path = project.db.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(database_path) as independent_reader:
        durable_facts = independent_reader.execute(
            "SELECT COUNT(*) FROM model_calls"
        ).fetchone()[0]
    observed = {
        "provider_calls": len(gateway.calls),
        "transaction_open": project.db.in_transaction,
        "pending_facts": project.db.execute(
            "SELECT COUNT(*) FROM model_calls"
        ).fetchone()[0],
        "durable_facts": durable_facts,
    }
    print(observed)
    assert observed == {
        "provider_calls": 1,
        "transaction_open": False,
        "pending_facts": 1,
        "durable_facts": 1,
    }

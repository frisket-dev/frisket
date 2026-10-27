"""Run projections can read receipt-owned facts without moving or re-rating them."""

from frisket.contracts.action import Receipt
from frisket.engine.store.receipts import ReceiptStore
from tests.engine.test_run_cost_projection import _seed, _fact, _writer


def test_direct_and_receipt_linked_facts_project_once_without_spend_mutation(tmp_path):
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    try:
        store.write_model_calls(
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "model_calls": [_fact("direct", 0.02)],
                }
            ],
            **_writer(project, run_id, column_id),
        )
        receipt_id = "receipt-linked-projection"
        ReceiptStore(project).insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id="cost",
                action_id="sample",
                action_kind="map.classify",
                params_hash="fixture",
                status="running",
                run_id=run_id,
            )
        )
        # Receipt-owned facts are already accounted. These recorded rows model
        # the two owner layouts readers encounter, including a historical fact
        # with both associations; projection must never count that row twice.
        project.db.execute(
            "INSERT INTO execution_attempts(id,receipt_id,seq,state,action_identity_hash,scope_json,created_at) VALUES(?,? ,1,'effected',?,'[-1]',datetime('now'))",
            ("receipt-attempt", receipt_id, "a" * 64),
        )
        for call_id, owner, cost in [
            ("receipt-owned", None, 0.01),
            ("both-associations", run_id, 0.04),
        ]:
            project.db.execute(
                "INSERT INTO model_calls(id,fact_version,run_id,attempt_id,capability,engine,provider,provider_kind,credential_source,provider_cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    "frisket.model-call-fact.v1",
                    owner,
                    "receipt-attempt",
                    "classify",
                    "fixture",
                    "fixture",
                    "test",
                    "platform_key",
                    cost,
                ),
            )
        project.db.commit()
        before_facts = [
            tuple(row)
            for row in project.db.execute(
                "SELECT id,run_id,attempt_id,provider_cost_usd FROM model_calls ORDER BY id"
            )
        ]
        before_spend = [
            tuple(row)
            for row in project.db.execute(
                "SELECT provider,spent_micro,unmetered_calls FROM project_provider_keys ORDER BY provider"
            )
        ]
        assert {row["id"] for row in store.model_calls(run_id)} == {
            "direct",
            "receipt-owned",
            "both-associations",
        }
        store.finish_run(run_id)
        assert (
            project.db.execute(
                "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            == 0.07
        )
        assert [
            tuple(row)
            for row in project.db.execute(
                "SELECT id,run_id,attempt_id,provider_cost_usd FROM model_calls ORDER BY id"
            )
        ] == before_facts
        assert [
            tuple(row)
            for row in project.db.execute(
                "SELECT provider,spent_micro,unmetered_calls FROM project_provider_keys ORDER BY provider"
            )
        ] == before_spend
    finally:
        project.close()

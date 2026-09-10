"""Actual cluster queue authority and paid-failure recovery boundaries."""

import copy


from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.executor.queued_actions import (
    queued_v1_payload_envelope,
    queued_v1_run_authorizes_action_lifecycle,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from helpers import replace_test_source_cells
from http_test_helpers import drain_queue
from test_typed_cluster_action import cluster_project as cluster_project


def test_real_cluster_queue_payload_roundtrips_and_finishes(tmp_path):
    with TestClient(create_app(tmp_path / "workspace", router=ModelRouter())) as client:
        pid = client.post("/api/projects", json={"name": "Review"}).json()["id"]
        imported = client.post(
            f"/api/projects/{pid}/import/csv",
            files={"file": ("names.csv", "name\nJon Smith\nSmith Jon\n", "text/csv")},
        )
        assert imported.status_code == 200, imported.text
        response = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "cluster.values",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": imported.json()["sheet_id"],
                },
                "params": {"source": "name"},
                "output_names": {"canonical": "Reviewed"},
                "idempotency_key": "review-cluster-queue",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "queued", body
        queue = client.app.state.workspace.queue
        job = queue.get(body["job_id"])
        envelope = queued_v1_payload_envelope(job.payload)
        assert envelope is not None, job.payload
        project = client.app.state.workspace.get(pid)
        assert envelope.pre_run_error(project) is None
        assert queued_v1_run_authorizes_action_lifecycle(
            project, job.payload, run_id=body["run_id"]
        )
        changed = copy.deepcopy(job.payload)
        changed["v1_cluster_values"]["options"]["min_size"] = 1
        changed["spec"]["cluster_values"] = copy.deepcopy(changed["v1_cluster_values"])
        forged = queued_v1_payload_envelope(changed)
        assert forged is not None
        assert forged.pre_run_error(project) is not None
        assert not queued_v1_run_authorizes_action_lifecycle(
            project, changed, run_id=body["run_id"]
        )
        changed = copy.deepcopy(job.payload)
        changed["spec"]["deferred_publication"] = False
        assert queued_v1_payload_envelope(changed) is None
        drain_queue(client)
        finished = queue.get(body["job_id"])
        assert finished.status == "done", finished


def test_paid_cluster_stale_source_receipt_preserves_actual_cost(
    cluster_project, monkeypatch
):
    from test_cluster_values_executor import _remote_embedding_router

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project, sheet, source, execute = cluster_project
    router, adapter = _remote_embedding_router()
    quote = execute(router=router, options={"method": "semantic"})
    assert quote.status == "needs_confirmation", quote.model_dump()

    def change_source():
        replace_test_source_cells(
            project,
            ((row_id, source, "Changed") for row_id in project.visible_row_ids(sheet)),
        )

    adapter.on_embed = change_source
    result = execute(
        router=router,
        options={"method": "semantic"},
        confirmation=quote.errors[0].details["promise_set_hash"],
    )
    assert result.status == "failed", result.model_dump()
    assert len(adapter.calls) == 1
    assert (
        project.db.execute(
            "SELECT sum(provider_cost_usd) FROM model_calls WHERE run_id=?",
            (result.run_id,),
        ).fetchone()[0]
        == adapter.COST_USD
    )
    assert (
        project.db.execute("SELECT count(*) FROM cell_result_heads").fetchone()[0] == 0
    )
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert (
        sum(item.get("cost_actual", 0) or 0 for item in receipt.provider_use)
        == adapter.COST_USD
    ), receipt.model_dump()


def test_cluster_ambiguous_remote_attempt_has_durable_retry_fence(
    cluster_project, monkeypatch
):
    from test_cluster_values_executor import _ambiguous_remote_embedding_router
    from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    project, sheet, source, execute = cluster_project
    router, adapter = _ambiguous_remote_embedding_router()
    quote = execute(router=router, options={"method": "semantic"})
    assert quote.status == "needs_confirmation"
    confirmation = quote.errors[0].details["promise_set_hash"]
    result = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmation
    )
    assert result.status == "failed", result.model_dump()
    assert len(adapter.calls) == 1
    checkpoint = project.db.execute(
        "SELECT id, state FROM effect_checkpoints WHERE state='reserved'"
    ).fetchone()
    assert checkpoint is not None, result.model_dump()
    replay = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmation
    )
    assert replay.status == "failed", replay.model_dump()
    assert len(adapter.calls) == 1
    assert replay.receipt_id == result.receipt_id
    assert replay.errors[0].code == "idempotency_checkpoint_ambiguous"
    EffectCheckpointStore(project.db).operator_accept_charged(checkpoint["id"])
    decided = execute(
        router=router, options={"method": "semantic"}, confirmation=confirmation
    )
    assert decided.status == "failed", decided.model_dump()
    assert decided.errors[0].code == "idempotency_checkpoint_operator_decided"
    assert decided.receipt_id == result.receipt_id
    assert len(adapter.calls) == 1
    assert (
        project.db.execute("SELECT count(*) FROM cell_result_heads").fetchone()[0] == 0
    )
    assert (
        EffectCheckpointStore(project.db).get(checkpoint["id"])["state"] == "consumed"
    )

"""Typed translation survives prepared-worker and receipt-repair boundaries."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs import runs
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.runner import MapRunner, validation
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.ops import builtin
from frisket.server.app import create_app
from test_model_rows_actions import _DataAdapter


class ProcessDeath(BaseException):
    pass


def test_prepared_translate_retry_recovery_and_backfill_keep_typed_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    legacy_lookup = builtin.get_recipe

    def reject_translation_recipe(kind):
        assert kind != "map.translate", "typed recovery must not look up a recipe"
        return legacy_lookup(kind)

    monkeypatch.setattr(builtin, "get_recipe", reject_translation_recipe)
    monkeypatch.setattr(validation, "get_recipe", reject_translation_recipe)
    requests = []
    router = ModelRouter(
        keys={"anthropic": "fixture"},
        use_env_keys=False,
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = _DataAdapter(
        requests, {"translation": "Hello", "detected_language": "es"}
    )
    with TestClient(create_app(tmp_path / "ws", router=router)) as client:
        project_id = client.post("/api/projects", json={"name": "Recovery"}).json()[
            "id"
        ]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        sheet = project.add_sheet("Text")
        source = project.add_column(sheet, "body")
        [row] = project.add_rows(sheet, [{"body": "Hola"}], {"body": source})
        endpoint = f"/api/projects/{project_id}/actions/v1/run"
        body = {
            "action_id": "map.translate",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["body"],
                "engine": "llm",
                "model": "anthropic/claude-haiku-4-5",
                "target_language": "en",
                "save_detected_language": True,
            },
            "output_names": {"translation": "English", "detected_language": "Language"},
            "idempotency_key": "translate-recovery",
        }
        challenge = client.post(endpoint, json=body).json()
        assert challenge["status"] == "needs_confirmation"
        assert requests == []
        body["confirmation"] = challenge["errors"][0]["details"]["promise_set_hash"]
        queued = client.post(endpoint, json=body).json()
        assert queued["status"] == "queued", queued
        claim_token = f"output-claim:{queued['receipt_id']}"
        frozen = OutputColumnClaimStore(project).active_output_fields(
            claim_token=claim_token,
            run_id=queued["run_id"],
            require_frozen_descriptors=True,
        )
        assert {field["name"] for field in frozen} == {"English", "Language"}
        job = workspace.queue.get(queued["job_id"])
        payload = {**deepcopy(job.payload), "job_id": job.id}
        handler = workspace.registry.get("project.run")
        context = JobHandlerContext.from_claimed_job(trusted_org_id=None)

        async def before_dispatch(*args, **kwargs):
            raise ProcessDeath()

        with monkeypatch.context() as patch:
            patch.setattr(MapRunner, "run", before_dispatch)
            with pytest.raises(ProcessDeath):
                handler(deepcopy(payload), context)
        assert requests == []
        assert (
            OutputColumnClaimStore(project).active_output_fields(
                claim_token=claim_token,
                run_id=queued["run_id"],
                require_frozen_descriptors=True,
            )
            == frozen
        )
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0

        def before_receipt(*args, **kwargs):
            raise ProcessDeath()

        with monkeypatch.context() as patch:
            patch.setattr(runs, "queued_v1_finalize_action_result", before_receipt)
            with pytest.raises(ProcessDeath):
                handler(deepcopy(payload), context)
        assert len(requests) == 1
        # The worker has completed its generation, so retry only repairs the
        # receipt. It must not repurchase translation or resolve a legacy recipe.
        assert handler(deepcopy(payload), context)["skipped"] is True
        receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        assert receipt.status == "completed"
        assert len(requests) == 1
        assert set(requests[0].schema["properties"]) == {
            "translation",
            "detected_language",
        }
        assert {output.name: output.ref["role"] for output in receipt.outputs} == {
            "English": "translation",
            "Language": "detected_language",
        }
        columns = {c["name"]: c["id"] for c in project.columns(sheet)}
        assert set(columns) == {"body", "English", "Language"}
        assert project.get_values(sheet, columns["English"]) == {row: "Hello"}
        assert project.get_values(sheet, columns["Language"]) == {row: "es"}

        [new_row] = project.add_rows(sheet, [{"body": "Buenos días"}], {"body": source})
        backfill = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"column": "English"},
            "idempotency_key": "translate-backfill",
        }
        challenge = client.post(endpoint, json=backfill).json()
        assert challenge["status"] == "needs_confirmation", challenge
        assert len(requests) == 1
        backfill["confirmation"] = challenge["errors"][0]["details"]["promise_set_hash"]
        completed = client.post(endpoint, json=backfill).json()
        assert completed["status"] == "completed", completed
        assert completed["run_id"] != queued["run_id"]
        assert len(requests) == 2
        assert requests[1].schema == requests[0].schema
        assert {c["name"]: c["id"] for c in project.columns(sheet)} == columns
        assert project.get_values(sheet, columns["English"]) == {
            row: "Hello",
            new_row: "Hello",
        }
        assert project.get_values(sheet, columns["Language"]) == {
            row: "es",
            new_row: "es",
        }
        replay = client.post(endpoint, json=backfill).json()
        assert replay["receipt_id"] == completed["receipt_id"]
        assert len(requests) == 2

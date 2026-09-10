"""Public routed backfill preserves typed identity and immutable output bindings."""

import json

import httpx
import pytest

from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import FAILURE_OUTCOMES
from http_test_helpers import drain_queue
from tests.server.test_census_action import census_client  # noqa: F401
from tests.server.test_geospatial_receipt_redelivery import geocode_client  # noqa: F401


def _execute(client, url, body):
    response = client.post(url, json=body)
    if response.status_code == 402:
        body["confirmation"] = response.json()["errors"][0]["details"][
            "promise_set_hash"
        ]
        response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    if result["status"] == "queued":
        drain_queue(client)
    else:
        assert result["status"] == "completed", response.text
    return result


@pytest.mark.parametrize("fixture_name", ["geocode_client", "census_client"])
def test_routed_run_backfills_repeatedly_with_original_output_ids(
    request, monkeypatch, fixture_name
):
    client, project_id, project, sheet_id, calls, body = request.getfixturevalue(
        fixture_name
    )
    census = fixture_name == "census_client"
    output_name = "demo_population" if census else "geo_point"
    body["output_names"] = {output_name: output_name}
    url = f"/api/projects/{project_id}/actions/v1/run"
    with monkeypatch.context() as failing:

        async def fail(self, url, **kwargs):
            calls.append(httpx.Request("POST" if census else "GET", url))
            return (
                httpx.Response(200, text="")
                if census
                else httpx.Response(200, json={"results": []})
            )

        failing.setattr(httpx.AsyncClient, "post" if census else "get", fail)
        original = _execute(client, url, body)
    row_ids = [
        row[0]
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY id", (sheet_id,)
        )
    ]
    store = ResultGenerationStore(project)
    output = next(
        col for col in project.columns(sheet_id) if col["name"] == output_name
    )
    output_id = int(output["id"])
    if census:
        assert all(
            head.outcome in FAILURE_OUTCOMES
            for head in store.read_cell_heads(output_id).values()
        )
    original_ids = {col["id"] for col in project.columns(sheet_id)}
    # The canonical names are descriptive; durable generation bindings own identity.
    project.db.execute(
        "UPDATE columns SET name=? WHERE id=?", ("renamed_output", output_id)
    )
    project.db.commit()
    previous_run = original["run_id"]
    for attempt in range(2):
        retry = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
            "params": {"column": "renamed_output"},
            "idempotency_key": f"routed-backfill-{attempt}",
        }
        before_calls = len(calls)
        result = _execute(client, url, retry)
        assert result["run_id"] not in (None, previous_run)
        assert len(calls) == before_calls + (2 if census else len(row_ids))
        heads = store.read_cell_heads(output_id)
        assert set(heads) == set(row_ids)
        assert all(
            head.run_id == result["run_id"] and head.value is not None
            for head in heads.values()
        )
        assert {col["id"] for col in project.columns(sheet_id)} == original_ids
        stored = json.loads(
            project.db.execute(
                "SELECT params FROM runs WHERE id=?", (result["run_id"],)
            ).fetchone()[0]
        )
        assert stored["replace_existing"] is True
        assert stored["overwrite"] is True
        assert stored["output_names"][output_name] == "renamed_output"
        assert stored["output_target_preconditions"]["renamed_output"] == output_id
        fact_count = project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[
            0
        ]
        replay = client.post(url, json=retry)
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_id"] == result["receipt_id"]
        assert len(calls) == before_calls + (2 if census else len(row_ids))
        assert (
            project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
            == fact_count
        )
        previous_run = result["run_id"]
    # Neither a successor receipt nor canonical output names authorize tampered Params.
    stored["params"]["source"] = "different_source"
    project.db.execute(
        "UPDATE runs SET params=? WHERE id=?", (json.dumps(stored), previous_run)
    )
    project.db.commit()
    before_calls = len(calls)
    refused = client.post(url, json={**retry, "idempotency_key": "tampered-successor"})
    assert refused.status_code == 400, refused.text
    assert refused.json()["errors"][0]["code"] == "invalid_run_params"
    assert len(calls) == before_calls


def test_routed_backfill_changed_scope_requires_fresh_confirmation(
    request, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client, project_id, project, sheet_id, calls, body = request.getfixturevalue(
        "geocode_client"
    )
    body["output_names"] = {"geo_point": "location"}
    url = f"/api/projects/{project_id}/actions/v1/run"
    _execute(client, url, body)
    source_id = next(
        col["id"] for col in project.columns(sheet_id) if col["name"] == "address"
    )
    project.add_rows(
        sheet_id, [{"address": "A newly added location"}], {"address": source_id}
    )
    retry = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "location"},
        "idempotency_key": "changed-geocode-scope",
    }
    before = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "receipts", "model_calls")
    }
    response = client.post(url, json=retry)
    assert response.status_code == 402, response.text
    fresh_hash = response.json()["errors"][0]["details"]["promise_set_hash"]
    assert fresh_hash != body["confirmation"]
    stale = client.post(url, json={**retry, "confirmation": body["confirmation"]})
    assert stale.status_code == 402, stale.text
    assert len(calls) == 1
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    } == before
    result = _execute(client, url, {**retry, "confirmation": fresh_hash})
    assert result["status"] == "completed"
    assert len(calls) == 2

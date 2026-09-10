"""Routed receipts reconstruct from completed runs without repeating effects."""

from copy import deepcopy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from http_test_helpers import drain_queue
from tests.server.test_census_action import census_client  # noqa: F401


@pytest.fixture
def geocode_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-owned-geocode-key")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"geometry": {"lat": 51.75, "lng": -1.25}, "formatted": "Oxford"}
                ]
            },
        )

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(
        create_app(tmp_path / "workspace", router=router, run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post("/api/projects", json={"name": "Geocode"}).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Places")
        address = project.add_column(sheet_id, "address", type="text")
        project.add_rows(sheet_id, [{"address": "Oxford"}], {"address": address})
        body = {
            "action_id": "enrich.geocode",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "address"},
            "idempotency_key": "geocode-redelivery",
        }
        yield client, project_id, project, sheet_id, calls, body


@pytest.mark.parametrize("fixture_name", ["geocode_client", "census_client"])
def test_completed_queue_redelivery_preserves_actual_provider_summary(
    request, fixture_name
):
    client, project_id, project, _, calls, body = request.getfixturevalue(fixture_name)
    url = f"/api/projects/{project_id}/actions/v1/run"
    # These are free external effects, covered by standing consent at or below
    # the threshold, so they queue without a 402 challenge. The crash, replay
    # and accounting assertions below are what this file owns.
    queued = client.post(url, json=body).json()
    assert queued["status"] == "queued"
    drain_queue(client)
    receipts = ReceiptStore(project)
    before = receipts.parsed_by_id(queued["receipt_id"])
    assert before.status == "completed"
    assert sum(item["request_count"] for item in before.provider_use) == len(calls)
    facts_before = [
        dict(row) for row in project.db.execute("SELECT * FROM model_calls").fetchall()
    ]
    cost_before = project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (queued["run_id"],)
    ).fetchone()[0]
    ws = client.app.state.workspace
    job = ws.queue.get(queued["job_id"])
    result = ws.registry.get("project.run")(
        deepcopy(job.payload), JobHandlerContext.from_claimed_job(trusted_org_id=None)
    )
    assert result["skipped"] is True
    after = receipts.parsed_by_id(queued["receipt_id"])
    assert after.provider_use == before.provider_use
    assert len(calls) == sum(item["request_count"] for item in before.provider_use)
    assert [
        dict(row) for row in project.db.execute("SELECT * FROM model_calls").fetchall()
    ] == facts_before
    assert (
        project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (queued["run_id"],)
        ).fetchone()[0]
        == cost_before
    )


@pytest.mark.parametrize("fixture_name", ["geocode_client", "census_client"])
def test_crash_after_run_finish_before_first_receipt_recovers_actual_facts(
    request, monkeypatch, fixture_name
):
    from frisket.engine.jobs import runs

    class ProcessDeath(BaseException):
        pass

    client, project_id, project, _, calls, body = request.getfixturevalue(fixture_name)
    url = f"/api/projects/{project_id}/actions/v1/run"
    # Free external effect: covered by standing consent, so no 402 challenge.
    queued = client.post(url, json=body).json()
    ws = client.app.state.workspace
    job = ws.queue.get(queued["job_id"])
    payload = {**deepcopy(job.payload), "job_id": job.id}
    handler = ws.registry.get("project.run")
    context = JobHandlerContext.from_claimed_job(trusted_org_id=None)
    original = runs.queued_v1_finalize_action_result

    def crash_before_finalizing(*args, **kwargs):
        raise ProcessDeath()

    monkeypatch.setattr(
        runs, "queued_v1_finalize_action_result", crash_before_finalizing
    )
    with pytest.raises(ProcessDeath):
        handler(deepcopy(payload), context)
    assert (
        project.db.execute(
            "SELECT status FROM runs WHERE id=?", (queued["run_id"],)
        ).fetchone()[0]
        == "completed"
    )
    assert ReceiptStore(project).parsed_by_id(queued["receipt_id"]).status == "queued"
    fact_ids = [
        row[0]
        for row in project.db.execute(
            "SELECT id FROM model_calls ORDER BY id"
        ).fetchall()
    ]
    calls_before = len(calls)
    monkeypatch.setattr(runs, "queued_v1_finalize_action_result", original)
    assert handler(deepcopy(payload), context)["skipped"] is True
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "completed"
    assert sum(item["request_count"] for item in receipt.provider_use) == calls_before
    assert {item["provider"] for item in receipt.provider_use} == {
        ("opencage" if fixture_name == "geocode_client" else "us_census_acs")
    }
    if fixture_name == "census_client":
        assert {item["credential_source"] for item in receipt.provider_use} == {
            "none",
            "local",
        }
        assert sum(item["model_call_count"] for item in receipt.provider_use) == len(
            fact_ids
        )
        for provider in receipt.provider_use:
            assert provider["cost_actual"] == 0
            assert provider["external_api"] is True
            assert {
                key: provider[key] for key in ("dataset", "vintage", "geography")
            } == {"dataset": "acs/acs5", "vintage": "2024", "geography": "tract"}
    assert len(calls) == calls_before
    assert [
        row[0]
        for row in project.db.execute(
            "SELECT id FROM model_calls ORDER BY id"
        ).fetchall()
    ] == fact_ids


@pytest.mark.parametrize(
    ("fixture_name", "scenario"),
    [
        ("geocode_client", "blank"),
        ("census_client", "blank"),
        ("census_client", "no_geographies"),
        ("census_client", "http_failure"),
        ("geocode_client", "transport"),
        ("geocode_client", "free_transport"),
        ("geocode_client", "retry_transport"),
        ("geocode_client", "retry_success"),
    ],
)
def test_recovered_summary_counts_actual_http_without_inventing_charges(
    request, monkeypatch, fixture_name, scenario
):
    from frisket.engine.executor import geocode_capability

    client, project_id, project, sheet_id, calls, body = request.getfixturevalue(
        fixture_name
    )
    if scenario == "blank":
        project.add_column(
            sheet_id,
            "empty",
            type="geo_point" if fixture_name == "census_client" else "text",
        )
        body["params"]["source"] = "empty"
    elif fixture_name == "census_client":

        async def census_post(self, url, **kwargs):
            calls.append(httpx.Request("POST", url))
            return httpx.Response(500 if scenario == "http_failure" else 200, text="")

        monkeypatch.setattr(httpx.AsyncClient, "post", census_post)
    else:
        if scenario == "free_transport":
            body["params"]["engine"] = "nominatim"

        async def no_wait(*args):
            pass

        monkeypatch.setattr(geocode_capability, "_nominatim_pace", no_wait)
        monkeypatch.setattr(geocode_capability.asyncio, "sleep", no_wait)

        async def geocode_get(self, url, **kwargs):
            calls.append(httpx.Request("GET", url))
            if scenario.startswith("retry_") and len(calls) == 1:
                return httpx.Response(429, text="retry")
            if scenario == "retry_success":
                return httpx.Response(
                    200, json={"results": [{"geometry": {"lat": 1, "lng": 2}}]}
                )
            raise httpx.ConnectError("transport outcome unknown")

        monkeypatch.setattr(httpx.AsyncClient, "get", geocode_get)
    url = f"/api/projects/{project_id}/actions/v1/run"
    # These are free external effects, covered by standing consent at or below
    # the threshold, so they queue without a 402 challenge. The crash, replay
    # and accounting assertions below are what this file owns.
    queued = client.post(url, json=body).json()
    assert queued["status"] == "queued"
    drain_queue(client)
    receipts = ReceiptStore(project)
    before = receipts.parsed_by_id(queued["receipt_id"])
    assert before.status in {"completed", "failed"}
    assert sum(item["request_count"] for item in before.provider_use) == len(calls)
    facts_before = [
        dict(row)
        for row in project.db.execute(
            "SELECT * FROM model_calls ORDER BY id"
        ).fetchall()
    ]
    assert sum(
        json.loads(fact["units"]).get("requests", 0) for fact in facts_before
    ) == len(calls)
    expected_unknown = scenario in {"transport", "retry_transport", "retry_success"}
    cost = project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (queued["run_id"],)
    ).fetchone()[0]
    assert cost is None if expected_unknown else cost == 0
    if scenario == "blank":
        assert not facts_before
        assert before.provider_use == []
    if fixture_name == "census_client" and scenario != "blank":
        assert all(fact["row_id"] is None for fact in facts_before)
        assert before.provider_use[0]["geography"] == "tract"
    ws = client.app.state.workspace
    job = ws.queue.get(queued["job_id"])
    for _ in range(2):
        result = ws.registry.get("project.run")(
            deepcopy(job.payload),
            JobHandlerContext.from_claimed_job(trusted_org_id=None),
        )
        assert result["skipped"] is True
        assert (
            receipts.parsed_by_id(queued["receipt_id"]).provider_use
            == before.provider_use
        )
    assert len(calls) == sum(item["request_count"] for item in before.provider_use)
    assert [
        dict(row)
        for row in project.db.execute(
            "SELECT * FROM model_calls ORDER BY id"
        ).fetchall()
    ] == facts_before

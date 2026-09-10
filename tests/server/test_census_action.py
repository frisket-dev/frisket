"""Public Census admission keeps key, consent, preview and queued replay gates."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from http_test_helpers import drain_queue

FIXTURES = Path(__file__).parent.parent / "fixtures" / "census"


@pytest.fixture
def census_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-public-key")
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, text=(FIXTURES / "coordinatesbatch_dedupe.csv").read_text()
            )
        assert request.url.params["key"] == "test-public-key"
        return httpx.Response(
            200, json=json.loads((FIXTURES / "acs_2024_dc_tracts.json").read_text())
        )

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=router,
            run_status_grace_seconds=3600,
        )
    )
    project_id = client.post("/api/projects", json={"name": "Census"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    point = project.add_column(sheet_id, "point", type="geo_point")
    project.add_rows(
        sheet_id,
        [
            {"point": {"lat": 38.9, "lon": -77.03}},
            {"point": {"lat": 38.9, "lon": -77.03}},
            {"point": {"lat": 38.91, "lon": -77.04}},
        ],
        {"point": point},
    )
    body = {
        "action_id": "enrich.census_demographics",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "point"},
        "idempotency_key": "public-census@1",
    }
    yield client, project_id, project, sheet_id, calls, body
    client.close()


def test_queue_executes_and_replays_without_more_http(census_client):
    """The Census API is free, and standing consent at or below the threshold
    covers provider data transfer, so this queues without a 402 challenge.
    What this test owns is the accounting and replay half: honest per-source
    provider_use, and a repeat request that returns the same receipt without
    another HTTP call."""

    client, project_id, project, sheet_id, calls, body = census_client
    url = f"/api/projects/{project_id}/actions/v1/run"
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert not calls  # queueing must not reach the provider
    queued = response.json()
    assert queued["status"] == "queued"
    drain_queue(client)
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "completed"
    providers = {item["credential_source"]: item for item in receipt.provider_use}
    assert len(receipt.provider_use) == len(providers) == 2
    assert set(providers) == {"none", "local"}
    for source, provider in providers.items():
        assert provider["provider"] == "us_census_acs"
        assert provider["request_count"] == 1
        assert provider["model_call_count"] == (1 if source == "none" else 4)
        assert provider["cost_actual"] == 0
        assert provider["external_api"] is True
        assert provider["dataset"] == "acs/acs5"
        assert provider["vintage"] == "2024"
        assert provider["geography"] == "tract"
    assert sum(item["request_count"] for item in receipt.provider_use) == 2
    assert len(project.columns(sheet_id)) == 20
    assert [request.method for request in calls] == ["POST", "GET"]
    replay = client.post(url, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == queued["receipt_id"]
    assert (
        ReceiptStore(project).parsed_by_id(queued["receipt_id"]).provider_use
        == receipt.provider_use
    )
    assert len(calls) == 2


def test_missing_key_refuses_before_queue_and_egress(census_client, monkeypatch):
    client, project_id, project, sheet_id, calls, body = census_client
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code >= 400, response.text
    assert "CENSUS_API_KEY" in response.text
    assert not calls
    assert len(project.columns(sheet_id)) == 1
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_preview_of_free_external_effects_is_write_free(census_client):
    """A free external effect is covered by standing consent, so preview no
    longer 402s. It must still be write-free and must not reach the provider:
    previewing is not a licence to spend the effect early."""

    client, project_id, project, sheet_id, calls, body = census_client
    response = client.post(f"/api/projects/{project_id}/actions/v1/preview", json=body)
    assert response.status_code == 202, response.text
    assert not calls
    assert len(project.columns(sheet_id)) == 1

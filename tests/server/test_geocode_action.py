"""Public typed Geocode queue preserves exact consent and replay authority."""

import httpx
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from http_test_helpers import drain_queue


def test_geocode_queue_and_replay(tmp_path, monkeypatch):
    """Queue, execute, account and replay a free external enrichment.

    Nominatim geocoding is free, and standing consent at or below the
    threshold covers provider data transfer, so this workflow no longer
    stops at a 402 challenge. The confirmation SECURITY property is proved
    by the priced and unknown-cost tests, not here; what this test owns is
    that the queued run writes nothing before it executes, records honest
    provider accounting, and replays idempotently.
    """

    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    from frisket.engine.executor import geocode_capability

    async def no_pace():
        pass

    monkeypatch.setattr(geocode_capability, "_nominatim_pace", no_pace)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=[{"lat": "51.75", "lon": "-1.25", "display_name": "Oxford"}]
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
            "idempotency_key": "public-geocode",
        }
        url = f"/api/projects/{project_id}/actions/v1/run"
        response = client.post(url, json=body)
        assert response.status_code == 200, response.text
        queued = response.json()
        assert queued["status"] == "queued"
        # Still the safety half of the old challenge: queueing declares the
        # output columns but must not reach the provider before the run
        # executes. (The old `columns == 1` check belonged to the refused
        # state, where nothing had been declared yet.)
        assert not calls
        drain_queue(client)
        receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        assert receipt.status == "completed"
        assert receipt.provider_use[0]["provider"] == "nominatim"
        assert receipt.provider_use[0]["request_count"] == 1
        assert len(project.columns(sheet_id)) == 3
        assert client.post(url, json=body).json()["receipt_id"] == receipt.receipt_id
        assert len(calls) == 1

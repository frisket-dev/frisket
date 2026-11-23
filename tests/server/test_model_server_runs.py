"""A run on a model server's model: unknown price asks, entered price binds.

A model on a server off this machine has no known price until the person
enters one. The run asks before it starts, even within an allowance, and sends
nothing until it is confirmed. An entered price is the estimate's rate, the
consent binds it by value, and the model call's cost uses it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from frisket.ai.llm import adapters as llm_adapters
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig, ModelServerModel
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMRequest
from frisket.execution.dispatch_control import STANDALONE
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server import provider_config
from helpers import make_client
from http_test_helpers import confirmation_hash_from_action_result

REMOTE_ORIGIN = "http://192.168.1.20:8000"
LOOPBACK_ORIGIN = "http://127.0.0.1:11434"


def _probe(origin: str, **_kwargs: Any) -> dict[str, Any]:
    return {
        "reachable": True,
        "status": 200,
        "models": ["qwen3:8b"] if origin == LOOPBACK_ORIGIN else ["listed-model"],
        "detail": None,
        "protocol": "openai_compatible",
        "auth_status": "ok",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "ollama_reachable", _probe)
    return make_client(tmp_path)


def _add_server(client, name: str, origin: str) -> dict[str, Any]:
    created = client.post(
        "/api/providers/local-endpoints",
        json={"display_name": name, "origin": origin},
    )
    assert created.status_code == 200, created.text
    return next(
        row
        for row in created.json()["providers"]
        if row["kind"] == "local_http" and row["origin"] == origin
    )


def _classify_action(sheet_id: int, model: str, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["snippet"],
            "engine": "llm",
            "model": model,
            "context": "Classify each row.",
            "fields": [{"name": "topic", "type": "text"}],
        },
        "idempotency_key": f"{key}@sha256:test",
    }


@pytest.fixture
def no_model_requests(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def refuse(self, req, client, **_kwargs):
        sent.append(req.model)
        raise AssertionError("a model request was sent")

    monkeypatch.setattr(llm_adapters.OpenAICompatAdapter, "complete", refuse)
    return sent


def _project_with_rows(client) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "Runs"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("stories")
    cols = {"snippet": project.add_column(sheet, "snippet", type="text")}
    project.add_rows(sheet, [{"snippet": "first"}, {"snippet": "second"}], cols)
    return project_id, sheet


def test_unknown_price_asks_even_within_an_allowance_and_sends_nothing(
    client, monkeypatch, no_model_requests
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "100")
    remote = _add_server(client, "LAN vLLM", REMOTE_ORIGIN)
    project_id, sheet = _project_with_rows(client)
    model = f"ollama/@{remote['endpoint_id']}/listed-model"

    gated = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_classify_action(sheet, model, "unknown-price"),
    )

    assert gated.status_code == 402, gated.text
    gate = gated.json()["errors"][0]
    assert gate["needs_confirmation"] is True
    estimate = gate["details"]["estimate"]
    assert estimate["cost"] is None
    assert estimate["cost_source"] == "unknown"
    assert no_model_requests == []


def test_loopback_server_runs_without_asking(
    client, monkeypatch, no_model_requests
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    loopback = _add_server(client, "Desk Ollama", LOOPBACK_ORIGIN)
    project_id, sheet = _project_with_rows(client)
    model = f"ollama/@{loopback['endpoint_id']}/qwen3:8b"

    estimated = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={"action": _classify_action(sheet, model, "loopback")},
    )
    assert estimated.status_code == 200, estimated.text
    assert estimated.json()["estimate"]["cost"] == 0.0
    assert estimated.json()["estimate"]["cost_source"] == "free_local"


def test_entered_price_is_the_estimate_and_consent_binds_it(
    client, monkeypatch, no_model_requests
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    remote = _add_server(client, "LAN vLLM", REMOTE_ORIGIN)
    endpoint_id = remote["endpoint_id"]
    priced = client.patch(
        f"/api/providers/local-endpoints/{endpoint_id}",
        json={
            "models": [
                {
                    "name": "listed-model",
                    "entered": False,
                    "price": {"input": 2.5, "output": 10},
                }
            ]
        },
    )
    assert priced.status_code == 200, priced.text
    project_id, sheet = _project_with_rows(client)
    model = f"ollama/@{endpoint_id}/listed-model"
    action = _classify_action(sheet, model, "entered-price")

    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert gated.status_code == 402, gated.text
    estimate = gated.json()["errors"][0]["details"]["estimate"]
    assert estimate["cost"] is not None and estimate["cost"] > 0
    assert estimate["cost_source"] == "pricing_data"
    assert estimate["pricing_key"] == f"{model}.tokens:2.5/10.0"
    consent = confirmation_hash_from_action_result(gated.json())
    assert consent is not None

    # The consent is to that price. A changed price is a different estimate:
    # echoing the old consent asks again instead of running.
    repriced = client.patch(
        f"/api/providers/local-endpoints/{endpoint_id}",
        json={
            "models": [
                {
                    "name": "listed-model",
                    "entered": False,
                    "price": {"input": 2.5, "output": 10.000001},
                }
            ]
        },
    )
    assert repriced.status_code == 200, repriced.text
    stale = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={**action, "confirmation": consent},
    )
    assert stale.status_code == 402, stale.text
    restated = stale.json()["errors"][0]["details"]["estimate"]
    assert restated["pricing_key"] == f"{model}.tokens:2.5/10.000001"
    assert no_model_requests == []


def test_entered_price_within_the_allowance_starts_without_asking(
    client, monkeypatch, no_model_requests
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "100")
    remote = _add_server(client, "LAN vLLM", REMOTE_ORIGIN)
    priced = client.patch(
        f"/api/providers/local-endpoints/{remote['endpoint_id']}",
        json={
            "models": [
                {
                    "name": "listed-model",
                    "entered": False,
                    "price": {"input": 0.1, "output": 0.1},
                }
            ]
        },
    )
    assert priced.status_code == 200, priced.text
    project_id, sheet = _project_with_rows(client)
    model = f"ollama/@{remote['endpoint_id']}/listed-model"

    started = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_classify_action(sheet, model, "allowance"),
    )

    assert started.status_code in {200, 202}, started.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "models", "cost", "cost_source"),
    [
        (LOOPBACK_ORIGIN, (), 0.0, "free_local"),
        (REMOTE_ORIGIN, (), None, "unknown"),
        (REMOTE_ORIGIN, ((2.0, 8.0),), 10.0, "pricing_data"),
    ],
)
async def test_model_call_cost_follows_the_server_origin_and_entered_price(
    origin: str,
    models: tuple[tuple[float, float], ...],
    cost: float | None,
    cost_source: str,
) -> None:
    endpoint = LocalModelEndpointConfig(
        endpoint_id="receipt-server",
        display_name="Receipt server",
        origin=origin,
        source="local_file",
        models=tuple(
            ModelServerModel(name="served", entered=True, price=price)
            for price in models
        ),
    )
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        use_env_keys=False,
        local_endpoints=(endpoint,),
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            },
        )

    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        response = await router.for_execution(STANDALONE).complete(
            LLMRequest(
                model="ollama/@receipt-server/served",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
    finally:
        await router._client.aclose()

    assert response.cost == cost
    assert response.cost_source == cost_source

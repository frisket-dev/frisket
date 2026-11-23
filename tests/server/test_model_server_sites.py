"""Every quote and spend site prices a model-server model from its workspace.

A loopback server's model estimates free and starts without asking; a server
off this machine with no entered price asks. Each site below reads the
workspace's configured servers, never a default that would make every
model-server model unknown or free.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from frisket.ai.llm import adapters as llm_adapters
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.pricing import cost_with_pricing, model_pricing
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.server import provider_config
from frisket.server.services.project_qa_research import model_estimate_micros
from helpers import make_client
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation

REMOTE_ORIGIN = "http://192.168.1.20:8000"
LOOPBACK_ORIGIN = "http://127.0.0.1:11434"


def _probe(origin: str, **_kwargs: Any) -> dict[str, Any]:
    return {
        "reachable": True,
        "status": 200,
        "models": ["served"],
        "detail": None,
        "protocol": "openai_compatible",
        "auth_status": "ok",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "ollama_reachable", _probe)
    return make_client(tmp_path)


def _model(client, origin: str) -> str:
    created = client.post(
        "/api/providers/local-endpoints",
        json={"display_name": f"Server {origin}", "origin": origin},
    )
    assert created.status_code == 200, created.text
    endpoint = next(
        row
        for row in created.json()["providers"]
        if row["kind"] == "local_http" and row["origin"] == origin
    )
    return f"ollama/@{endpoint['endpoint_id']}/served"


def _project(client) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "Sites"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("stories")
    columns = {
        "story": project.add_column(sheet, "story", type="text"),
        "desk": project.add_column(sheet, "desk", type="text"),
    }
    project.add_rows(
        sheet,
        [
            {"story": "Council approves contract.", "desk": "news"},
            {"story": "Road closes for repairs.", "desk": "news"},
        ],
        columns,
    )
    return project_id, sheet


def _group_summary(sheet: int, model: str, key: str) -> dict[str, Any]:
    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "sheet_name": f"Summaries {key}",
        "params": {"source": ["story"], "model": model, "instruction": "Summarize."},
        "idempotency_key": f"{key}@sha256:test",
    }


def _find(sheet: int, model: str, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.find",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "sheet_name": f"Findings {key}",
        "params": {
            "source": "story",
            "instruction": "Find every discussion of public contracts.",
            "fields": [],
            "model": model,
        },
        "idempotency_key": f"{key}@sha256:test",
    }


@pytest.mark.parametrize("action", [_group_summary, _find])
def test_grouped_summary_and_find_quote_from_the_workspace_servers(
    client, monkeypatch, action
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "5")
    loopback = _model(client, LOOPBACK_ORIGIN)
    remote = _model(client, REMOTE_ORIGIN)
    project_id, sheet = _project(client)

    def estimate(model: str, key: str) -> dict[str, Any]:
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/estimate",
            json={"action": action(sheet, model, key)},
        )
        assert response.status_code == 200, response.text
        return response.json()["estimate"]

    free = estimate(loopback, "loopback")
    assert (free["cost"], free["cost_source"]) == (0.0, "free_local")
    assert free["requires_confirmation"] is False
    unknown = estimate(remote, "remote")
    assert (unknown["cost"], unknown["cost_source"]) == (None, "unknown")
    assert unknown["requires_confirmation"] is True


def test_grouped_summary_run_on_a_remote_server_asks(client, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "5")
    remote = _model(client, REMOTE_ORIGIN)
    project_id, sheet = _project(client)

    gated = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_group_summary(sheet, remote, "remote-run"),
    )

    assert gated.status_code == 402, gated.text
    assert gated.json()["errors"][0]["details"]["estimate"]["cost"] is None


def test_project_ask_research_quotes_from_the_workspace_servers() -> None:
    servers = (
        LocalModelEndpointConfig(
            endpoint_id="desk",
            display_name="Desk",
            origin=LOOPBACK_ORIGIN,
            source="local_file",
        ),
        LocalModelEndpointConfig(
            endpoint_id="lan",
            display_name="LAN",
            origin=REMOTE_ORIGIN,
            source="local_file",
        ),
    )

    def micros(model: str) -> int | None:
        return model_estimate_micros(
            LLMRequest(model=model, messages=[{"role": "user", "content": "q"}]),
            servers,
        )

    assert micros("ollama/@desk/served") == 0
    assert micros("ollama/@lan/served") is None


def _answer(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def complete(self, req, client, *, cost_pricing=None):
        sent.append(req.model)
        cost, cost_source = cost_with_pricing(
            cost_pricing or model_pricing(req.model), 10, 5
        )
        fields = (req.schema or {}).get("properties") or {"topic": {}}
        data = {name: "contracts" for name in fields}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=10,
            tokens_out=5,
            cost=cost,
            cost_source=cost_source,
            model=req.model,
        )

    monkeypatch.setattr(llm_adapters.OpenAICompatAdapter, "complete", complete)
    return sent


def _classify(sheet: int, model: str, key: str = "unattended") -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": model,
            "context": "Classify each row.",
            "fields": [{"name": f"topic_{key.replace('-', '_')}", "type": "text"}],
        },
        "idempotency_key": f"{key}@sha256:test",
    }


def test_unattended_loopback_run_within_an_allowance_runs_without_asking(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "5")
    sent = _answer(monkeypatch)
    loopback = _model(client, LOOPBACK_ORIGIN)
    project_id, sheet = _project(client)

    started = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=_classify(sheet, loopback)
    )
    assert started.status_code in {200, 202}, started.text
    run_id = started.json()["run_id"]
    drain_queue(client)

    status = client.get(f"/api/projects/{project_id}/actions/runs/{run_id}/status")
    assert status.status_code == 200, status.text
    body = status.json()
    run = body["run"]
    assert (run["status"], run["completed_rows"], run["cost_actual"]) == (
        "completed",
        2,
        0.0,
    )
    assert sent == ["ollama/served", "ollama/served"]


def test_spend_dashboard_prices_past_runs_from_the_workspace_servers(
    client, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "5")
    _answer(monkeypatch)
    loopback = _model(client, LOOPBACK_ORIGIN)
    remote = _model(client, REMOTE_ORIGIN)
    project_id, sheet = _project(client)
    for model, key in ((loopback, "spend-loopback"), (remote, "spend-remote")):
        ran = post_v1_action_with_exact_confirmation(
            client, project_id, _classify(sheet, model, key)
        )
        assert ran.status_code in {200, 202}, ran.text
        drain_queue(client)

    spend = client.get("/api/spend")
    assert spend.status_code == 200, spend.text
    assert spend.json()["unknown_cost_models"] == [remote]

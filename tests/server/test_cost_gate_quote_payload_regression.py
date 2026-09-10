"""The authoritative 402 must carry the same quote as /estimate."""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def test_action_402_preserves_full_estimate_including_row_count(
    tmp_path, monkeypatch
) -> None:
    # Make a small deterministic fixture cross the configured confirmation gate.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off"),
        )
    )
    project_id = client.post("/api/projects", json={"name": "Quote"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("stories")
    cols = {"snippet": project.add_column(sheet, "snippet", type="text")}
    project.add_rows(
        sheet,
        [{"snippet": "first"}, {"snippet": "second"}],
        cols,
    )
    action = {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": ["snippet"],
            "engine": "llm",
            "model": "anthropic/claude-opus-4-8",
            "context": "Classify each row.",
            "fields": [
                {
                    "name": "relevance",
                    "type": "score",
                    "description": "0-10 relevance",
                }
            ],
        },
        "idempotency_key": "quote-payload@sha256:test",
    }

    estimated = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={"action": action},
    )
    assert estimated.status_code == 200, estimated.text
    authoritative_quote = estimated.json()["estimate"]
    assert authoritative_quote["rows"] == 2

    gated = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=action,
    )
    assert gated.status_code == 402, gated.text
    gate = gated.json()["errors"][0]
    assert gate["needs_confirmation"] is True
    assert gate["field"] == "confirmation"
    returned_quote = gate["details"]["estimate"]

    # A retry confirmation should authorize this exact server quote, including
    # its scope and token assumptions, not a detached scalar dollar amount.
    assert returned_quote == authoritative_quote
    assert isinstance(gate["details"].get("promise_set_hash"), str)

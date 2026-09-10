"""An unrouted paid confirmation must remain inspectable after dispatch."""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.execution.attempt import run_attempt_receipts
from frisket.server.app import create_app
from http_test_helpers import drain_queue


class _PaidClassifyAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req, client):  # noqa: ANN001
        self.calls += 1
        return LLMResponse(
            content='{"relevance": 7}',
            data={"relevance": 7},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


def test_unrouted_paid_402_confirmation_is_linked_from_attempt_receipt(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    router = ModelRouter(
        keys={"anthropic": "sk-ant-test"},
        key_sources={"anthropic": "org_byok"},
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = _PaidClassifyAdapter()  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    project_id = client.post("/api/projects", json={"name": "Approval"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("stories")
    cols = {"snippet": project.add_column(sheet, "snippet", type="text")}
    project.add_rows(
        sheet,
        [{"snippet": "first"}, {"snippet": "second"}],
        cols,
    )
    gated_scope = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet,)
        ).fetchall()
    ]
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
        "idempotency_key": "unrouted-approval@sha256:test",
    }

    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]
    assert isinstance(promise_set_hash, str) and promise_set_hash

    # Consent is the exact quote token echoed at the top level of the request.
    confirmed = {**action, "confirmation": promise_set_hash}
    accepted = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert accepted.status_code == 200, accepted.text
    run_id = int(accepted.json()["run_id"])
    drain_queue(client)

    [receipt] = run_attempt_receipts(project, run_id)
    assert receipt["scope"] == gated_scope
    paid_byok_calls = project.db.execute(
        "SELECT COUNT(*) FROM model_calls WHERE run_id=? "
        "AND credential_source='org_byok' AND provider_cost_usd > 0",
        (run_id,),
    ).fetchone()[0]
    assert paid_byok_calls == 2
    assert receipt["consent"] is not None
    assert receipt["consent"]["grant_basis"] == "user_confirmation"
    assert receipt["consent"]["promise_set_hash"] == promise_set_hash

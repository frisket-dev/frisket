"""Frozen Stage-2 contract for BYOK execution on the open team server.

The provider boundary is fake; identity HTTP, project storage, the action
executor, SQLite queue, worker, and durable usage facts are production code.
Model-backed v1 actions are deliberately queued by the product placement
registry, so the synchronous half invokes the same public executor over the
project created through team HTTP rather than adding a test-only HTTP mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.ai.llm import LLMError, LLMResponse
from frisket.engine.store.runs import RunResultStore
from frisket.team.app import TeamConfig, create_team_app
from http_test_helpers import post_v1_action_with_exact_confirmation
from executor_harness import (
    run_action_with_confirmation as run_action_with_exact_confirmation,
)
from tests.team_setup_helpers import claim_server, sign_in_with_magic_link


pytestmark = pytest.mark.gap

SECRET = "sk-open-team-contract-secret"


class _ProviderAdapter:
    calls: list[str] = []
    failure: LLMError | None = None

    def __init__(self, key: str, *args: Any, **kwargs: Any) -> None:
        self.key = key
        self.base_url = str(args[0]) if args else "https://provider.invalid/v1"

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        type(self).calls.append(self.key)
        if type(self).failure is not None:
            raise type(self).failure
        properties = dict((req.schema or {}).get("properties") or {})
        field_name = next(
            (
                name
                for name in properties
                if not name.endswith(("_justification", "_confidence"))
            ),
            "beat",
        )
        value = {
            field_name: "accountability",
            f"{field_name}_justification": "The award lacked competition.",
            f"{field_name}_confidence": 0.91,
        }
        return LLMResponse(
            content=json.dumps(value),
            data=value,
            tokens_in=13,
            tokens_out=8,
            cost=0.0042,
            model=req.model,
        )


def _app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ambient_openai_key: str | None = None,
):
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    if ambient_openai_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", ambient_openai_key)
    monkeypatch.setattr("frisket.ai.llm.router.OpenAICompatAdapter", _ProviderAdapter)

    async def send(_email: str, _link: str) -> bool:
        return True

    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            # create_team_app now requires a run-queue locator
            # unconditionally (deferred follow-up from the queue-composition audit,
            # "general run-queue mismatch", completed).
            run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Open Desk",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=send,
    )
    return app


def _login(client: TestClient, app: Any) -> None:
    claim_server(app, client=client)
    sign_in_with_magic_link(app, client, "owner@example.com")


def _project(client: TestClient) -> tuple[str, int]:
    made = client.post("/api/projects", json={"name": "BYOK Evidence"})
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "stories.csv",
                b'story\n"The city awarded a no-bid contract."\n',
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    return pid, int(imported.json()["sheet_id"])


def _action(sheet_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "openai/gpt-5-mini",
            "context": "Classify the story.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "other"],
                    "description": "Editorial beat.",
                }
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": key,
    }


def _reduce_action(sheet_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "sheet_name": "Story Summaries",
        "params": {
            "source": ["story"],
            "model": "openai/gpt-5-mini",
            "instruction": "Summarize the reporting risks.",
        },
        "idempotency_key": key,
    }


def _project_state(project: Any) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("columns", "runs", "receipts", "output_column_claims")
    }


def _assert_facts(
    project: Any,
    run_id: int,
    *,
    calls: int = 1,
    source: str = "org_byok",
    provider_cost_usd: float = 0.0042,
) -> list[Any]:
    facts = RunResultStore(project).model_calls(run_id)
    assert len(facts) == calls
    for fact in facts:
        assert fact["credential_source"] == source
        assert fact["fact_version"] == "frisket.model-call-fact.v1"
        serialized = json.dumps(dict(fact), default=str).lower()
        assert SECRET not in serialized
        assert "key_hint" not in serialized
        assert fact["provider_reported_cost_usd"] == pytest.approx(provider_cost_usd)
        assert fact["provider_cost_usd"] == pytest.approx(provider_cost_usd)
    return facts


def test_http_configured_byok_drives_sync_and_queued_runtime_without_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ProviderAdapter.calls = []
    _ProviderAdapter.failure = None
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    saved = client.post("/api/org/keys", json={"provider": "openai", "key": SECRET})
    assert saved.status_code == 200, saved.text
    pid, sheet_id = _project(client)
    project = app.state.workspace.get(pid)

    sync = run_action_with_exact_confirmation(
        project,
        _action(sheet_id, "team-sync"),
        project_id=pid,
        router=app.state.workspace.router_for(project),
    )
    assert sync.status == "completed", sync.errors
    assert sync.run_id is not None
    _assert_facts(project, sync.run_id)

    estimate = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _action(sheet_id, "team-estimate")},
    )
    assert estimate.status_code == 200, estimate.text
    assert float(estimate.json()["estimate"]["cost"]) >= 0

    queued_action = _action(sheet_id, "team-queued")
    queued_action["params"]["fields"][0]["name"] = "queued_beat"
    queued = post_v1_action_with_exact_confirmation(
        client,
        pid,
        queued_action,
    )
    assert queued.status_code == 200, queued.text
    queued_body = queued.json()
    assert queued_body["status"] in {"running", "queued"}, queued_body
    run_id = int(queued_body["run_id"])
    assert Worker(app.state.workspace.queue, app.state.workspace.registry).run_once()
    status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["run"]["status"] == "completed", status.text
    _assert_facts(project, run_id)

    # Two provider actions produce two facts and two calls: queue finalization,
    # polling, and spend projection must not charge or record them again.
    assert _ProviderAdapter.calls == [SECRET, SECRET]
    all_facts = project.db.execute("SELECT * FROM model_calls").fetchall()
    assert len(all_facts) == 2
    spend = client.get("/api/spend")
    assert spend.status_code == 200, spend.text
    assert spend.json()["total_cost"] == pytest.approx(0.0084)


def test_open_control_and_project_state_have_no_commerce_or_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    pid, _ = _project(client)
    with app.state.control_engine.connect() as cx:
        control_tables = set(sa.inspect(cx).get_table_names())
    project = app.state.workspace.get(pid)
    project_tables = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    forbidden = {
        "org_billing",
        "funding_accounts",
        "funding_reservations",
        "credit_ledger",
        "billing_events",
        "stripe_events",
        "run_settlements",
    }
    assert not (control_tables & forbidden)
    assert not (project_tables & forbidden)
    assert not hasattr(app.state, "billing_service")
    assert not hasattr(app.state, "settlement_port")


def test_missing_byok_is_named_non_402_and_never_uses_ambient_platform_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ProviderAdapter.calls = []
    _ProviderAdapter.failure = None
    ambient = "sk-host-process-ambient-must-not-win"
    app = _app(tmp_path, monkeypatch, ambient_openai_key=ambient)
    client = TestClient(app)
    _login(client, app)
    pid, sheet_id = _project(client)
    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_action(sheet_id, "missing-byok"),
    )
    assert response.status_code != 402, response.text
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed", body
    error = body["errors"][0]
    assert error["code"] == "missing_provider_key", error
    assert error["details"]["provider"] == "openai", error
    assert _ProviderAdapter.calls == []
    assert ambient not in json.dumps(body)


def test_cache_replay_needs_no_key_and_records_zero_cost_cache_fact_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.ai.llm import LLMRequest, ResponseCache, request_key
    from typed_model_fixtures import model_plan

    _ProviderAdapter.calls = []
    _ProviderAdapter.failure = None
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    pid, sheet_id = _project(client)
    project = app.state.workspace.get(pid)
    action = _action(sheet_id, "team-cache-replay")
    params = action["params"]
    spec = {
        "recipe": "classify",
        "model": params["model"],
        "sheet_id": sheet_id,
        "input_columns": params["source"],
        "context": params["context"],
        "fields": params["fields"],
        "include_justification": True,
        "include_confidence": True,
    }
    recipe = model_plan(spec).program
    call = recipe.render({"story": "The city awarded a no-bid contract."}, spec)
    request = LLMRequest(
        model=params["model"],
        messages=call.messages,
        schema=call.schema,
        max_tokens=call.max_tokens,
    )
    reply = {
        "beat": "accountability",
        "beat_justification": "The award lacked competition.",
        "beat_confidence": 0.91,
    }
    cache = ResponseCache(project.path / "project.cache.db")
    cache.put(
        request_key(request, recipe.version),
        LLMResponse(
            content=json.dumps(reply),
            data=reply,
            tokens_in=13,
            tokens_out=8,
            cost=0.0042,
            model=params["model"],
        ),
    )
    cache.close()

    started = post_v1_action_with_exact_confirmation(client, pid, action)
    assert started.status_code == 200, started.text
    assert started.json()["status"] in {"queued", "running"}, started.text
    run_id = int(started.json()["run_id"])
    assert Worker(app.state.workspace.queue, app.state.workspace.registry).run_once()
    status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["run"]["status"] == "completed", status.text
    _assert_facts(project, run_id, source="cache", provider_cost_usd=0.0)
    assert _ProviderAdapter.calls == []
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()["n"]
        == 1
    )
    spend = client.get("/api/spend")
    assert spend.status_code == 200, spend.text
    assert spend.json()["total_cost"] == 0.0


def test_wrong_cache_key_does_not_bypass_missing_key_preflight_or_mutate_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.ai.llm import LLMRequest, ResponseCache, request_key
    from typed_model_fixtures import model_plan

    _ProviderAdapter.calls = []
    _ProviderAdapter.failure = None
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    pid, sheet_id = _project(client)
    project = app.state.workspace.get(pid)
    action = _action(sheet_id, "team-cache-key-mismatch")

    # Same provider/model, deliberately different context and row input. A
    # model-level cache candidate is not proof this exact request can replay.
    params = action["params"]
    mismatched_spec = {
        "recipe": "classify",
        "model": params["model"],
        "sheet_id": sheet_id,
        "input_columns": params["source"],
        "context": "Different context that must produce a different cache key.",
        "fields": params["fields"],
        "include_justification": True,
        "include_confidence": True,
    }
    recipe = model_plan(mismatched_spec).program
    rendered = recipe.render(
        {"story": "A deliberately different cached story."}, mismatched_spec
    )
    cache = ResponseCache(project.path / "project.cache.db")
    cache.put(
        request_key(
            LLMRequest(
                model=params["model"],
                messages=rendered.messages,
                schema=rendered.schema,
                max_tokens=rendered.max_tokens,
            ),
            recipe.version,
        ),
        LLMResponse(
            content=json.dumps({"beat": "other"}),
            data={"beat": "other"},
            tokens_in=1,
            tokens_out=1,
            cost=0.001,
            model=params["model"],
        ),
    )
    cache.close()
    before = _project_state(project)
    before_jobs = app.state.workspace.queue.list_project_jobs(pid)

    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert response.status_code == 200, response.text
    assert response.status_code != 402, response.text
    body = response.json()
    assert body["status"] == "failed", body
    assert body["run_id"] is None and body["job_id"] is None
    assert body["receipt_id"] is None and body["op_ids"] == []
    error = body["errors"][0]
    assert error["code"] == "missing_provider_key", error
    assert error["details"] == {
        "provider": "openai",
        "retryable": True,
        "resumable": True,
    }
    assert _ProviderAdapter.calls == []
    assert _project_state(project) == before
    assert app.state.workspace.queue.list_project_jobs(pid) == before_jobs


def test_direct_reduce_quota_error_keeps_named_resumable_receipt_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ProviderAdapter.calls = []
    _ProviderAdapter.failure = LLMError(
        "provider quota exhausted", status=429, retryable=True
    )
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    assert (
        client.post(
            "/api/org/keys", json={"provider": "openai", "key": SECRET}
        ).status_code
        == 200
    )
    pid, sheet_id = _project(client)
    before_jobs = app.state.workspace.queue.list_project_jobs(pid)

    response = post_v1_action_with_exact_confirmation(
        client, pid, _reduce_action(sheet_id, "team-reduce-provider-quota")
    )
    assert response.status_code == 200, response.text
    assert response.status_code != 402, response.text
    body = response.json()
    assert body["status"] in {"failed", "needs_user_action"}, body
    assert body["job_id"] is None, body
    assert body["receipt_id"], body
    assert body["errors"], body
    error = body["errors"][0]
    assert error["code"] == "provider_rate_limited", error
    assert error["details"] == {"retryable": True, "resumable": True}, error

    receipt = client.get(
        f"/api/projects/{pid}/actions/v1/receipts/{body['receipt_id']}"
    )
    assert receipt.status_code == 200, receipt.text
    receipt_body = receipt.json()
    assert receipt_body["status"] in {"failed", "needs_user_action"}
    assert receipt_body["errors"] == body["errors"]
    assert app.state.workspace.queue.list_project_jobs(pid) == before_jobs


def test_provider_quota_error_is_named_resumable_and_does_not_become_402(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ProviderAdapter.calls = []
    _ProviderAdapter.failure = LLMError(
        "provider quota exhausted", status=429, retryable=True
    )
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    assert (
        client.post(
            "/api/org/keys", json={"provider": "openai", "key": SECRET}
        ).status_code
        == 200
    )
    pid, sheet_id = _project(client)
    started = post_v1_action_with_exact_confirmation(
        client,
        pid,
        _action(sheet_id, "provider-quota"),
    )
    assert started.status_code != 402, started.text
    assert started.status_code == 200, started.text
    started_body = started.json()
    run_id = int(started_body["run_id"])
    receipt_id = started_body["receipt_id"]
    Worker(app.state.workspace.queue, app.state.workspace.registry).run_once()
    status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert status.status_code != 402, status.text
    assert status.status_code == 200, status.text
    body = status.json()["run"]
    assert body["status"] in {"failed", "needs_user_action"}, body
    receipt = client.get(f"/api/projects/{pid}/actions/v1/receipts/{receipt_id}")
    assert receipt.status_code == 200, receipt.text
    errors = receipt.json().get("errors") or []
    assert errors, body
    assert errors[0]["code"] in {
        "provider_quota_exhausted",
        "provider_rate_limited",
        "provider_key_exhausted",
    }, errors[0]
    # Retry/resume is represented as stable, non-commerce action error data;
    # re-submitting the same idempotent action after fixing provider quota is
    # the recovery path, not buying credits or crossing a 402 gate.
    assert errors[0]["details"].get("retryable") is True, errors[0]
    assert errors[0]["details"].get("resumable", True) is True, errors[0]

"""LLM-model key request-time gate.

Without a Gemini key, an 11-row ``map.extract`` run with ``gemini/flash-lite``
would otherwise burn every row independently failing "No API key is
configured for gemini" — a fact knowable at LAUNCH, before any row work
started. `ActionRunService.run_action` (server/services/action_runs.py) is
the ONE choke point both the queued (`queue_v1_action_run`) and direct
(`run_action_spec`) dispatch paths pass through — the same choke point the
`missing_action_credential` census gate (`test_action_api_key_gate.py`) uses.
That gate keys off a STATIC per-kind `required_credentials` list; this one
generalizes it to any action whose `params.model` names a chat-completion
provider/model ("provider/model-id"), resolved at REQUEST time via
`missing_model_key_error_for_request` (server/action_enqueue.py).

Replay-mode design decision (documented in
`missing_model_key_error_for_request`'s docstring too): a `'replay'`-mode
cache HIT can complete a row with no adapter at all — the "keyless dev
default" the whole test suite runs under. Cheaply proving
every target row of a SPECIFIC request is a cache hit before reservation
would mean re-deriving each recipe's request-rendering logic at this
generic, recipe-agnostic seam, which is not a cheap or honest thing to do
here — so, exactly like `MapRunner._validate_spec`'s own `MissingProviderKey`
pre-flight in ``runner/map_runner.py``, this
gate exempts `'replay'` mode when a cache object is actually configured (and
`'replay_strict'` when one is, which then never falls through to a live call
at all — WITHOUT a cache, strict replay reaches the adapter just like
cacheless `'replay'`, so it gates), and otherwise gates honestly on
key-absence. A genuine cache miss under either
exemption still surfaces the SAME `missing_provider_key` message, just at
the row level — unchanged, pre-existing, already-tested behavior for
`map.*` actions (`tests/test_preflight_cache_hit.py`). What's NEW here is
coverage: before this gate, `research.answer` and `reduce.group_summary`
had NO request-time (or even row-level) key
check at all — a keyless launch of any of them in `'fresh'`/`'off'` mode
would have failed opaquely per row, or worse.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.ai.llm import ModelRouter, ResponseCache
from frisket.cloud_connection import CloudConnectionStore
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMUsage,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudUser,
)
from frisket.engine.jobs import Worker
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.action_enqueue import missing_model_key_error_for_request
from frisket.server.app import create_app
from frisket.server.services.action_runs import v1_action_result_http_status


def _client(tmp_path, *, router: ModelRouter | None = None) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=router or ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _project_id(client: TestClient, name: str = "Key Gate") -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _extract_action(
    *,
    model: str = "gemini/gemini-2.5-flash-lite",
    idempotency_key: str = "map_extract@sha256:key-gate",
) -> dict[str, Any]:
    return {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": ["text"],
            "model": model,
            "instruction": "Extract the named entity.",
            "fields": [{"name": "entity", "type": "text", "description": "the entity"}],
        },
        "idempotency_key": idempotency_key,
    }


def _reduce_action(
    *,
    model: str = "gemini/gemini-2.5-flash-lite",
    idempotency_key: str = "reduce_summary@sha256:key-gate",
) -> dict[str, Any]:
    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "sheet_name": "Summaries",
        "params": {
            "source": ["story"],
            "model": model,
            "instruction": "Summarize.",
        },
        "idempotency_key": idempotency_key,
    }


def test_map_extract_refuses_keyless_provider_zero_runs_created(tmp_path) -> None:
    client = _client(tmp_path)
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    assert response.status_code == 400, response.text
    assert response.status_code != 402  # distinct from the cost-gate envelope
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.action.kind == "map.extract"
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.code == "missing_provider_key"
    assert error.details["provider"] == "gemini"
    assert "No API key is configured for 'gemini'" in error.message
    assert error.field == "params.model"

    project = client.app.state.workspace.get(project_id)
    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 0


def test_reduce_group_summary_also_gated_generic_shared_seam(tmp_path) -> None:
    """Proves the gate isn't map.*-specific: reduce.group_summary had NO
    request-time (or row-level) key check at all before this task."""
    client = _client(tmp_path)
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_reduce_action(),
    )
    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    error = result.errors[0]
    assert error.code == "missing_provider_key"
    assert error.details["provider"] == "gemini"

    project = client.app.state.workspace.get(project_id)
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_gate_message_matches_the_row_level_missing_provider_key_copy(
    tmp_path,
) -> None:
    """'400 w/ the same actionable message the row error carries' (manifest).
    classify_llm_error (frisket.llm.remediation) is what a row-level failure
    would have used for this exact exception; both build from the same
    missing_provider_key_message helper, so the strings cannot drift."""
    from frisket.ai.llm.remediation import missing_provider_key_message

    client = _client(tmp_path)
    project_id = _project_id(client)
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    result = ActionResult.model_validate(response.json())
    assert result.errors[0].message == missing_provider_key_message("gemini")


def test_provider_with_configured_key_passes_the_gate(tmp_path) -> None:
    """Past the gate, the launch fails for an ordinary reason (sheet_id=1
    does not exist in a fresh project) rather than the key error — proves
    this is the FIRST check, not a blanket refusal."""
    client = _client(
        tmp_path,
        router=ModelRouter(keys={"gemini": "a-real-key"}, cache=None, cache_mode="off"),
    )
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    assert response.status_code != 400 or "missing_provider_key" not in response.text


def test_connected_cloud_model_runs_through_queued_worker_composition(
    tmp_path,
) -> None:
    model = "gemini/gemini-2.5-flash-lite"
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[
                CloudAccount(
                    id=41,
                    name="Personal",
                    kind="personal",
                    balance_micro=100,
                    available_micro=100,
                    manage_billing=True,
                )
            ],
        ),
    )

    class Client:
        origin = "https://app.frisket.dev"
        calls = 0

        @staticmethod
        def route():
            return CloudCapabilityRoute(
                capability="llm.complete",
                engine=model,
                provider="gemini",
                lane="frisket_credits",
                route_revision="route-1",
                ready=True,
                blocker=None,
                price=CloudTokenPrice(
                    kind="tokens",
                    input_micro_per_million_tokens=1,
                    output_micro_per_million_tokens=1,
                ),
                options=CloudLLMOptionSupport(
                    kind="llm",
                    structured_output=True,
                    tools=True,
                    vision=True,
                    max_output_tokens=32768,
                ),
            )

        def capabilities(self, token, account_id):
            assert token == "installation-token"
            assert account_id == 41
            self.calls += 1
            return CloudCapabilitiesResponse(
                account_id=41,
                routes=[self.route()],
            )

        def close(self):
            return None

    store.save_capabilities(
        CloudCapabilitiesResponse(account_id=41, routes=[Client.route()]),
        expected_user_id=7,
    )
    cloud_client = Client()

    class Transport:
        calls = []

        async def execute_llm(self, attempt, request):
            self.calls.append((attempt, request))
            assert request.schema_ is not None
            [field] = request.schema_["properties"]
            data = {field: {"value": "Alice"}}
            return CloudExecutionResponse(
                execution_id=f"cloud-key-gate-execution-{len(self.calls)}",
                status="completed",
                route=Client.route(),
                result=CloudLLMResult(
                    content=json.dumps(data),
                    data=data,
                    tokens_in=4,
                    tokens_out=2,
                    cost=0.000001,
                    model=model,
                    cost_source="cloud_receipt",
                    cached=False,
                    provider="gemini",
                    credential_source="platform_key",
                    raw={},
                    output_limited=False,
                ),
                receipt=CloudExecutionReceipt(
                    user_id=7,
                    account_id=41,
                    lane="frisket_credits",
                    provider="gemini",
                    engine=model,
                    charge_micro=1,
                    provider_cost_micro=1,
                    usage=CloudLLMUsage(kind="llm.complete", tokens_in=4, tokens_out=2),
                ),
            )

    transport = Transport()
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
            cloud_connection_store=store,
            cloud_connection_client=cloud_client,
            cloud_execution_transport=transport,
        )
    )
    project_id = _project_id(client)
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("rows.csv", b"text\nAlice arrived\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    project = client.app.state.workspace.get(project_id)
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )

    action = _extract_action(model=model)
    action["scope"]["sheet_id"] = imported.json()["sheet_id"]
    challenge = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=action,
    )

    assert challenge.status_code == 402, challenge.text
    assert "missing_provider_key" not in challenge.text
    assert cloud_client.calls == 0
    action["confirmation"] = challenge.json()["errors"][0]["details"][
        "promise_set_hash"
    ]
    launched = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=action,
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["status"] == "queued"
    assert cloud_client.calls == 0

    assert Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    ).run_once()

    assert cloud_client.calls == 0
    assert len(transport.calls) == 1, [
        (job.status, job.error)
        for job in client.app.state.workspace.queue.list_jobs(limit=10)
    ]
    assert transport.calls[0][0].quote_authority.maximum_charge_micro is None
    sheet = client.get(
        f"/api/projects/{project_id}/sheets/{imported.json()['sheet_id']}/data"
    )
    assert sheet.status_code == 200, sheet.text
    sheet_data = sheet.json()
    entity_column = next(
        column for column in sheet_data["columns"] if column["name"] == "entity"
    )
    assert sheet_data["rows"][0]["cells"][str(entity_column["id"])] == "Alice"
    facts = project.db.execute(
        "SELECT id,provider,credential_source,epoch_id FROM model_calls"
    ).fetchall()
    assert {fact["id"] for fact in facts} == {"cloud-key-gate-execution-1"}
    assert {fact["provider"] for fact in facts} == {"gemini"}
    assert {fact["credential_source"] for fact in facts} == {"platform_key"}
    assert all(fact["epoch_id"] is not None for fact in facts)


def test_ollama_model_never_gated_local_keyless(tmp_path) -> None:
    client = _client(tmp_path)
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(model="ollama/@test-local/qwen3:8b"),
    )
    assert response.status_code != 400 or "missing_provider_key" not in response.text


def test_action_kind_with_no_model_param_not_gated(tmp_path) -> None:
    """derive.link_table has no `model` field in its params — nothing for
    this gate to resolve, so it must pass through untouched (a schema/other
    error is fine; missing_provider_key must never appear)."""
    client = _client(tmp_path)
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "derive.link_table",
            "scope": {"kind": "project"},
            "sheet_name": "Links",
            "params": {
                "source": {"kind": "semantic_join", "receipt_id": "missing"},
            },
            "idempotency_key": "derive_link_table@sha256:key-gate",
        },
    )
    assert "missing_provider_key" not in response.text


def test_replay_mode_with_cache_configured_bypasses_the_gate(tmp_path) -> None:
    """The documented replay-mode exemption: a cache HIT can complete keyless
    in 'replay' mode, so the gate must not block on key-absence alone when a
    cache object is actually configured — mirrors
    the regression guard's MapRunner design exactly."""
    client = _client(
        tmp_path,
        router=ModelRouter(
            cache=ResponseCache(tmp_path / "cache.db"), cache_mode="replay"
        ),
    )
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    assert response.status_code != 400 or "missing_provider_key" not in response.text


def test_replay_strict_with_a_cache_bypasses_the_gate(tmp_path) -> None:
    """The keyless golden-cache posture: a strict
    replay that HAS a cache answers from it or raises CacheMiss, never
    reaching an adapter, so a missing key is not a misconfiguration."""
    client = _client(
        tmp_path,
        router=ModelRouter(
            cache=ResponseCache(tmp_path / "gate.cache.db"),
            cache_mode="replay_strict",
        ),
    )
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    assert response.status_code != 400 or "missing_provider_key" not in response.text


def test_replay_strict_without_a_cache_object_still_gates(tmp_path) -> None:
    """'replay_strict' with NO cache configured can never serve a hit and
    never raises CacheMiss either: `_complete_transport`'s strict branch is
    guarded on `self.cache is not None`, so the call falls straight through to
    the live adapter. It needs a key exactly like 'replay' without a cache —
    the exact sibling case below. Exempt it on the mode string alone and the
    launch is admitted keyless, then bills (or fails opaquely) per row."""
    client = _client(
        tmp_path,
        router=ModelRouter(cache=None, cache_mode="replay_strict"),
    )
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.errors[0].code == "missing_provider_key"


def test_replay_mode_without_a_cache_object_still_gates(tmp_path) -> None:
    """'replay' with NO cache configured can never serve a hit, so it must
    still hard-block exactly like 'fresh'/'off' — the case the exemption
    above must not swallow."""
    client = _client(
        tmp_path,
        router=ModelRouter(cache=None, cache_mode="replay"),
    )
    project_id = _project_id(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_extract_action(),
    )
    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.errors[0].code == "missing_provider_key"


def test_missing_provider_key_status_maps_to_400_not_402() -> None:
    result = ActionResult(
        action={"kind": "map.extract", "action_id": "act_1"},
        status="failed",
        project_id="p1",
        errors=[
            {
                "schema_version": "frisket.action_error.v1",
                "code": "missing_provider_key",
                "message": "needs a key",
                "action_kind": "map.extract",
            }
        ],
    )
    assert v1_action_result_http_status(result) == 400


def test_helper_returns_none_when_model_field_absent() -> None:
    router = ModelRouter(cache=None, cache_mode="off")
    assert (
        missing_model_key_error_for_request("sheet.refresh", {"sheet_id": 1}, router)
        is None
    )


def test_helper_returns_none_for_unrecognized_provider() -> None:
    """An unrecognized provider id isn't this gate's concern — no key/settings
    story to offer; let normal validation/execution surface it instead."""
    router = ModelRouter(cache=None, cache_mode="off")
    assert (
        missing_model_key_error_for_request(
            "map.extract", {"model": "made_up_provider/x"}, router
        )
        is None
    )

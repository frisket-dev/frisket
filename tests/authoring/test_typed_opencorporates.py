"""Real typed OpenCorporates child, paid admission and host-owned HTTP facts."""

import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor import opencorporates_read
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from helpers import replace_test_source_cell

PLUGIN = "frisket.opencorporates"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    shipped = (
        Path(__file__).resolve().parents[2] / "src/frisket/authoring/bundled_plugins"
    )
    root = tmp_path / "bundled"
    shutil.copytree(shipped / PLUGIN, root / PLUGIN)
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: root)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: root)
    _reset_default_registry_for_tests()
    app = create_app(
        tmp_path / "workspace", enable_provider_config=False, serve_spa=False
    )
    with TestClient(app) as client:
        project_id = app.state.workspace.create("companies")["id"]
        project = app.state.workspace.get(project_id)
        sheet = project.add_sheet("Companies")
        columns = {
            name: project.add_column(sheet, name)
            for name in ("jurisdiction", "number", "name")
        }
        rows = project.add_rows(
            sheet,
            [
                {
                    "jurisdiction": "gb",
                    "number": "00123",
                    "name": "Example Ltd",
                }
            ],
            columns,
        )
        entry = next(
            item
            for item in client.get(
                f"/api/projects/{project_id}/workbench/plugins"
            ).json()["plugins"]
            if item["pluginId"] == PLUGIN
        )
        for suffix, payload in (
            (
                "activate",
                {
                    "receiptId": entry["receiptId"],
                    "trustAcknowledged": True,
                    "permissionsAccepted": [
                        "plugin:trusted_local_backend",
                        "external:opencorporates",
                    ],
                    "arbitraryPackageLoadAllowed": False,
                },
            ),
            (
                "backend/activate",
                {
                    "trustAcknowledged": True,
                    "arbitraryPackageLoadAllowed": False,
                    "executableHandlersAllowed": True,
                },
            ),
            ("env", {"name": "OPENCORPORATES_API_TOKEN", "value": "test-token"}),
        ):
            response = client.post(
                f"/api/projects/{project_id}/workbench/plugins/{PLUGIN}/{suffix}",
                json=payload,
            )
            assert response.status_code == 200, response.text
        calls = []
        responses = []

        def respond(request):
            calls.append(request)
            assert request.headers["X-API-TOKEN"] == "test-token"
            if responses:
                return responses.pop(0)
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "row": {
                            "result": [
                                {
                                    "id": "/companies/gb/00123",
                                    "score": 90,
                                    "match": True,
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": {
                        "company": {
                            "jurisdiction_code": "gb",
                            "company_number": "00123",
                            "name": "Example Ltd",
                            "inactive": False,
                        }
                    }
                },
            )

        actual_client = httpx.AsyncClient
        monkeypatch.setattr(
            opencorporates_read.httpx,
            "AsyncClient",
            lambda **kwargs: actual_client(
                **kwargs, transport=httpx.MockTransport(respond)
            ),
        )
        yield client, project, project_id, sheet, rows, calls, responses
    _reset_default_registry_for_tests()


def request(sheet, action="lookup_company"):
    return {
        "action_id": f"{PLUGIN}.{action}",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"jurisdiction_code": "jurisdiction", "company_number": "number"}
        if action == "lookup_company"
        else {"company_name": "name"},
        "idempotency_key": action,
    }


def confirm(project, project_id, body):
    quote = run_action_spec(project, body, project_id=project_id)
    assert quote.status == "needs_confirmation", quote.model_dump()
    error = quote.errors[0]
    body = {**body, "confirmation": error.details["promise_set_hash"]}
    return body, quote


@pytest.mark.parametrize(
    "action,expected_calls", [("lookup_company", 1), ("match_company", 2)]
)
def test_actual_typed_child_requires_exact_quote_and_records_host_responses(
    installed, action, expected_calls
):
    _, project, project_id, sheet, _, calls, _ = installed
    body, quote = confirm(project, project_id, request(sheet, action))
    assert not calls
    assert quote.errors[0].details["estimate"]["cost"] is None
    result = run_action_spec(project, body, project_id=project_id)
    assert result.status == "completed", result.model_dump()
    assert len(calls) == expected_calls
    assert calls[-1].url.path == "/v0.4/companies/gb/00123"
    if action == "match_company":
        from urllib.parse import parse_qs

        query = json.loads(parse_qs(calls[0].content.decode())["queries"][0])
        assert query == {"row": {"query": "Example Ltd", "limit": 1}}
    assert len(result.outputs) == 18
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["effect_count"] == expected_calls
    assert receipt.provider_use[0]["cost_actual"] is None
    assert receipt.provider_use[0]["effect_count_source"] == "host_http_response"
    assert (
        len(
            [
                item
                for item in receipt.evidence
                if item.ref["kind"] == "opencorporates_request"
            ]
        )
        == expected_calls
    )
    assert run_action_spec(project, body, project_id=project_id) == result
    assert len(calls) == expected_calls
    values = {
        row["name"]: row["value"]
        for row in project.db.execute(
            "SELECT c.name, r.value FROM results r JOIN columns c ON c.id=r.column_id"
        )
    }
    assert json.loads(values["oc_company_number"]) == "00123"
    assert json.loads(values["oc_match_state"]) == "matched"


@pytest.mark.parametrize(
    "status,expected", [(404, "not_found"), (503, None), (200, None)]
)
def test_provider_failures_are_counted_without_inventing_price(
    installed, status, expected
):
    _, project, project_id, sheet, _, calls, responses = installed
    responses.append(httpx.Response(status, content=b"not-json"))
    body, _ = confirm(project, project_id, request(sheet))
    result = run_action_spec(project, body, project_id=project_id)
    assert result.status == ("completed" if expected else "failed"), result.model_dump()
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["effect_count"] == 1
    assert receipt.provider_use[0]["cost_actual"] is None
    assert len(calls) == 1
    assert run_action_spec(project, body, project_id=project_id) == result
    assert len(calls) == 1


def test_missing_secret_is_a_returned_refusal_not_an_ambiguous_call(
    installed, monkeypatch
):
    from frisket.authoring.workbench import plugin_subprocess

    _, project, project_id, sheet, _, calls, _ = installed
    body, _ = confirm(project, project_id, request(sheet))
    monkeypatch.setattr(plugin_subprocess, "_project_plugin_secrets", lambda *a, **kw: {})
    result = run_action_spec(project, body, project_id=project_id)
    assert result.status == "failed", result.model_dump()
    assert not calls
    assert not ReceiptStore(project).parsed_by_id(result.receipt_id).provider_use
    assert not project.db.execute(
        "SELECT 1 FROM effect_checkpoints WHERE state='reserved'"
    ).fetchone()


def test_publication_rollback_retains_paid_raw_outcome_and_response_facts(
    installed, monkeypatch
):
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram

    _, project, project_id, sheet, _, calls, _ = installed

    def fail_publication(self, *args, **kwargs):
        raise RuntimeError("publication rollback probe")

    monkeypatch.setattr(
        _TypedMapRowsProgram, "write_result_evidence", fail_publication, raising=False
    )
    body, _ = confirm(project, project_id, request(sheet))
    result = run_action_spec(project, body, project_id=project_id)
    assert result.status == "failed", result.model_dump()
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    saved = project.db.execute("SELECT family,state FROM effect_checkpoints").fetchall()
    assert [(item["family"], item["state"]) for item in saved] == [
        ("row_effect", "returned")
    ]
    assert (
        ReceiptStore(project)
        .parsed_by_id(result.receipt_id)
        .provider_use[0]["effect_count"]
        == 1
    )
    replay = run_action_spec(project, body, project_id=project_id)
    # No output was published: the shared receipt guard refuses replay instead
    # of presenting a successful materialization or purchasing another call.
    assert replay.status == "failed"
    assert [error.code for error in replay.errors] == ["project_write_failed"]
    assert len(calls) == 1


@pytest.mark.parametrize("exhausted", [False, True])
def test_rate_limit_retry_preserves_policy_and_counts_each_response(
    installed, monkeypatch, exhausted
):
    from frisket.engine.executor import opencorporates_read
    from frisket.engine.jobs.rate_limiter import SqliteRateLimiter

    _, project, project_id, sheet, _, calls, responses = installed

    class Clock:
        value = 0

        def now_ms(self):
            return self.value

        def sleep_ms(self, ms):
            self.value += ms

    clock = Clock()
    monkeypatch.setattr(
        opencorporates_read,
        "SqliteRateLimiter",
        lambda path: SqliteRateLimiter(path, clock=clock, sleeper=clock),
    )
    responses.append(httpx.Response(429, headers={"Retry-After": "2"}))
    if exhausted:
        responses.append(httpx.Response(429, headers={"Retry-After": "3"}))
    body, _ = confirm(project, project_id, request(sheet))
    result = run_action_spec(project, body, project_id=project_id)
    assert result.status == ("failed" if exhausted else "completed"), (
        result.model_dump()
    )
    assert len(calls) == 2 and clock.value >= 2000
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["effect_count"] == 2
    assert receipt.provider_use[0]["cost_actual"] is None
    assert run_action_spec(project, body, project_id=project_id) == result
    assert len(calls) == 2


def test_confirmation_refuses_source_drift_and_wrong_echo_before_http(installed):
    _, project, project_id, sheet, rows, calls, _ = installed
    body, _ = confirm(project, project_id, request(sheet))
    wrong = run_action_spec(
        project, {**body, "confirmation": "wrong"}, project_id=project_id
    )
    assert wrong.status == "needs_confirmation"
    number = next(
        column["id"] for column in project.columns(sheet) if column["name"] == "number"
    )
    replace_test_source_cell(
        project,
        row_id=rows[0],
        column_id=number,
        value="999",
    )
    stale = run_action_spec(project, body, project_id=project_id)
    assert stale.status == "needs_confirmation", stale.model_dump()
    assert not calls


def test_cancel_after_response_keeps_host_facts_without_child_outcome(
    installed, monkeypatch
):
    from frisket.engine.executor.action_inventory import ExecutorDeps
    from frisket.engine.executor.actions import _composed_map_runner_factory
    from frisket.engine.store.runs import RunResultStore

    _, project, project_id, sheet, _, calls, _ = installed
    record = opencorporates_read.AdmittedOpenCorporates.record

    def cancel_after_record(self, response, method, *, ctx=None):
        assert ctx is not None
        record(self, response, method, ctx=ctx)
        receipt = project.db.execute(
            "SELECT body FROM receipts WHERE run_id=?", (ctx.extras["run_id"],)
        ).fetchone()
        assert json.loads(receipt["body"])["provider_use"][0]["effect_count"] == 1
        assert RunResultStore(project).request_cancel(ctx.extras["run_id"])

    monkeypatch.setattr(
        opencorporates_read.AdmittedOpenCorporates, "record", cancel_after_record
    )
    body, _ = confirm(project, project_id, request(sheet))

    def factory(project, router):
        runner = _composed_map_runner_factory()(project, router)
        runner.should_cancel = RunResultStore(project).cancellation_requested
        return runner

    result = run_action_spec(
        project,
        body,
        project_id=project_id,
        deps=ExecutorDeps(map_runner_factory=factory),
    )
    assert result.status == "cancelled", result.model_dump()
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert sum(item["effect_count"] for item in receipt.provider_use) == 1
    assert len(calls) == 1
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    saved = project.db.execute("SELECT family,state FROM effect_checkpoints").fetchall()
    assert [(item["family"], item["state"]) for item in saved] == [
        ("row_effect", "returned")
    ]


def test_queue_reconstructs_the_same_paid_confirmation(installed):
    from frisket.engine.jobs.worker import Worker

    client, project, project_id, sheet, _, calls, _ = installed
    url = f"/api/projects/{project_id}/actions/v1/run"
    body = request(sheet)
    quote = client.post(url, json=body)
    assert quote.json()["status"] == "needs_confirmation", quote.text
    assert not calls
    body["confirmation"] = quote.json()["errors"][0]["details"]["promise_set_hash"]
    workspace = client.app.state.workspace
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["status"] == "queued", queued
    assert not calls
    payload = workspace.queue.get(queued["job_id"]).payload
    assert payload["spec"]["implementation_identity"]["plugin_id"] == PLUGIN
    assert payload["spec"]["consented_promise_set_hash"] == body["confirmation"]
    assert Worker(
        workspace.queue, workspace.registry, worker_id="opencorporates"
    ).run_once()
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "completed", receipt.model_dump()
    assert len(calls) == 1 and receipt.provider_use[0]["effect_count"] == 1


def test_new_row_backfill_retains_successor_provider_facts_and_replay(installed):
    _, project, project_id, sheet, _, calls, _ = installed
    body, _ = confirm(project, project_id, request(sheet))
    first = run_action_spec(project, body, project_id=project_id)
    assert first.status == "completed", first.model_dump()
    assert "implementation_identity" in json.loads(
        project.db.execute(
            "SELECT params FROM runs WHERE id=?", (first.run_id,)
        ).fetchone()["params"]
    )
    columns = {column["name"]: column["id"] for column in project.columns(sheet)}
    added = project.add_rows(
        sheet,
        [{"jurisdiction": "gb", "number": "00123", "name": "Example Ltd"}],
        {key: columns[key] for key in ("jurisdiction", "number", "name")},
    )
    backfill, quote = confirm(
        project,
        project_id,
        {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"column": "oc_company_name"},
            "idempotency_key": "oc-new-row-backfill",
        },
    )
    assert len(calls) == 1
    result = run_action_spec(project, backfill, project_id=project_id)
    assert result.status == "completed", result.model_dump()
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["effect_count"] == 1
    assert receipt.provider_use[0]["cost_actual"] is None
    evidence = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "opencorporates_request"
    ]
    assert len(evidence) == 1
    assert evidence[0]["run_id"] == result.run_id != first.run_id
    assert evidence[0]["row_id"] == added[0]
    replay = run_action_spec(project, backfill, project_id=project_id)
    assert replay.receipt_id == result.receipt_id
    assert (
        ReceiptStore(project).parsed_by_id(replay.receipt_id).provider_use
        == receipt.provider_use
    )
    assert len(calls) == 2

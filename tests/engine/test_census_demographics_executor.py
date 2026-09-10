"""Census typed lifecycle against real routing and replayed provider responses."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "census"


def _request(sheet_id, **kwargs):
    return {
        "action_id": "enrich.census_demographics",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "point"},
        "idempotency_key": "census@1",
        **kwargs,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-census-key")
    project = Project.create(tmp_path / "census.frisket")
    sheet_id = project.add_sheet("Places")
    column = project.add_column(sheet_id, "point", type="geo_point")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"point": {"lat": 38.9, "lon": -77.03}},
            {"point": {"lat": 38.9, "lon": -77.03}},
            {"point": {"lat": 38.91, "lon": -77.04}},
        ],
        {"point": column},
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = request.content.decode()
            assert "\n1,-77.03,38.9\n" in body
            assert "\n2,-77.03,38.9\n" not in body
            return httpx.Response(
                200, text=(FIXTURES / "coordinatesbatch_dedupe.csv").read_text()
            )
        assert request.url.params["key"] == "test-census-key"
        return httpx.Response(
            200, json=json.loads((FIXTURES / "acs_2024_dc_tracts.json").read_text())
        )

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    deps = ExecutorDeps(
        execution_composition=open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        )
    )

    def run(body):
        return run_action_spec(
            project, body, project_id="census-project", router=router, deps=deps
        )

    yield project, sheet_id, row_ids, calls, run
    project.close()


def _confirmed(run, body):
    first = run(body)
    if first.status in {"completed", "partial"}:
        # An existing consent covering identical execution terms may be reused.
        return first, body
    assert first.status == "needs_confirmation", first.errors
    request = {**body, "confirmation": first.errors[0].details["promise_set_hash"]}
    return run(request), request


def test_free_public_api_admits_and_materializes(env):
    project, sheet_id, _, calls, run = env
    result = run(_request(sheet_id))
    assert result.status == "completed", result.errors
    assert [request.method for request in calls] == ["POST", "GET"]
    assert len(project.columns(sheet_id)) == 20
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_all_fields_and_known_zero_provider_facts(env):
    project, sheet_id, _, calls, run = env
    result, _ = _confirmed(run, _request(sheet_id))
    assert result.status == "completed", result.errors
    assert [request.method for request in calls] == ["POST", "GET"]
    columns = {column["name"]: column for column in project.columns(sheet_id)}
    assert len(columns) == 20
    assert columns["demo_population"]["type"] == "number"
    assert columns["demo_area_id"]["type"] == "text"
    for name, expected in {
        "demo_population": [1843, 1843, 2479],
        "demo_poverty_rate": [8.46, 8.46, 14.96],
        "demo_area_id": ["11001005802", "11001005802", "11001005303"],
    }.items():
        assert (
            list(project.get_values(sheet_id, columns[name]["id"]).values()) == expected
        )
    row = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert row["action_kind"] == "enrich.census_demographics"
    assert (row["total_rows"], row["completed_rows"], row["failed_rows"]) == (3, 3, 0)
    assert row["cost_actual"] == 0
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (result.run_id,)
        ).fetchone()[0]
        == 57
    )
    facts = project.db.execute(
        "SELECT row_id, units, provider_cost_usd, cost_source, credential_source "
        "FROM model_calls WHERE run_id=?",
        (result.run_id,),
    ).fetchall()
    assert len(facts) == 5
    assert sum(fact["row_id"] is None for fact in facts) == 2
    assert sum(json.loads(fact["units"]).get("requests", 0) for fact in facts) == 2
    assert sum(json.loads(fact["units"]).get("rows", 0) for fact in facts) == 3
    assert all(
        fact["provider_cost_usd"] == 0 and fact["cost_source"] == "free_public_api"
        for fact in facts
    )
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.status == "completed"
    assert len(receipt.outputs) == 19
    assert "test-census-key" not in receipt.model_dump_json()
    assert "census_demographics_tariff" not in receipt.model_dump_json()
    providers = {item["credential_source"]: item for item in receipt.provider_use}
    assert len(receipt.provider_use) == len(providers) == 2
    assert set(providers) == {"none", "local"}
    # Public GeoLookup has no key; ACS and its row facts use the environment
    # key. Their distinct durable credential sources must not be merged.
    for source, provider in providers.items():
        source_facts = [fact for fact in facts if fact["credential_source"] == source]
        assert provider["provider"] == "us_census_acs"
        assert provider["service"] == "census_demographics"
        assert provider["external_api"] is True
        assert provider["cost_actual"] == 0
        assert provider["request_count"] == 1
        assert (
            provider["model_call_count"]
            == len(source_facts)
            == (1 if source == "none" else 4)
        )
        assert provider["dataset"] == "acs/acs5"
        assert provider["geography"] == "tract"
        assert provider["vintage"] == "2024"
    assert (
        sum(provider["request_count"] for provider in providers.values())
        == len(calls)
        == 2
    )


def test_replay_and_conflict_never_repeat_provider_work(env):
    project, sheet_id, _, calls, run = env
    first, body = _confirmed(run, _request(sheet_id))
    assert first.status == "completed", first.errors
    replay = run(body)
    assert replay.receipt_id == first.receipt_id
    assert replay.status == "completed"
    conflict = run({**body, "params": {"source": "point", "include_moe": True}})
    assert conflict.status == "failed"
    assert conflict.errors[0].code == "idempotency_conflict"
    assert len(calls) == 2
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_running_reservation_blocks_duplicate_work(env):
    project, sheet_id, _, calls, run = env
    body = _request(sheet_id)
    request = ActionRequest.model_validate(body)
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    receipt = Receipt(
        receipt_id="receipt-census-running",
        project_id="census-project",
        action_id="action-census-running",
        action_kind=request.action_id,
        idempotency_key=request.idempotency_key,
        params_hash=typed_request_hash(bound),
        status="running",
    )
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, action_id, idempotency_key, params_hash, status, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt.receipt_id,
            receipt.action_kind,
            receipt.action_id,
            receipt.idempotency_key,
            receipt.params_hash,
            receipt.status,
            receipt.model_dump_json(),
        ),
    )
    project.db.commit()
    result = run(body)
    assert result.status == "failed"
    assert result.errors[0].code == "idempotency_in_progress"
    assert not calls


def test_retired_output_makes_replay_stale_without_http(env):
    project, sheet_id, _, calls, run = env
    first, body = _confirmed(run, _request(sheet_id))
    assert first.status == "completed", first.errors
    column = next(
        c for c in project.columns(sheet_id) if c["name"] == "demo_population"
    )
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (column["id"],))
    project.db.commit()
    stale = run(body)
    assert stale.status == "failed"
    assert stale.errors[0].code == "stale_replay"
    assert len(calls) == 2


def test_json_shape_cannot_substitute_for_geo_point_source(env):
    project, _, _, calls, run = env
    sheet_id = project.add_sheet("Untyped JSON")
    column = project.add_column(sheet_id, "point", type="json")
    project.add_rows(
        sheet_id, [{"point": {"lat": 38.9, "lon": -77.03}}], {"point": column}
    )
    result = run(_request(sheet_id))
    assert result.status == "failed"
    assert not calls
    assert len(project.columns(sheet_id)) == 1


def test_requested_moe_materializes_with_output_naming(env):
    project, sheet_id, _, _, run = env
    result, _ = _confirmed(
        run,
        _request(
            sheet_id,
            params={"source": "point", "include_moe": True},
            output_names={"demo_population_moe": "population_uncertainty"},
        ),
    )
    assert result.status == "completed", result.errors
    names = {column["name"] for column in project.columns(sheet_id)}
    assert len(names) == 23
    assert "population_uncertainty" in names
    assert "demo_population_moe" not in names


def test_invalid_point_fails_only_its_row(env):
    project, sheet_id, _, _, run = env
    column = next(c for c in project.columns(sheet_id) if c["name"] == "point")
    project.add_rows(sheet_id, [{"point": None}], {"point": column["id"]})
    result, _ = _confirmed(run, _request(sheet_id))
    row = project.db.execute(
        "SELECT completed_rows, failed_rows FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    # The run counter is processed rows (including its independently failed row).
    assert row["completed_rows"] == 4
    assert row["failed_rows"] == 1
    assert result.status == "partial"


def test_undo_then_new_invocation_does_fresh_provider_work(env):
    project, sheet_id, _, calls, run = env
    first, _ = _confirmed(run, _request(sheet_id))
    assert first.status == "completed", first.errors
    assert project.undo() is not None
    assert {column["name"] for column in project.columns(sheet_id)} == {"point"}
    second, _ = _confirmed(run, _request(sheet_id, idempotency_key="census@2"))
    assert second.status == "completed", second.errors
    assert len(calls) == 4
    assert len(project.columns(sheet_id)) == 20


def test_collision_refuses_before_provider_work(env):
    project, sheet_id, _, calls, run = env
    project.add_column(sheet_id, "demo_population")
    result = run(_request(sheet_id))
    assert result.status == "failed"
    assert not calls
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

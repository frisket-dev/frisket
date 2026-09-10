from __future__ import annotations

import httpx
import pytest
from dataclasses import replace

from frisket.actions.core import RegisteredAction, map_rows
from frisket.actions.geospatial_types import GeocodedAddress, Geocoder
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    EngineRef,
    Row,
    RowResult,
    TextLike,
)


from frisket.ai.llm import ModelRouter
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)


class RenamedGeocodeParams(ActionParams):
    query: ColumnRef[TextLike]
    selected: EngineRef[Geocoder] = EngineRef[Geocoder]("auto")


async def derived_lookup(
    params: RenamedGeocodeParams, row: Row, geocoder: Geocoder
) -> RowResult[GeocodedAddress]:
    return RowResult(output=await geocoder.lookup(f"derived {params.query.read(row)}"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    from frisket.engine.executor import geocode_capability

    async def no_pace():
        pass

    monkeypatch.setattr(geocode_capability, "_nominatim_pace", no_pace)
    project = Project.create(tmp_path / "geo.frisket")
    sheet_id = project.add_sheet("Places")
    column_id = project.add_column(sheet_id, "address", type="text")
    project.add_rows(sheet_id, [{"address": "Oxford"}], {"address": column_id})
    calls = []
    responses = []

    def handler(request):
        calls.append(request)
        if responses:
            return responses.pop(0)
        if request.url.params["q"] == "No result":
            return httpx.Response(
                200,
                json={"results": []}
                if request.url.host == "api.opencagedata.com"
                else [],
            )
        if request.url.host == "api.opencagedata.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "geometry": {"lat": 51.75, "lng": -1.25},
                            "formatted": "Oxford",
                            "confidence": 9,
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json=[{"lat": "51.75", "lon": "-1.25", "display_name": "Oxford"}]
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
            project, body, project_id="geocode", router=router, deps=deps
        )

    run.responses = responses
    yield project, sheet_id, calls, run
    project.close()


def test_typed_geocode_admits_pins_publishes_and_replays(env):
    project, sheet_id, calls, run = env
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "address", "include_lat_lon": True},
        "idempotency_key": "geo-1",
    }
    result, body = _confirmed(run, body)
    assert result.status == "completed", result.errors
    assert len(calls) == 1
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["request_count"] == 1
    assert receipt.provider_use[0]["engine"] == "nominatim"
    columns = {item["name"]: item for item in project.columns(sheet_id)}
    assert set(columns) == {
        "address",
        "geo_point",
        "formatted_address",
        "latitude",
        "longitude",
    }
    assert list(project.get_values(sheet_id, columns["latitude"]["id"]).values()) == [
        51.75
    ]
    assert run(body).status == "completed"
    assert len(calls) == 1


def test_typed_geocode_template_keeps_literal_text_and_numeric_references(env):
    project, sheet_id, calls, run = env
    number = project.add_column(sheet_id, "number", type="integer")
    address = next(
        item["id"] for item in project.columns(sheet_id) if item["name"] == "address"
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"number": 10, "address": "Oxford"}],
        {"number": number, "address": address},
    )
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {"source": {"text": "{{number}} {{address}}"}, "engine": "nominatim"},
        "idempotency_key": "geo-template",
    }
    result, _ = _confirmed(run, body)
    assert result.status == "completed", result.errors
    assert calls[0].url.params["q"] == "10 Oxford"


def test_routed_host_uses_typed_engine_field_and_actual_derived_arguments(
    env, monkeypatch
):
    from frisket.actions.registry import ACTION_REGISTRY

    project, sheet_id, calls, run = env
    registered = ACTION_REGISTRY.get("enrich.geocode")
    replacement = RegisteredAction(
        "enrich.geocode",
        replace(
            registered.definition, run=map_rows(derived_lookup), _example_params=()
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, "enrich.geocode": replacement},
    )
    result, _ = _confirmed(
        run, _request(sheet_id, params={"query": "address", "selected": "nominatim"})
    )
    assert result.status == "completed", result.errors
    assert calls[0].url.params["q"] == "derived Oxford"
    assert calls[0].url.host == "nominatim.openstreetmap.org"
    assert (
        ReceiptStore(project)
        .parsed_by_id(result.receipt_id)
        .provider_use[0]["provider"]
        == "nominatim"
    )


def test_retry_receipt_counts_actual_requests_and_preserves_each_response_fact(
    env, monkeypatch
):
    import json
    from frisket.engine.executor import geocode_capability

    project, sheet_id, calls, run = env
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(geocode_capability.asyncio, "sleep", no_sleep)
    run.responses.append(httpx.Response(429, text="slow down"))
    result, _ = _confirmed(run, _request(sheet_id))
    assert result.status == "completed", result.errors
    assert len(calls) == 2
    facts = project.db.execute("SELECT * FROM model_calls").fetchall()
    assert len(facts) == 2
    assert sum(json.loads(fact["units"])["requests"] for fact in facts) == 2
    assert (
        ReceiptStore(project)
        .parsed_by_id(result.receipt_id)
        .provider_use[0]["request_count"]
        == 2
    )


def _request(sheet_id, **kwargs):
    return {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "address"},
        "idempotency_key": "geocode-lifecycle",
        **kwargs,
    }


def _confirmed(run, body):
    first = run(body)
    if first.status != "needs_confirmation":
        return first, body
    body = {**body, "confirmation": first.errors[0].details["promise_set_hash"]}
    return run(body), body


def test_paid_no_match_remains_review_work_with_named_outputs(env, monkeypatch):
    from frisket.engine.runner.review import review_queue

    project, sheet_id, calls, run = env
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")
    column = next(
        item["id"] for item in project.columns(sheet_id) if item["name"] == "address"
    )
    project.add_rows(sheet_id, [{"address": "No result"}], {"address": column})
    result, body = _confirmed(
        run,
        _request(
            sheet_id,
            output_names={
                "geo_point": "location",
                "formatted_address": "location_address",
            },
        ),
    )
    assert result.status == "completed", result.errors
    assert len(calls) == 2
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    provider = receipt.provider_use[0]
    assert provider["provider"] == "opencage"
    assert provider["request_count"] == 2
    assert provider["cost_actual"] == pytest.approx(0.02)
    assert len(receipt.outputs) == 2
    queue = review_queue(project, sheet_id=sheet_id)
    miss = next(
        item
        for item in queue
        if item["column_name"] == "location" and item["confidence"] == 0
    )
    assert "No result" in miss["justification"] and "opencage" in miss["justification"]
    run_row = project.db.execute(
        "SELECT completed_rows, failed_rows FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert tuple(run_row) == (2, 0)
    assert run(body).receipt_id == result.receipt_id
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["integer", "geo_point"])
def test_incompatible_direct_sources_fail_before_effects(env, kind):
    project, sheet_id, calls, run = env
    project.add_column(sheet_id, "incompatible", type=kind)
    result = run(_request(sheet_id, params={"source": "incompatible"}))
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"
    assert not calls
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "names",
    [{"geo_point": "address"}, {"geo_point": "same", "formatted_address": "same"}],
)
def test_output_collisions_fail_before_effects(env, names):
    project, sheet_id, calls, run = env
    result = run(_request(sheet_id, output_names=names))
    assert result.status == "failed"
    assert not calls
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_conflict_and_stale_replay_do_not_repeat_calls(env):
    project, sheet_id, calls, run = env
    result, body = _confirmed(run, _request(sheet_id))
    conflict = run({**body, "params": {"source": "address", "include_lat_lon": True}})
    assert conflict.errors[0].code == "idempotency_conflict"
    assert project.undo() is not None
    stale = run(body)
    assert stale.status == "failed" and stale.errors[0].code == "stale_replay"
    assert len(calls) == 1
    rerun, _ = _confirmed(run, _request(sheet_id, idempotency_key="fresh-geocode"))
    assert rerun.status == "completed", rerun.errors
    assert len(calls) == 2


def test_running_reservation_blocks_duplicate_provider_work(env):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.contracts.action import Receipt
    from frisket.engine.executor.map_rows_action import typed_request_hash

    project, sheet_id, calls, run = env
    body = _request(sheet_id)
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("enrich.geocode"), ActionRequest.model_validate(body)
    )
    receipt = Receipt(
        receipt_id="receipt-geo-running",
        project_id="geocode",
        action_id="action-geo-running",
        action_kind="enrich.geocode",
        idempotency_key=body["idempotency_key"],
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
    assert (
        result.status == "failed" and result.errors[0].code == "idempotency_in_progress"
    )
    assert not calls


@pytest.mark.parametrize("kind", ["http_body", "geometry", "json"])
def test_known_provider_credential_is_not_persisted_in_errors(env, monkeypatch, kind):
    project, sheet_id, calls, run = env
    key = "opaque-owned-fixture-value-9372"
    monkeypatch.setenv("OPENCAGE_API_KEY", key)
    response = (
        httpx.Response(400, text="x" * 265 + f" authorization value {key}")
        if kind == "http_body"
        else httpx.Response(200, text=f"invalid JSON containing {key}")
        if kind == "json"
        else httpx.Response(
            200, json={"results": [{"geometry": {"lat": key, "lng": 0}}]}
        )
    )
    run.responses.append(response)
    result, _ = _confirmed(run, _request(sheet_id))
    assert result.status == "failed"
    assert len(calls) == 1
    errors = project.db.execute("SELECT error FROM results").fetchall()
    assert errors
    assert all(key[:12] not in (row["error"] or "") for row in errors)
    assert (
        key[:12]
        not in ReceiptStore(project).parsed_by_id(result.receipt_id).model_dump_json()
    )


@pytest.mark.parametrize("latitude", [1000, "NaN", "Infinity"])
def test_invalid_coordinates_keep_response_facts_without_claiming_known_charge(
    env, monkeypatch, latitude
):
    project, sheet_id, calls, run = env
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")
    run.responses.append(
        httpx.Response(
            200, json={"results": [{"geometry": {"lat": latitude, "lng": 0}}]}
        )
    )
    result, _ = _confirmed(run, _request(sheet_id))
    assert result.status == "failed"
    assert len(calls) == 1
    facts = project.db.execute(
        "SELECT provider_cost_usd, cost_source FROM model_calls"
    ).fetchall()
    assert len(facts) == 1
    assert facts[0]["provider_cost_usd"] is None
    assert facts[0]["cost_source"] == "unknown"


@pytest.mark.parametrize("retry", [False, True])
def test_terminal_provider_responses_keep_unknown_cost_and_replay(
    env, monkeypatch, retry
):
    import asyncio

    project, sheet_id, calls, run = env
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")

    async def no_sleep(delay):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    run.responses.extend(
        (
            [httpx.Response(429, text="slow down", headers={"x-request-id": "retry-1"})]
            if retry
            else []
        )
        + [
            httpx.Response(
                402, text="quota exceeded", headers={"x-request-id": "terminal-1"}
            )
        ]
    )
    result, body = _confirmed(run, _request(sheet_id))
    assert result.status == "failed", result.errors
    assert len(calls) == 1 + int(retry)
    facts = project.db.execute("SELECT * FROM model_calls").fetchall()
    assert len(facts) == 1 + int(retry)
    assert all(
        fact["provider_cost_usd"] is None and fact["cost_source"] == "unknown"
        for fact in facts
    )
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use[0]["request_count"] == 1 + int(retry)
    assert receipt.provider_use[0]["cost_actual"] is None
    assert run(body).receipt_id == result.receipt_id
    assert len(calls) == 1 + int(retry)

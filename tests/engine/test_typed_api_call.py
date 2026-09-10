from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest
from pydantic import BaseModel

from frisket.actions.core import RegisteredAction, map_rows
from frisket.actions.http_types import HttpRequest, HttpRequester
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.actions.geospatial_types import Geocoder, GeocodedAddress
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from tests.engine.test_typed_geocode import RenamedGeocodeParams
from tests.engine.test_typed_geocode import env as env
from tests.ops.test_api_call import _allow_egress as _allow_egress


class DerivedParams(ActionParams):
    selected: ColumnRef[str]
    endpoint: HttpRequest
    missing_reference: bool = False
    malformed_request: bool = False


class DerivedOutput(BaseModel):
    answer: dict


async def derived_request(
    params: DerivedParams, row: Row, sender: HttpRequester
) -> RowResult[DerivedOutput]:
    # These actual arguments deliberately do not match the saved template.
    url = (
        "https://api.test.example/{{not_admitted}}"
        if params.missing_reference
        else "https://api.test.example/items/" + params.selected.read(row)
    )
    actual = params.endpoint.model_copy(update={"url": url})
    if params.malformed_request:
        actual = actual.model_copy(update={"timeout": params.selected.read(row)})
    return RowResult(
        output=DerivedOutput(answer=await sender.request_json(actual, row))
    )


def _install_handler(monkeypatch, handler, action_id="map.api_call"):
    original = ACTION_REGISTRY.get(action_id)
    replacement = RegisteredAction(
        action_id,
        replace(original.definition, run=map_rows(handler), _example_params=()),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, action_id: replacement},
    )


def _confirmed(project, body, router):
    quote = run_action_spec(project, body, project_id="api", router=router)
    assert quote.status == "needs_confirmation", quote.errors
    body = {**body, "confirmation": quote.errors[0].details["promise_set_hash"]}
    return run_action_spec(project, body, project_id="api", router=router), body, quote


@pytest.mark.parametrize(
    ("missing_reference", "malformed_request"),
    [(False, False), (True, False), (False, True)],
)
def test_actual_derived_request_is_checked_and_error_never_echoes_arguments(
    tmp_path, monkeypatch, _allow_egress, missing_reference, malformed_request
):
    _install_handler(monkeypatch, derived_request)
    project = Project.create(tmp_path / "api.frisket")
    sheet = project.add_sheet("Data")
    column = project.add_column(sheet, "private", type="text")
    private = "private-customer-123"
    project.add_rows(sheet, [{"private": private}], {"private": column})
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503, text="secret response")

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    body = {
        "action_id": "map.api_call",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "selected": "private",
            "endpoint": {"url": "https://original.example/"},
            "missing_reference": missing_reference,
            "malformed_request": malformed_request,
        },
        "idempotency_key": "derived",
    }
    result, body, _ = _confirmed(project, body, router)
    assert result.status == "failed", result.errors
    assert calls == (
        []
        if missing_reference or malformed_request
        else ["https://api.test.example/items/" + private]
    )
    error = project.db.execute("SELECT error FROM results").fetchone()[0]
    assert (
        "invalid_params"
        if malformed_request
        else "missing_column"
        if missing_reference
        else "http_error"
    ) in error
    assert private not in error and "secret response" not in error
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert private not in receipt.model_dump_json()
    replay = run_action_spec(project, body, project_id="api", router=router)
    assert replay.receipt_id == result.receipt_id
    assert len(calls) == (0 if missing_reference or malformed_request else 1)
    asyncio.run(router._client.aclose())
    project.close()


def test_custom_http_capability_network_off_refuses_before_effect(
    tmp_path, monkeypatch
):
    _install_handler(monkeypatch, derived_request)
    project = Project.create(tmp_path / "network.frisket")
    sheet = project.add_sheet("Data")
    column = project.add_column(sheet, "private", type="text")
    project.add_rows(sheet, [{"private": "one"}], {"private": column})
    project.set_network_policy(mode="off")
    body = {
        "action_id": "map.api_call",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "selected": "private",
            "endpoint": {"url": "https://original.example"},
        },
        "idempotency_key": "blocked",
    }
    result = run_action_spec(project, body, project_id="api")
    assert result.status == "failed" and result.errors[0].code == "network_disabled"
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    project.close()


async def mixed_request(
    params: RenamedGeocodeParams, row: Row, geocoder: Geocoder, sender: HttpRequester
) -> RowResult[GeocodedAddress]:
    result = await geocoder.lookup(params.query.read(row))
    await sender.request_json(
        HttpRequest(url="https://api.test.example/", query_params=[("q", "Oxford")]),
        row,
    )
    return RowResult(output=result)


def test_http_and_routed_capabilities_keep_unknown_total_and_both_provider_facts(
    env, monkeypatch, _allow_egress
):
    project, sheet, calls, run = env
    _install_handler(monkeypatch, mixed_request, "enrich.geocode")
    run.responses.extend(
        [
            httpx.Response(
                200, json=[{"lat": "51.75", "lon": "-1.25", "display_name": "Oxford"}]
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"query": "address", "selected": "nominatim"},
        "idempotency_key": "mixed-http",
    }
    quote = run(body)
    assert quote.status == "needs_confirmation", quote.errors
    assert quote.errors[0].details["estimate"]["cost"] is None
    assert quote.errors[0].details["estimate"]["cost_source"] == "unknown"
    assert calls == []
    body["confirmation"] = quote.errors[0].details["promise_set_hash"]
    result = run(body)
    assert result.status == "completed", result.errors
    assert len(calls) == 2
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert {item["provider"] for item in receipt.provider_use} == {
        "nominatim",
        "external_http",
    }
    assert (
        next(item for item in receipt.provider_use if item["provider"] == "nominatim")[
            "request_count"
        ]
        == 1
    )
    assert run(body).receipt_id == result.receipt_id
    assert len(calls) == 2

    address = next(
        column for column in project.columns(sheet) if column["name"] == "address"
    )
    project.add_rows(sheet, [{"address": "Cambridge"}], {"address": address["id"]})
    run.responses.extend(
        [
            httpx.Response(
                200,
                json=[{"lat": "51.75", "lon": "-1.25", "display_name": "Cambridge"}],
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    backfill = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"column": receipt.outputs[0].name},
        "idempotency_key": "mixed-backfill",
    }
    quote = run(backfill)
    assert quote.status == "needs_confirmation", quote.errors
    assert quote.errors[0].details["estimate"]["cost"] is None
    backfill["confirmation"] = quote.errors[0].details["promise_set_hash"]
    successor = run(backfill)
    assert successor.status == "completed", successor.errors
    assert len(calls) == 4
    assert (
        ReceiptStore(project).parsed_by_id(successor.receipt_id).provider_use
        == receipt.provider_use
    )
    assert run(backfill).receipt_id == successor.receipt_id
    assert len(calls) == 4


@pytest.mark.parametrize("crash", [False, True])
def test_queued_http_request_replay_redelivery_and_backfill(
    tmp_path, monkeypatch, _allow_egress, crash
):
    from tests.engine.test_typed_project_run_queue import _client
    from http_test_helpers import drain_queue
    from frisket.engine.executor import http_request
    from frisket.ops.netguard import safe_request
    from frisket.engine.jobs.ports import JobHandlerContext
    from copy import deepcopy

    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "HTTP lifecycle"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("Data")
    column = project.add_column(sheet, "id", type="text")
    project.add_rows(sheet, [{"id": "1"}, {"id": "2"}], {"id": column})
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})

    async def request_with_mock_client(_client, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as fake:
            return await safe_request(fake, **kwargs)

    monkeypatch.setattr(http_request, "safe_request", request_with_mock_client)
    body = {
        "action_id": "map.api_call",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"request": {"url": "https://api.test.example/{{id}}"}},
        "output_names": {"api_result": "payload"},
        "idempotency_key": "http-queue",
    }
    url = f"/api/projects/{project_id}/actions/v1/run"
    quote = client.post(url, json=body).json()
    assert quote["status"] == "needs_confirmation", quote
    assert calls == []
    body["confirmation"] = quote["errors"][0]["details"]["promise_set_hash"]
    queued = client.post(url, json=body).json()
    assert queued["status"] == "queued", queued
    job = client.app.state.workspace.queue.get(queued["job_id"])
    payload = {**deepcopy(job.payload), "job_id": job.id}
    if crash:
        from frisket.engine.jobs import runs

        class ProcessDeath(BaseException):
            pass

        original = runs.queued_v1_finalize_action_result

        def die(*args, **kwargs):
            raise ProcessDeath()

        monkeypatch.setattr(runs, "queued_v1_finalize_action_result", die)
        with pytest.raises(ProcessDeath):
            client.app.state.workspace.registry.get("project.run")(
                payload, JobHandlerContext.from_claimed_job(trusted_org_id=None)
            )
        monkeypatch.setattr(runs, "queued_v1_finalize_action_result", original)
        client.app.state.workspace.registry.get("project.run")(
            payload, JobHandlerContext.from_claimed_job(trusted_org_id=None)
        )
    else:
        drain_queue(client)
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "completed"
    assert calls == ["/1", "/2"]
    output = next(item for item in project.columns(sheet) if item["name"] == "payload")
    assert output["default_hidden"] and not output["hidden"]
    # A completed worker redelivery reconstructs only receipt facts, not HTTP.
    client.app.state.workspace.registry.get("project.run")(
        payload, JobHandlerContext.from_claimed_job(trusted_org_id=None)
    )
    replay = client.post(url, json=body).json()
    assert (
        replay["receipt_id"] == receipt.receipt_id and replay["status"] == "completed"
    )
    assert calls == ["/1", "/2"]
    project.add_rows(sheet, [{"id": "3"}], {"id": column})
    router = ModelRouter(cache=None, cache_mode="off")
    backfill = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"column": "payload"},
        "idempotency_key": "http-backfill",
    }
    successor, backfill, _ = _confirmed(project, backfill, router)
    assert successor.status == "completed", successor.errors
    assert calls == ["/1", "/2", "/3"]
    assert (
        ReceiptStore(project).parsed_by_id(successor.receipt_id).provider_use
        == receipt.provider_use
    )
    assert (
        run_action_spec(project, backfill, project_id="api", router=router).receipt_id
        == successor.receipt_id
    )
    assert calls == ["/1", "/2", "/3"]


def test_http_row_failures_do_not_halt_remaining_requests(tmp_path, _allow_egress):
    project = Project.create(tmp_path / "many.frisket")
    sheet = project.add_sheet("Data")
    project.add_rows(sheet, [{} for _ in range(20)], {})
    calls = []

    def transport(request):
        calls.append(1)
        return httpx.Response(503, json={"error": "no"})

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    body = {
        "action_id": "map.api_call",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"request": {"url": "https://api.test.example/"}},
        "idempotency_key": "all-errors",
    }
    result, _, _ = _confirmed(project, body, router)
    assert result.status == "failed", result.errors
    assert len(calls) == 20
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE error IS NOT NULL"
        ).fetchone()[0]
        == 20
    )
    asyncio.run(router._client.aclose())
    project.close()

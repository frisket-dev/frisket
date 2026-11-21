"""HTTP-06-F6: alert routes have exact typed HTTP owners.

Alerts bind one saved View. These tests retain typed boundary, paging,
authorization and failure coverage while selection has one canonical owner.
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, JsonValue, RootModel, ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError, WireModel
from frisket.alerts.events import ALERT_CHECK_EVENT_SCHEMA_VERSION
from frisket.server.app import create_app
from frisket.search_index import drain_index
from tests.alert_helpers import post_alert


_CONTRACT_MODULE = "frisket.contracts.http.alerts"

# The alert row: SELECT * over `alerts` plus decoded query/policy/latest run.
_ALERT_FIELDS = frozenset(
    {
        "id",
        "name",
        "view_id",
        "sheet_id",
        "detection_policy",
        "enabled",
        "schedule",
        "last_evaluated_op",
        "last_run_id",
        "last_status",
        "created_at",
        "updated_at",
        "latest_run",
    }
)
_ALERT_NULLABLE_FIELDS = frozenset(
    {
        "schedule",
        "sheet_id",
        "last_run_id",
        "last_status",
        "latest_run",
    }
)
# SELECT * over `alert_checks` with resolved_query decoded.
_RUN_FIELDS = frozenset(
    {
        "id",
        "alert_id",
        "status",
        "op_cursor_before",
        "op_cursor_after",
        "matched_rows",
        "new_rows",
        "error",
        "error_code",
        "resolved_query_hash",
        "resolved_query",
        "started_at",
        "finished_at",
    }
)
_RUN_NULLABLE_FIELDS = frozenset(
    {
        "error",
        "error_code",
        "resolved_query_hash",
        "finished_at",
    }
)
# SELECT * over `alert_check_hits` with is_new coerced to bool.
_HIT_FIELDS = frozenset(
    {"run_id", "sheet_id", "row_id", "column_id", "rank", "snippet", "is_new"}
)
_HIT_NULLABLE_FIELDS = frozenset({"column_id", "snippet"})
_RUN_WITH_HITS_EXTRA_FIELDS = frozenset({"hits", "hits_limit", "hits_truncated"})
_RUN_RESULT_FIELDS = frozenset({"schema_version", "alert", "run", "hits"})
_RUNS_PAGE_FIELDS = frozenset(
    {
        "schema_version",
        "order",
        "offset",
        "limit",
        "total",
        "has_more",
        "next_offset",
        "hits_limit",
        "runs",
    }
)
# decode_alert_check_event: SELECT * with subject_ref/before/after/delta decoded.
_EVENT_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "alert_id",
        "event_kind",
        "subject_kind",
        "subject_ref",
        "before_json",
        "after_json",
        "delta_json",
        "severity",
        "rank",
        "snippet",
        "created_at",
    }
)
_EVENT_NULLABLE_FIELDS = frozenset(
    {"before_json", "after_json", "delta_json", "snippet"}
)
_EVENTS_PAGE_FIELDS = frozenset(
    {
        "schema_version",
        "order",
        "offset",
        "limit",
        "total",
        "has_more",
        "next_offset",
        "events",
    }
)
_CREATE_REQUEST_FIELDS = frozenset(
    {
        "schedule",
        "name",
        "view_id",
        "detection_policy",
        "enabled",
    }
)
_PATCH_REQUEST_FIELDS = frozenset({"name", "enabled", "detection_policy", "schedule"})

_GET_NO_QUERY_ERRORS = frozenset({401, 403, 404, 409, 500})
_GET_PAGED_ERRORS = frozenset({401, 403, 404, 409, 422, 500})
_POST_ERRORS = frozenset({400, 401, 403, 404, 409, 422, 500})
_DELETE_ERRORS = frozenset({401, 403, 404, 409, 500})
_EVENTS_ERRORS = frozenset({400, 401, 403, 404, 409, 422, 500})


@dataclass(frozen=True)
class QueryTruth:
    default: Any
    bounds: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteTruth:
    path: str
    method: str
    errors: frozenset[int]
    response: str
    request: str | None = None
    query: dict[str, QueryTruth] = field(default_factory=dict)


ROUTES: dict[str, RouteTruth] = {
    "list_alerts": RouteTruth(
        path="/api/projects/{pid}/alerts",
        method="GET",
        errors=_GET_NO_QUERY_ERRORS,
        response="AlertList",
    ),
    "create_alert": RouteTruth(
        path="/api/projects/{pid}/alerts",
        method="POST",
        errors=_POST_ERRORS,
        response="Alert",
        request="AlertCreateRequest",
    ),
    "patch_alert": RouteTruth(
        path="/api/projects/{pid}/alerts/{alert_id}",
        method="PATCH",
        errors=_POST_ERRORS,
        response="Alert",
        request="AlertPatchRequest",
    ),
    "delete_alert": RouteTruth(
        path="/api/projects/{pid}/alerts/{alert_id}",
        method="DELETE",
        errors=_DELETE_ERRORS,
        response="AlertDelete",
    ),
    # check_alert remains BODYLESS: an event-triggered evaluation with no
    # request payload.
    "check_alert": RouteTruth(
        path="/api/projects/{pid}/alerts/{alert_id}/check",
        method="POST",
        errors=_POST_ERRORS,
        response="AlertCheckResult",
    ),
    "list_alert_checks": RouteTruth(
        path="/api/projects/{pid}/alerts/{alert_id}/checks",
        method="GET",
        errors=_GET_PAGED_ERRORS,
        response="AlertChecksPage",
        query={
            "offset": QueryTruth(0, {"ge": 0}),
            "limit": QueryTruth(20, {"ge": 1, "le": 100}),
            "hits_limit": QueryTruth(20, {"ge": 1, "le": 500}),
        },
    ),
    "list_alert_check_events": RouteTruth(
        path="/api/projects/{pid}/alerts/{alert_id}/checks/{check_id}/events",
        method="GET",
        errors=_EVENTS_ERRORS,
        response="AlertCheckEventsPage",
        query={
            "offset": QueryTruth(0, {"ge": 0}),
            "limit": QueryTruth(50, {"ge": 1, "le": 100}),
            "event_kind": QueryTruth(None),
        },
    ),
}

OPERATION_IDS = frozenset(
    {
        "tenant.list_alerts.get",
        "tenant.create_alert.post",
        "tenant.patch_alert.patch",
        "tenant.delete_alert.delete",
        "tenant.check_alert.post",
        "tenant.list_alert_checks.get",
        "tenant.list_alert_check_events.get",
    }
)

_ROLE_BY_METHOD = {
    "GET": "viewer",
    "POST": "editor",
    "PATCH": "editor",
    "DELETE": "editor",
}
POLICY_IDENTITIES = frozenset(
    (
        f"tenant.{name}.{truth.method.lower()}",
        "tenant",
        name,
        truth.method,
        "session_or_pat",
        _ROLE_BY_METHOD[truth.method],
        True,
        (),
        False,
    )
    for name, truth in ROUTES.items()
)


def _contracts() -> ModuleType:
    spec = importlib.util.find_spec(_CONTRACT_MODULE)
    if spec is None:
        pytest.fail(
            "INTENDED_F6_RED: F6 watches HTTP contract module is absent "
            f"({_CONTRACT_MODULE} must exist)",
            pytrace=False,
        )
    return importlib.import_module(_CONTRACT_MODULE)


def _model(module: ModuleType, name: str) -> type[BaseModel]:
    candidate = getattr(module, name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
        pytest.fail(
            f"INTENDED_F6_RED: watches.{name} must be a Pydantic model",
            pytrace=False,
        )
    return candidate


def _assert_closed_annotation(annotation: object, *, label: str) -> None:
    """Reject an unbounded DTO field without banning ``JsonValue`` leaves."""

    if annotation is Any:
        pytest.fail(f"INTENDED_F6_RED: {label} must not use Any", pytrace=False)
    origin = get_origin(annotation)
    if (
        annotation in (dict, list, object)
        or origin in (dict, list)
        and not get_args(annotation)
    ):
        pytest.fail(
            f"INTENDED_F6_RED: {label} must not use an unparameterized object",
            pytrace=False,
        )
    for child in get_args(annotation):
        _assert_closed_annotation(child, label=label)


def _assert_closed_wire_model(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("strict") is True
    for name, model_field in model.model_fields.items():
        _assert_closed_annotation(
            model_field.annotation, label=f"{model.__name__}.{name}"
        )


def _assert_all_fields_required(model: type[BaseModel]) -> None:
    assert all(model_field.is_required() for model_field in model.model_fields.values())


def _assert_required_nullable(
    model: type[BaseModel], field_names: frozenset[str]
) -> None:
    for name in field_names:
        model_field = model.model_fields[name]
        assert model_field.is_required(), f"{model.__name__}.{name} must stay required"
        assert type(None) in get_args(model_field.annotation), (
            f"{model.__name__}.{name} must remain nullable"
        )


def _assert_missing_fields_rejected(
    model: type[BaseModel], payload: dict[str, object], field_names: frozenset[str]
) -> None:
    for name in field_names:
        with pytest.raises(ValidationError):
            model.model_validate(
                {key: value for key, value in payload.items() if key != name}
            )


def _query_bounds(query_field: Any) -> dict[str, int]:
    bounds: dict[str, int] = {}
    for metadata in query_field.field_info.metadata:
        for name in ("ge", "le", "gt", "lt"):
            value = getattr(metadata, name, None)
            if value is not None:
                bounds[name] = value
    return bounds


def _run_payload(run_id: int, *, alert_id: int = 1) -> dict[str, Any]:
    return {
        "id": run_id,
        "alert_id": alert_id,
        "status": "ok",
        "op_cursor_before": 0,
        "op_cursor_after": 3,
        "matched_rows": 2,
        "new_rows": 1,
        "error": None,
        "error_code": None,
        "resolved_query_hash": "sha256:abc",
        "resolved_query": {"kind": "search.fts", "q": "budget"},
        "started_at": "2026-08-10 00:00:00",
        "finished_at": "2026-08-10 00:00:01",
    }


def _hit_payload(rank: int) -> dict[str, Any]:
    return {
        "run_id": 4,
        "sheet_id": 3,
        "row_id": 7,
        "column_id": None,
        "rank": rank,
        "snippet": "…budget…",
        "is_new": True,
    }


def _alert_payload(
    alert_id: int, *, latest_run: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "id": alert_id,
        "name": "Budget",
        "view_id": 7,
        "sheet_id": None,
        "detection_policy": {"kind": "new_matches"},
        "enabled": True,
        "schedule": "@hourly",
        "last_evaluated_op": 3,
        "last_run_id": 4,
        "last_status": "ok",
        "created_at": "2026-08-10 00:00:00",
        "updated_at": "2026-08-10 00:00:01",
        "latest_run": latest_run,
    }


def _event_payload(event_id: int) -> dict[str, Any]:
    return {
        "id": event_id,
        "run_id": 4,
        "alert_id": 1,
        "event_kind": "row_entered",
        "subject_kind": "row",
        "subject_ref": {"sheet_id": 3, "row_id": 7},
        "before_json": None,
        "after_json": {"matched": True},
        "delta_json": None,
        "severity": "info",
        "rank": 1,
        "snippet": "…budget…",
        "created_at": "2026-08-10 00:00:01",
    }


@pytest.fixture(scope="module")
def routes_by_name(tmp_path_factory: pytest.TempPathFactory) -> dict[str, APIRoute]:
    app = create_app(tmp_path_factory.mktemp("watch-http-contracts"))
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


# ---------------------------------------------------------------------------
# policy identity and browser membership
# ---------------------------------------------------------------------------


def test_five_alert_routes_have_policy_identity_auth_and_browser_membership() -> None:
    entries = [entry for entry in BASE_ENDPOINT_CATALOG if entry.id in OPERATION_IDS]
    assert len(entries) == len(OPERATION_IDS)
    assert {
        (
            entry.id,
            entry.route_owner,
            entry.route_name,
            entry.method,
            entry.auth,
            entry.project_role,
            entry.browser_client,
            entry.resolvers,
            entry.reserves_funding,
        )
        for entry in entries
    } == set(POLICY_IDENTITIES)


# ---------------------------------------------------------------------------
# route declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_alert_routes_declare_exact_typed_route_shapes(
    name: str, routes_by_name: dict[str, APIRoute]
) -> None:
    assert set(routes_by_name) == set(ROUTES)
    route, truth = routes_by_name[name], ROUTES[name]
    assert route.methods == {truth.method}
    assert route.path == truth.path
    assert (route.status_code or 200) == 200

    response = route.response_model
    assert isinstance(response, type) and issubclass(response, BaseModel), (
        f"INTENDED_F6_RED: {name} must declare a typed response model"
    )
    assert response.__module__ == _CONTRACT_MODULE
    assert response.__name__ == truth.response
    assert set(route.responses) == set(truth.errors)
    assert all(value == {"model": HttpError} for value in route.responses.values())

    body_models = [field.field_info.annotation for field in route.dependant.body_params]
    if truth.request is None:
        # run_watch stays BODYLESS; the GETs stay body-free.
        assert body_models == []
        assert route.body_field is None
    else:
        assert len(body_models) == 1
        assert body_models[0] is not None, (
            f"INTENDED_F6_RED: {name} must declare its request model"
        )
        assert body_models[0].__module__ == _CONTRACT_MODULE
        assert body_models[0].__name__ == truth.request

    query = {param.name: param for param in route.dependant.query_params}
    assert set(query) == set(truth.query)
    for query_name, query_truth in truth.query.items():
        assert query[query_name].default == query_truth.default
        assert _query_bounds(query[query_name]) == query_truth.bounds, (
            f"{name}.{query_name} must keep its existing paging bounds exactly"
        )


# ---------------------------------------------------------------------------
# request DTO stays coercive and keeps its defaults
# ---------------------------------------------------------------------------


def test_alert_create_request_is_closed_strict_and_query_authoritative() -> None:
    contracts = _contracts()
    request = _model(contracts, "AlertCreateRequest")
    assert request.model_config.get("strict") is True
    assert request.model_config.get("extra") == "forbid"
    assert set(request.model_fields) == _CREATE_REQUEST_FIELDS
    for field_name, model_field in request.model_fields.items():
        _assert_closed_annotation(
            model_field.annotation, label=f"WatchCreateRequest.{field_name}"
        )
        assert model_field.alias in (None, field_name), (
            f"WatchCreateRequest.{field_name} must not alias: the route "
            "forwards fields to the service by wire name"
        )
    assert {
        name: model_field.annotation
        for name, model_field in request.model_fields.items()
    } == {
        "name": str,
        "view_id": int,
        "detection_policy": dict[str, JsonValue] | None,
        "enabled": bool,
        "schedule": str | None,
    }

    direct = request.model_validate({"name": " W ", "view_id": 7})
    assert direct.name == "W"
    for invalid in (
        {},
        {"name": "W"},
        {"name": "W", "query": {}, "extra": 1},
        {"name": "W", "query": {}, "binding_mode": "snapshot"},
        {"name": "W", "query": {}, "source_view_id": 3},
        {"name": "W", "query": {}, "source_view": {}},
    ):
        with pytest.raises(ValidationError):
            request.model_validate(invalid)


def test_alert_patch_request_is_closed_nonempty_and_validated() -> None:
    contracts = _contracts()
    request = _model(contracts, "AlertPatchRequest")
    assert request.model_config.get("strict") is True
    assert request.model_config.get("extra") == "forbid"
    assert set(request.model_fields) == _PATCH_REQUEST_FIELDS
    assert {
        name: model_field.annotation
        for name, model_field in request.model_fields.items()
    } == {
        "name": str | None,
        "enabled": bool | None,
        "schedule": str | None,
        "detection_policy": dict[str, JsonValue] | None,
    }

    assert request.model_validate({"name": " Renamed "}).name == "Renamed"
    assert request.model_validate({"enabled": False}).model_fields_set == {"enabled"}
    for invalid in ({}, {"name": ""}, {"enabled": None}, {"extra": True}):
        with pytest.raises(ValidationError):
            request.model_validate(invalid)


# ---------------------------------------------------------------------------
# response DTO graph
# ---------------------------------------------------------------------------


def test_alert_and_run_and_hit_rows_freeze_the_wire_graph() -> None:
    contracts = _contracts()
    alert = _model(contracts, "Alert")
    alert_list = _model(contracts, "AlertList")
    run = _model(contracts, "AlertCheck")
    run_with_hits = _model(contracts, "AlertCheckWithHits")
    hit = _model(contracts, "AlertCheckHit")

    for model in (alert, run, run_with_hits, hit):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)

    run_annotations = {
        "id": int,
        "alert_id": int,
        "status": str,
        "op_cursor_before": int,
        "op_cursor_after": int,
        "matched_rows": int,
        "new_rows": int,
        "error": str | None,
        "error_code": str | None,
        "resolved_query_hash": str | None,
        "resolved_query": dict[str, JsonValue],
        "started_at": str,
        "finished_at": str | None,
    }
    hit_annotations = {
        "run_id": int,
        "sheet_id": int,
        "row_id": int,
        "column_id": int | None,
        "rank": int,
        "snippet": str | None,
        "is_new": bool,
    }
    alert_annotations = {
        "id": int,
        "name": str,
        "view_id": int,
        "sheet_id": int | None,
        "detection_policy": dict[str, JsonValue],
        "enabled": bool,
        "schedule": str | None,
        "last_evaluated_op": int,
        "last_run_id": int | None,
        "last_status": str | None,
        "created_at": str,
        "updated_at": str,
        "latest_run": run | None,
    }

    assert set(run.model_fields) == _RUN_FIELDS
    assert set(hit.model_fields) == _HIT_FIELDS
    assert set(alert.model_fields) == _ALERT_FIELDS
    assert set(run_with_hits.model_fields) == _RUN_FIELDS | _RUN_WITH_HITS_EXTRA_FIELDS
    assert {
        name: model_field.annotation for name, model_field in run.model_fields.items()
    } == run_annotations
    assert {
        name: model_field.annotation for name, model_field in hit.model_fields.items()
    } == hit_annotations
    assert {
        name: model_field.annotation for name, model_field in alert.model_fields.items()
    } == alert_annotations
    assert {
        name: model_field.annotation
        for name, model_field in run_with_hits.model_fields.items()
    } == {
        **run_annotations,
        "hits": list[hit],
        "hits_limit": int,
        "hits_truncated": bool,
    }
    _assert_required_nullable(run, _RUN_NULLABLE_FIELDS)
    _assert_required_nullable(hit, _HIT_NULLABLE_FIELDS)
    _assert_required_nullable(alert, _ALERT_NULLABLE_FIELDS)
    _assert_required_nullable(run_with_hits, _RUN_NULLABLE_FIELDS)

    assert issubclass(alert_list, RootModel)
    assert alert_list.model_config.get("strict") is True
    root_annotation = alert_list.model_fields["root"].annotation
    assert root_annotation == list[alert]
    _assert_closed_annotation(root_annotation, label="WatchList.root")

    run_payload = _run_payload(4)
    hit_payload = _hit_payload(1)
    alert_payload = _alert_payload(1, latest_run=run_payload)
    assert run.model_validate(run_payload).model_dump() == run_payload
    assert hit.model_validate(hit_payload).model_dump() == hit_payload
    alert_wire = alert.model_validate(alert_payload)
    assert alert_wire.model_dump() == alert_payload
    assert isinstance(alert_wire.latest_run, run)
    assert (
        alert.model_validate({**alert_payload, "latest_run": None}).latest_run is None
    )
    assert alert_list.model_validate([alert_payload]).model_dump() == [alert_payload]
    run_with_hits_payload = {
        **run_payload,
        "hits": [hit_payload],
        "hits_limit": 20,
        "hits_truncated": False,
    }
    run_with_hits_wire = run_with_hits.model_validate(run_with_hits_payload)
    assert run_with_hits_wire.model_dump() == run_with_hits_payload
    assert isinstance(run_with_hits_wire.hits[0], hit)

    for name in _RUN_NULLABLE_FIELDS:
        assert run.model_validate({**run_payload, name: None}).model_dump()[name] is (
            None
        )
    for name in ("started_at", "status", "id", "resolved_query"):
        with pytest.raises(ValidationError):
            run.model_validate({**run_payload, name: None})
    with pytest.raises(ValidationError):
        alert.model_validate({**alert_payload, "created_at": None})
    with pytest.raises(ValidationError):
        alert.model_validate({**alert_payload, "enabled": None})
    with pytest.raises(ValidationError):
        hit.model_validate({**hit_payload, "is_new": None})
    with pytest.raises(ValidationError):
        run.model_validate({**run_payload, "id": "4"})
    with pytest.raises(ValidationError):
        alert.model_validate({**alert_payload, "unapproved_watch_field": 1})
    with pytest.raises(ValidationError):
        run.model_validate({**run_payload, "unapproved_run_field": 1})
    with pytest.raises(ValidationError):
        hit.model_validate({**hit_payload, "unapproved_hit_field": 1})
    _assert_missing_fields_rejected(run, run_payload, _RUN_FIELDS)
    _assert_missing_fields_rejected(hit, hit_payload, _HIT_FIELDS)
    _assert_missing_fields_rejected(alert, alert_payload, _ALERT_FIELDS)
    _assert_missing_fields_rejected(
        run_with_hits,
        run_with_hits_payload,
        _RUN_FIELDS | _RUN_WITH_HITS_EXTRA_FIELDS,
    )
    # query/binding/policy stay recursive JSON, not narrowed objects.
    for spec in ({}, {"a": [1, {"b": None}]}, {"c": "s"}, {"d": True}):
        assert (
            alert.model_validate(
                {**alert_payload, "detection_policy": spec}
            ).detection_policy
            == spec
        )


def test_alert_check_result_and_pages_freeze_their_envelopes() -> None:
    contracts = _contracts()
    alert = _model(contracts, "Alert")
    run = _model(contracts, "AlertCheck")
    run_with_hits = _model(contracts, "AlertCheckWithHits")
    hit = _model(contracts, "AlertCheckHit")
    result = _model(contracts, "AlertCheckResult")
    runs_page = _model(contracts, "AlertChecksPage")
    event = _model(contracts, "AlertCheckEvent")
    events_page = _model(contracts, "AlertCheckEventsPage")

    for model in (result, runs_page, event, events_page):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)

    assert set(result.model_fields) == _RUN_RESULT_FIELDS
    assert set(runs_page.model_fields) == _RUNS_PAGE_FIELDS
    assert set(event.model_fields) == _EVENT_FIELDS
    assert set(events_page.model_fields) == _EVENTS_PAGE_FIELDS
    assert {
        name: model_field.annotation
        for name, model_field in result.model_fields.items()
    } == {
        "schema_version": Literal["frisket.alert_check.v1"],
        "alert": alert,
        "run": run,
        "hits": list[hit],
    }
    assert {
        name: model_field.annotation
        for name, model_field in runs_page.model_fields.items()
    } == {
        "schema_version": Literal["frisket.alert_checks_page.v1"],
        "order": Literal["desc"],
        "offset": int,
        "limit": int,
        "total": int,
        "has_more": bool,
        "next_offset": int | None,
        "hits_limit": int,
        "runs": list[run_with_hits],
    }
    assert {
        name: model_field.annotation for name, model_field in event.model_fields.items()
    } == {
        "id": int,
        "run_id": int,
        "alert_id": int,
        "event_kind": str,
        "subject_kind": str,
        "subject_ref": dict[str, JsonValue],
        "before_json": dict[str, JsonValue] | None,
        "after_json": dict[str, JsonValue] | None,
        "delta_json": dict[str, JsonValue] | None,
        "severity": str,
        "rank": int,
        "snippet": str | None,
        "created_at": str,
    }
    assert {
        name: model_field.annotation
        for name, model_field in events_page.model_fields.items()
    } == {
        "schema_version": Literal["frisket.alert_check_events_page.v1"],
        "order": Literal["asc"],
        "offset": int,
        "limit": int,
        "total": int,
        "has_more": bool,
        "next_offset": int | None,
        "events": list[event],
    }
    _assert_required_nullable(runs_page, frozenset({"next_offset"}))
    _assert_required_nullable(events_page, frozenset({"next_offset"}))
    _assert_required_nullable(event, _EVENT_NULLABLE_FIELDS)
    # The events-page literal is the producer's own constant.
    assert ALERT_CHECK_EVENT_SCHEMA_VERSION == "frisket.alert_check_events_page.v1"

    run_payload = _run_payload(4)
    result_payload = {
        "schema_version": "frisket.alert_check.v1",
        "alert": _alert_payload(1, latest_run=run_payload),
        "run": run_payload,
        "hits": [_hit_payload(1)],
    }
    result_wire = result.model_validate(result_payload)
    assert result_wire.model_dump() == result_payload
    assert isinstance(result_wire.alert, alert)
    assert isinstance(result_wire.run, run)
    assert isinstance(result_wire.hits[0], hit)

    runs_page_payload = {
        "schema_version": "frisket.alert_checks_page.v1",
        "order": "desc",
        "offset": 0,
        "limit": 20,
        "total": 1,
        "has_more": False,
        "next_offset": None,
        "hits_limit": 20,
        "runs": [
            {
                **run_payload,
                "hits": [_hit_payload(1)],
                "hits_limit": 20,
                "hits_truncated": False,
            }
        ],
    }
    runs_page_wire = runs_page.model_validate(runs_page_payload)
    assert runs_page_wire.model_dump() == runs_page_payload
    assert isinstance(runs_page_wire.runs[0], run_with_hits)

    event_payload = _event_payload(1)
    events_page_payload = {
        "schema_version": "frisket.alert_check_events_page.v1",
        "order": "asc",
        "offset": 0,
        "limit": 50,
        "total": 1,
        "has_more": False,
        "next_offset": None,
        "events": [event_payload],
    }
    assert event.model_validate(event_payload).model_dump() == event_payload
    events_page_wire = events_page.model_validate(events_page_payload)
    assert events_page_wire.model_dump() == events_page_payload
    assert isinstance(events_page_wire.events[0], event)

    with pytest.raises(ValidationError):
        result.model_validate({**result_payload, "schema_version": "v2"})
    with pytest.raises(ValidationError):
        runs_page.model_validate({**runs_page_payload, "order": "asc"})
    with pytest.raises(ValidationError):
        events_page.model_validate({**events_page_payload, "order": "desc"})
    with pytest.raises(ValidationError):
        events_page.model_validate(
            {**events_page_payload, "schema_version": "frisket.alert_checks_page.v1"}
        )
    with pytest.raises(ValidationError):
        event.model_validate({**event_payload, "created_at": None})
    with pytest.raises(ValidationError):
        event.model_validate({**event_payload, "unapproved_event_field": 1})
    _assert_missing_fields_rejected(result, result_payload, _RUN_RESULT_FIELDS)
    _assert_missing_fields_rejected(runs_page, runs_page_payload, _RUNS_PAGE_FIELDS)
    _assert_missing_fields_rejected(event, event_payload, _EVENT_FIELDS)
    _assert_missing_fields_rejected(
        events_page, events_page_payload, _EVENTS_PAGE_FIELDS
    )
    for name in _EVENT_NULLABLE_FIELDS:
        assert (
            event.model_validate({**event_payload, name: None}).model_dump()[name]
            is None
        )


# ---------------------------------------------------------------------------
# live producers keep their current payloads and semantics
# ---------------------------------------------------------------------------


def test_live_alert_routes_preserve_their_current_payloads(tmp_path: Path) -> None:
    app = create_app(tmp_path / "live-watches")
    workspace = app.state.workspace
    project_id = str(workspace.create("live watches")["id"])

    with TestClient(app) as client:
        assert client.get(f"/api/projects/{project_id}/alerts").json() == []

        created = post_alert(
            client,
            f"/api/projects/{project_id}/alerts",
            json={
                "name": "Budget",
                "query": {"kind": "fts", "q": "budget"},
            },
        )
        assert created.status_code == 200, created.text
        alert = created.json()
        assert set(alert) == _ALERT_FIELDS
        assert alert["view_id"] > 0
        assert alert["sheet_id"] is None
        assert alert["enabled"] is True
        assert alert["detection_policy"] == {
            "kind": "conditions",
            "entered": True,
            "exited": False,
            "count_changed": False,
            "fields": [],
        }
        assert alert["latest_run"] is None
        alert_id = alert["id"]

        renamed = client.patch(
            f"/api/projects/{project_id}/alerts/{alert_id}",
            json={"name": "  Renamed budget  "},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Renamed budget"
        assert renamed.json()["view_id"] == alert["view_id"]
        assert renamed.json()["enabled"] is True

        paused = client.patch(
            f"/api/projects/{project_id}/alerts/{alert_id}", json={"enabled": False}
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["enabled"] is False
        assert paused.json()["name"] == "Renamed budget"

        drain_index(workspace.get(project_id))
        run_result = client.post(f"/api/projects/{project_id}/alerts/{alert_id}/check")
        assert run_result.status_code == 200, run_result.text
        body = run_result.json()
        assert set(body) == _RUN_RESULT_FIELDS
        assert body["schema_version"] == "frisket.alert_check.v1"
        assert set(body["alert"]) == _ALERT_FIELDS
        assert set(body["run"]) == _RUN_FIELDS
        assert body["run"]["status"] == "ok"
        assert body["hits"] == []
        assert set(body["alert"]["latest_run"]) == _RUN_FIELDS

        listed = client.get(f"/api/projects/{project_id}/alerts")
        assert listed.status_code == 200
        assert [set(row) for row in listed.json()] == [_ALERT_FIELDS]
        assert set(listed.json()[0]["latest_run"]) == _RUN_FIELDS

        runs = client.get(f"/api/projects/{project_id}/alerts/{alert_id}/checks")
        assert runs.status_code == 200
        runs_body = runs.json()
        assert set(runs_body) == _RUNS_PAGE_FIELDS
        assert runs_body["schema_version"] == "frisket.alert_checks_page.v1"
        assert runs_body["order"] == "desc"
        assert runs_body["offset"] == 0
        assert runs_body["limit"] == 20
        assert runs_body["hits_limit"] == 20
        assert runs_body["total"] == 1
        assert runs_body["has_more"] is False
        assert runs_body["next_offset"] is None
        assert [set(row) for row in runs_body["runs"]] == [
            _RUN_FIELDS | _RUN_WITH_HITS_EXTRA_FIELDS
        ]
        assert runs_body["runs"][0]["hits"] == []
        assert runs_body["runs"][0]["hits_truncated"] is False
        run_id = runs_body["runs"][0]["id"]

        events = client.get(
            f"/api/projects/{project_id}/alerts/{alert_id}/checks/{run_id}/events"
        )
        assert events.status_code == 200
        events_body = events.json()
        assert set(events_body) == _EVENTS_PAGE_FIELDS
        assert events_body["schema_version"] == "frisket.alert_check_events_page.v1"
        assert events_body["order"] == "asc"
        assert events_body["offset"] == 0
        assert events_body["limit"] == 50
        assert events_body["events"] == []

        # Unknown query keys stay ignored, never rejected.
        for path in (
            f"/api/projects/{project_id}/alerts?unrecognized=1",
            f"/api/projects/{project_id}/alerts/{alert_id}/checks?unrecognized=1",
            f"/api/projects/{project_id}/alerts/{alert_id}/checks/{run_id}/events"
            "?unrecognized=1",
        ):
            assert client.get(path).status_code == 200, path


def test_alert_error_oracles_preserve_existing_http_behavior(tmp_path: Path) -> None:
    app = create_app(tmp_path / "watch-errors")
    workspace = app.state.workspace
    project_id = str(workspace.create("watch errors")["id"])

    with TestClient(app) as client:
        alert_id = post_alert(
            client,
            f"/api/projects/{project_id}/alerts",
            json={
                "name": "W",
                "query": {"kind": "fts", "q": "x"},
            },
        ).json()["id"]
        drain_index(workspace.get(project_id))
        run_id = client.post(
            f"/api/projects/{project_id}/alerts/{alert_id}/check"
        ).json()["run"]["id"]

        bad_kind = client.post(
            f"/api/projects/{project_id}/alerts",
            json={
                "name": "W2",
                "query": {"kind": "unsupported"},
            },
        )
        assert bad_kind.status_code == 422

        retired_source = client.post(
            f"/api/projects/{project_id}/alerts",
            json={
                "name": "W3",
                "query": {"kind": "fts", "q": "x"},
                "source_view_id": 999,
            },
        )
        assert retired_source.status_code == 422

        missing_alert = client.post(f"/api/projects/{project_id}/alerts/9999/check")
        assert missing_alert.status_code == 404
        assert missing_alert.json() == {"detail": "alert not found"}

        for body in ({}, {"name": "   "}, {"name": None}, {"enabled": None}):
            invalid_patch = client.patch(
                f"/api/projects/{project_id}/alerts/{alert_id}", json=body
            )
            assert invalid_patch.status_code in {400, 422}, invalid_patch.text

        assert (
            client.patch(
                f"/api/projects/{project_id}/alerts/9999", json={"enabled": True}
            ).status_code
            == 404
        )
        assert client.delete(f"/api/projects/{project_id}/alerts/9999").json() == {
            "detail": "alert not found"
        }
        assert (
            client.get(f"/api/projects/{project_id}/alerts/9999/checks").status_code
            == 404
        )
        assert client.get(
            f"/api/projects/{project_id}/alerts/{alert_id}/checks/9999/events"
        ).json() == {"detail": "alert check not found"}

        bad_event_kind = client.get(
            f"/api/projects/{project_id}/alerts/{alert_id}/checks/{run_id}/events"
            "?event_kind=bad"
        )
        assert bad_event_kind.status_code == 400
        assert bad_event_kind.json() == {"detail": "unsupported alert check event kind"}

        # Existing paging bounds stay route-level 422s.
        assert (
            client.get(
                f"/api/projects/{project_id}/alerts/{alert_id}/checks?limit=101"
            ).status_code
            == 422
        )
        assert (
            client.get(
                f"/api/projects/{project_id}/alerts/{alert_id}/checks?offset=-1"
            ).status_code
            == 422
        )
        assert (
            client.get(
                f"/api/projects/{project_id}/alerts/{alert_id}/checks?hits_limit=501"
            ).status_code
            == 422
        )
        assert (
            client.get(
                f"/api/projects/{project_id}/alerts/{alert_id}/checks/{run_id}/events"
                "?limit=101"
            ).status_code
            == 422
        )

        for suffix in ("", f"/{alert_id}/checks"):
            missing = client.get(f"/api/projects/missing/alerts{suffix}")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "no project 'missing'"}


def test_alert_schedule_default_manual_patch_and_invalid_schedule(tmp_path):
    app = create_app(tmp_path / "watch-schedule")
    workspace = app.state.workspace
    project_id = str(workspace.create("schedule")["id"])
    with TestClient(app) as client:
        path = f"/api/projects/{project_id}/alerts"
        created = post_alert(
            client, path, json={"name": "W", "query": {"kind": "fts", "q": "x"}}
        )
        assert created.status_code == 200, created.text
        assert created.json()["schedule"] == "@hourly"
        target = f"{path}/{created.json()['id']}"
        assert client.patch(target, json={"schedule": None}).json()["schedule"] is None
        assert client.patch(target, json={"name": "Renamed"}).json()["schedule"] is None
        assert (
            client.patch(target, json={"schedule": "@daily"}).json()["schedule"]
            == "@daily"
        )
        assert client.patch(target, json={"schedule": "invalid"}).status_code == 400

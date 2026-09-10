"""HTTP-06-F7A: the seven notification read/actor-state routes have exact
typed HTTP owners.

Existing-behavior migration: the DTO graph freezes the executable wire truth
(service/store producers), and the live oracles prove the payloads, defaults,
filter quirks, ignored unknown queries, and error envelopes — including the
raw Python int() message leak — do not move. emit_notification and
deliver_notification are cluster D and stay untouched and untyped.
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
from pydantic import BaseModel, JsonValue, ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError, WireModel
from frisket.server.app import create_app
from frisket.server.notifications.service import (
    NOTIFICATION_PAGE_SCHEMA_VERSION,
    NOTIFICATION_SUMMARY_SCHEMA_VERSION,
)


_CONTRACT_MODULE = "frisket.contracts.http.notifications"

# public_notification_item: seventeen keys.
_ITEM_FIELDS = frozenset(
    {
        "id",
        "source_kind",
        "source_ref",
        "source_event_ids",
        "event_count",
        "event_kinds",
        "title",
        "summary",
        "severity",
        "deep_link",
        "created_at",
        "updated_at",
        "state",
        "seen_at",
        "read_at",
        "acknowledged_at",
        "acknowledged_by",
    }
)
_ITEM_NULLABLE_FIELDS = frozenset(
    {"seen_at", "read_at", "acknowledged_at", "acknowledged_by"}
)
_PAGE_FIELDS = frozenset(
    {
        "schema_version",
        "order",
        "offset",
        "limit",
        "total",
        "has_more",
        "next_offset",
        "notifications",
    }
)
_SUMMARY_SOURCE_REF_FIELDS = frozenset({"source_kind", "source_ref", "unseen"})
_SUMMARY_FIELDS = frozenset(
    {
        "schema_version",
        "total",
        "unseen",
        "seen",
        "read",
        "acknowledged",
        "by_severity",
        "by_source_kind",
        "by_source_ref",
    }
)
# public_actor_state: eight keys.
_ACTOR_STATE_FIELDS = frozenset(
    {
        "notification_id",
        "actor_id",
        "state",
        "seen_at",
        "read_at",
        "acknowledged_at",
        "acknowledged_by",
        "updated_at",
    }
)
_ACTOR_STATE_NULLABLE_FIELDS = frozenset(
    {"seen_at", "read_at", "acknowledged_at", "acknowledged_by"}
)
_FILTER_REQUEST_FIELDS = frozenset(
    {"notification_ids", "source_kind", "source_ref", "before_created_at"}
)

_LIST_ERRORS = frozenset({400, 401, 403, 404, 409, 422, 500})
_SUMMARY_ERRORS = frozenset({401, 403, 404, 409, 500})
_FILTER_POST_ERRORS = frozenset({400, 401, 403, 404, 409, 422, 500})
_ITEM_POST_ERRORS = frozenset({401, 403, 404, 409, 422, 500})

# Captured empirically from the unmodified Base routes: the raw Python int()
# messages leak through normalize_filter_payload into the 400 envelope.
_INT_LEAK_STR = "invalid literal for int() with base 10: 'x'"
_INT_LEAK_NONE = (
    "int() argument must be a string, a bytes-like object or a real number, "
    "not 'NoneType'"
)


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
    browser: bool = True


ROUTES: dict[str, RouteTruth] = {
    "list_notifications": RouteTruth(
        path="/api/projects/{pid}/notifications",
        method="GET",
        errors=_LIST_ERRORS,
        response="NotificationPage",
        query={
            "state": QueryTruth("all"),
            "source_kind": QueryTruth(None),
            "source_ref": QueryTruth(None),
            "severity": QueryTruth(None),
            "offset": QueryTruth(0, {"ge": 0}),
            "limit": QueryTruth(50, {"ge": 1, "le": 100}),
        },
    ),
    "notifications_summary": RouteTruth(
        path="/api/projects/{pid}/notifications/summary",
        method="GET",
        errors=_SUMMARY_ERRORS,
        response="NotificationSummary",
    ),
    "mark_notifications_seen": RouteTruth(
        path="/api/projects/{pid}/notifications/seen",
        method="POST",
        errors=_FILTER_POST_ERRORS,
        response="SeenResult",
        request="NotificationStateFilterRequest",
    ),
    "bulk_ack_notifications": RouteTruth(
        path="/api/projects/{pid}/notifications/ack",
        method="POST",
        errors=_FILTER_POST_ERRORS,
        response="AckResult",
        request="NotificationStateFilterRequest",
        browser=False,
    ),
    # The three per-item POSTs remain BODYLESS.
    "mark_notification_read": RouteTruth(
        path="/api/projects/{pid}/notifications/{notification_id}/read",
        method="POST",
        errors=_ITEM_POST_ERRORS,
        response="NotificationActorState",
    ),
    "ack_notification": RouteTruth(
        path="/api/projects/{pid}/notifications/{notification_id}/ack",
        method="POST",
        errors=_ITEM_POST_ERRORS,
        response="NotificationActorState",
    ),
    "unack_notification": RouteTruth(
        path="/api/projects/{pid}/notifications/{notification_id}/unack",
        method="POST",
        errors=_ITEM_POST_ERRORS,
        response="NotificationActorState",
    ),
}

OPERATION_IDS = frozenset(
    {
        "tenant.list_notifications.get",
        "tenant.notifications_summary.get",
        "tenant.mark_notifications_seen.post",
        "tenant.bulk_ack_notifications.post",
        "tenant.mark_notification_read.post",
        "tenant.ack_notification.post",
        "tenant.unack_notification.post",
    }
)

_ROLE_BY_METHOD = {"GET": "viewer", "POST": "editor"}
POLICY_IDENTITIES = frozenset(
    (
        f"tenant.{name}.{truth.method.lower()}",
        "tenant",
        name,
        truth.method,
        "session_or_pat",
        _ROLE_BY_METHOD[truth.method],
        truth.browser,
        (),
        False,
    )
    for name, truth in ROUTES.items()
)


def _contracts() -> ModuleType:
    spec = importlib.util.find_spec(_CONTRACT_MODULE)
    if spec is None:
        pytest.fail(
            "INTENDED_F7A_RED: F7A notifications HTTP contract module is absent "
            f"({_CONTRACT_MODULE} must exist)",
            pytrace=False,
        )
    return importlib.import_module(_CONTRACT_MODULE)


def _model(module: ModuleType, name: str) -> type[BaseModel]:
    candidate = getattr(module, name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
        pytest.fail(
            f"INTENDED_F7A_RED: notifications.{name} must be a Pydantic model",
            pytrace=False,
        )
    return candidate


def _assert_closed_annotation(annotation: object, *, label: str) -> None:
    """Reject an unbounded DTO field without banning ``JsonValue`` leaves."""

    if annotation is Any:
        pytest.fail(f"INTENDED_F7A_RED: {label} must not use Any", pytrace=False)
    origin = get_origin(annotation)
    if (
        annotation in (dict, list, object)
        or origin in (dict, list)
        and not get_args(annotation)
    ):
        pytest.fail(
            f"INTENDED_F7A_RED: {label} must not use an unparameterized object",
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


def _item_payload(item_id: int) -> dict[str, Any]:
    return {
        "id": item_id,
        "source_kind": "watch",
        "source_ref": {"watch_id": 5},
        "source_event_ids": [1, 2],
        "event_count": 2,
        "event_kinds": ["row_entered"],
        "title": "Budget watch",
        "summary": "2 new rows",
        "severity": "warning",
        "deep_link": {"view": "watch", "watch_id": 5},
        "created_at": "2026-08-11 00:00:00",
        "updated_at": "2026-08-11 00:00:01",
        "state": "unseen",
        "seen_at": None,
        "read_at": None,
        "acknowledged_at": None,
        "acknowledged_by": None,
    }


def _actor_state_payload(notification_id: int) -> dict[str, Any]:
    return {
        "notification_id": notification_id,
        "actor_id": "local:project",
        "state": "read",
        "seen_at": "2026-08-11 00:00:02",
        "read_at": "2026-08-11 00:00:02",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "updated_at": "2026-08-11 00:00:02",
    }


def _emit(client: TestClient, project_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "source_kind": "system",
        "source_ref": {"sys": 1},
        "source_event_ids": [],
        "title": "T",
        "summary": "S",
        "severity": "info",
        "deep_link": {},
        "dedupe_key": "k",
        **overrides,
    }
    response = client.post(f"/api/projects/{project_id}/notifications/emit", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def routes_by_name(tmp_path_factory: pytest.TempPathFactory) -> dict[str, APIRoute]:
    app = create_app(tmp_path_factory.mktemp("notification-http-contracts"))
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


# ---------------------------------------------------------------------------
# policy identity and browser membership
# ---------------------------------------------------------------------------


def test_seven_notification_routes_keep_policy_identity_and_browser_membership() -> (
    None
):
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
    # Cluster D plus the retained bulk-ack route stay off the browser surface.
    for excluded in (
        "tenant.emit_notification.post",
        "tenant.deliver_notification.post",
        "tenant.bulk_ack_notifications.post",
    ):
        entry = next(e for e in BASE_ENDPOINT_CATALOG if e.id == excluded)
        assert entry.browser_client is False, (
            f"{excluded} is cluster D and must stay unprojected"
        )


# ---------------------------------------------------------------------------
# route declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_notification_routes_declare_exact_typed_route_shapes(
    name: str, routes_by_name: dict[str, APIRoute]
) -> None:
    assert set(routes_by_name) == set(ROUTES)
    route, truth = routes_by_name[name], ROUTES[name]
    assert route.methods == {truth.method}
    assert route.path == truth.path
    assert (route.status_code or 200) == 200

    response = route.response_model
    assert isinstance(response, type) and issubclass(response, BaseModel), (
        f"INTENDED_F7A_RED: {name} must declare a typed response model"
    )
    assert response.__module__ == _CONTRACT_MODULE
    assert response.__name__ == truth.response
    assert set(route.responses) == set(truth.errors)
    assert all(value == {"model": HttpError} for value in route.responses.values())

    body_models = [field.field_info.annotation for field in route.dependant.body_params]
    if truth.request is None:
        # The per-item POSTs stay BODYLESS; the GETs stay body-free.
        assert body_models == []
        assert route.body_field is None
    else:
        assert len(body_models) == 1
        assert body_models[0] is not None, (
            f"INTENDED_F7A_RED: {name} must declare its request model"
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
# request DTO stays coercive with the frozen filter semantics
# ---------------------------------------------------------------------------


def test_notification_filter_request_stays_coercive_with_existing_defaults() -> None:
    contracts = _contracts()
    request = _model(contracts, "NotificationStateFilterRequest")
    assert request.model_config.get("strict") in (None, False), (
        "NotificationStateFilterRequest must stay coercive"
    )
    assert request.model_config.get("extra") in (None, "ignore"), (
        "NotificationStateFilterRequest must keep ignoring unknown request keys"
    )
    assert set(request.model_fields) == _FILTER_REQUEST_FIELDS
    for field_name, model_field in request.model_fields.items():
        _assert_closed_annotation(
            model_field.annotation,
            label=f"NotificationStateFilterRequest.{field_name}",
        )
        assert model_field.alias in (None, field_name)
        assert not model_field.is_required(), (
            f"NotificationStateFilterRequest.{field_name} must stay optional"
        )
    assert {
        name: model_field.annotation
        for name, model_field in request.model_fields.items()
    } == {
        "notification_ids": list[int] | None,
        "source_kind": str | None,
        "source_ref": dict[str, JsonValue] | None,
        "before_created_at": str | None,
    }

    # An empty body is the everything-filter; all four keys default to None.
    empty = request.model_validate({})
    assert empty.model_dump() == {
        "notification_ids": None,
        "source_kind": None,
        "source_ref": None,
        "before_created_at": None,
    }
    assert empty.model_fields_set == set()
    # Coercion oracle: "7" coerces, unknown keys drop.
    coerced = request.model_validate({"notification_ids": ["7"], "unrecognized": 1})
    assert coerced.notification_ids == [7]
    assert coerced.model_fields_set == {"notification_ids"}
    # The frozen no-predicate shapes stay representable: empty list and empty
    # object pass through unchanged.
    passthrough = request.model_validate({"notification_ids": [], "source_ref": {}})
    assert passthrough.notification_ids == []
    assert passthrough.source_ref == {}


# ---------------------------------------------------------------------------
# response DTO graph — 8 models, 51 fields
# ---------------------------------------------------------------------------


def test_notification_item_page_and_summary_freeze_the_wire_graph() -> None:
    contracts = _contracts()
    item = _model(contracts, "NotificationItem")
    page = _model(contracts, "NotificationPage")
    source_ref = _model(contracts, "NotificationSummarySourceRef")
    summary = _model(contracts, "NotificationSummary")

    for model in (item, page, source_ref, summary):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)

    item_annotations = {
        "id": int,
        "source_kind": str,
        "source_ref": dict[str, JsonValue],
        "source_event_ids": list[int],
        "event_count": int,
        "event_kinds": list[str],
        "title": str,
        "summary": str,
        "severity": str,
        "deep_link": dict[str, JsonValue],
        "created_at": str,
        "updated_at": str,
        # The service only ever writes the four row states; the state QUERY
        # param stays str (it accepts arbitrary strings and the service
        # raises the frozen 400).
        "state": Literal["unseen", "seen", "read", "acknowledged"],
        "seen_at": str | None,
        "read_at": str | None,
        "acknowledged_at": str | None,
        "acknowledged_by": str | None,
    }
    assert set(item.model_fields) == _ITEM_FIELDS
    assert len(item.model_fields) == 17
    assert {
        name: model_field.annotation for name, model_field in item.model_fields.items()
    } == item_annotations
    _assert_required_nullable(item, _ITEM_NULLABLE_FIELDS)

    assert set(page.model_fields) == _PAGE_FIELDS
    assert {
        name: model_field.annotation for name, model_field in page.model_fields.items()
    } == {
        "schema_version": Literal["frisket.notifications_page.v1"],
        "order": Literal["desc"],
        "offset": int,
        "limit": int,
        "total": int,
        "has_more": bool,
        "next_offset": int | None,
        "notifications": list[item],
    }
    _assert_required_nullable(page, frozenset({"next_offset"}))

    assert set(source_ref.model_fields) == _SUMMARY_SOURCE_REF_FIELDS
    assert {
        name: model_field.annotation
        for name, model_field in source_ref.model_fields.items()
    } == {
        "source_kind": str,
        "source_ref": dict[str, JsonValue],
        "unseen": int,
    }

    assert set(summary.model_fields) == _SUMMARY_FIELDS
    assert {
        name: model_field.annotation
        for name, model_field in summary.model_fields.items()
    } == {
        "schema_version": Literal["frisket.notifications_summary.v1"],
        "total": int,
        "unseen": int,
        "seen": int,
        "read": int,
        "acknowledged": int,
        "by_severity": dict[str, int],
        "by_source_kind": dict[str, int],
        "by_source_ref": list[source_ref],
    }
    # The literals are the producers' own constants.
    assert NOTIFICATION_PAGE_SCHEMA_VERSION == "frisket.notifications_page.v1"
    assert NOTIFICATION_SUMMARY_SCHEMA_VERSION == "frisket.notifications_summary.v1"

    item_payload = _item_payload(1)
    assert item.model_validate(item_payload).model_dump() == item_payload
    page_payload = {
        "schema_version": "frisket.notifications_page.v1",
        "order": "desc",
        "offset": 0,
        "limit": 50,
        "total": 1,
        "has_more": False,
        "next_offset": None,
        "notifications": [item_payload],
    }
    page_wire = page.model_validate(page_payload)
    assert page_wire.model_dump() == page_payload
    assert isinstance(page_wire.notifications[0], item)
    summary_payload = {
        "schema_version": "frisket.notifications_summary.v1",
        "total": 2,
        "unseen": 1,
        "seen": 1,
        "read": 0,
        "acknowledged": 0,
        "by_severity": {"info": 1, "warning": 1},
        "by_source_kind": {"system": 1, "watch": 1},
        "by_source_ref": [
            {"source_kind": "watch", "source_ref": {"watch_id": 5}, "unseen": 1}
        ],
    }
    summary_wire = summary.model_validate(summary_payload)
    assert summary_wire.model_dump() == summary_payload
    assert isinstance(summary_wire.by_source_ref[0], source_ref)

    for name in _ITEM_NULLABLE_FIELDS:
        assert (
            item.model_validate({**item_payload, name: None}).model_dump()[name] is None
        )
    for name in ("created_at", "updated_at", "state", "title", "id"):
        with pytest.raises(ValidationError):
            item.model_validate({**item_payload, name: None})
    with pytest.raises(ValidationError):
        item.model_validate({**item_payload, "id": "1"})
    with pytest.raises(ValidationError):
        item.model_validate({**item_payload, "unapproved_item_field": 1})
    with pytest.raises(ValidationError):
        page.model_validate({**page_payload, "order": "asc"})
    with pytest.raises(ValidationError):
        page.model_validate(
            {**page_payload, "schema_version": "frisket.notifications_page.v2"}
        )
    with pytest.raises(ValidationError):
        summary.model_validate(
            {**summary_payload, "schema_version": "frisket.notifications_page.v1"}
        )
    with pytest.raises(ValidationError):
        summary.model_validate({**summary_payload, "by_severity": {"info": "1x"}})
    with pytest.raises(ValidationError):
        summary.model_validate({**summary_payload, "unapproved_summary_field": 1})
    _assert_missing_fields_rejected(item, item_payload, _ITEM_FIELDS)
    _assert_missing_fields_rejected(page, page_payload, _PAGE_FIELDS)
    _assert_missing_fields_rejected(summary, summary_payload, _SUMMARY_FIELDS)
    _assert_missing_fields_rejected(
        source_ref,
        summary_payload["by_source_ref"][0],
        _SUMMARY_SOURCE_REF_FIELDS,
    )
    # source_ref and deep_link stay recursive JSON.
    for ref in ({}, {"watch_id": 5, "nested": [1, {"a": None}]}):
        assert item.model_validate({**item_payload, "source_ref": ref}).source_ref == (
            ref
        )


def test_actor_state_and_count_results_freeze_their_shapes() -> None:
    contracts = _contracts()
    actor_state = _model(contracts, "NotificationActorState")
    seen_result = _model(contracts, "SeenResult")
    ack_result = _model(contracts, "AckResult")

    for model in (actor_state, seen_result, ack_result):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)

    assert set(actor_state.model_fields) == _ACTOR_STATE_FIELDS
    assert len(actor_state.model_fields) == 8
    assert {
        name: model_field.annotation
        for name, model_field in actor_state.model_fields.items()
    } == {
        "notification_id": int,
        "actor_id": str,
        "state": Literal["unseen", "seen", "read", "acknowledged"],
        "seen_at": str | None,
        "read_at": str | None,
        "acknowledged_at": str | None,
        "acknowledged_by": str | None,
        "updated_at": str,
    }
    _assert_required_nullable(actor_state, _ACTOR_STATE_NULLABLE_FIELDS)
    # Private-consumer confirmation target: actor_id is REQUIRED and str —
    # a downstream consumer reads it from every per-item POST response.
    assert actor_state.model_fields["actor_id"].is_required()
    with pytest.raises(ValidationError):
        actor_state.model_validate(
            {k: v for k, v in _actor_state_payload(1).items() if k != "actor_id"}
        )

    # The count results are bare one-field objects with NO schema_version —
    # frozen as-is, exactly what mark_seen/bulk_ack return today.
    assert set(seen_result.model_fields) == {"seen_count"}
    assert seen_result.model_fields["seen_count"].annotation is int
    assert set(ack_result.model_fields) == {"acknowledged_count"}
    assert ack_result.model_fields["acknowledged_count"].annotation is int

    state_payload = _actor_state_payload(1)
    assert actor_state.model_validate(state_payload).model_dump() == state_payload
    assert seen_result.model_validate({"seen_count": 2}).model_dump() == {
        "seen_count": 2
    }
    assert ack_result.model_validate({"acknowledged_count": 0}).model_dump() == {
        "acknowledged_count": 0
    }
    with pytest.raises(ValidationError):
        actor_state.model_validate({**state_payload, "updated_at": None})
    with pytest.raises(ValidationError):
        actor_state.model_validate({**state_payload, "unapproved_state_field": 1})
    with pytest.raises(ValidationError):
        seen_result.model_validate({"seen_count": 2, "schema_version": "v1"})
    with pytest.raises(ValidationError):
        ack_result.model_validate({})
    _assert_missing_fields_rejected(actor_state, state_payload, _ACTOR_STATE_FIELDS)

    # Exactly the mandated 8-model / 51-field graph, nothing else public.
    item = _model(contracts, "NotificationItem")
    page = _model(contracts, "NotificationPage")
    source_ref = _model(contracts, "NotificationSummarySourceRef")
    summary = _model(contracts, "NotificationSummary")
    request = _model(contracts, "NotificationStateFilterRequest")
    assert (
        len(item.model_fields)
        + len(page.model_fields)
        + len(source_ref.model_fields)
        + len(summary.model_fields)
        + len(actor_state.model_fields)
        + len(request.model_fields)
        + len(seen_result.model_fields)
        + len(ack_result.model_fields)
        == 51
    )


# ---------------------------------------------------------------------------
# live producers keep their current payloads and semantics
# ---------------------------------------------------------------------------


def test_live_notification_routes_preserve_their_current_payloads(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "live-notifications")
    workspace = app.state.workspace
    project_id = str(workspace.create("live notifications")["id"])
    project = workspace.get(project_id)

    with TestClient(app) as client:
        empty = client.get(f"/api/projects/{project_id}/notifications").json()
        assert empty == {
            "schema_version": "frisket.notifications_page.v1",
            "order": "desc",
            "offset": 0,
            "limit": 50,
            "total": 0,
            "has_more": False,
            "next_offset": None,
            "notifications": [],
        }

        _emit(client, project_id)
        # A watch-sourced item enters through the store; the read routes see
        # it identically to an emitted one.
        project.db.execute(
            "INSERT INTO notification_items (source_kind, source_ref, "
            "source_event_ids, event_count, event_kinds, title, summary, "
            "severity, deep_link, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "watch",
                '{"watch_id": 5}',
                "[]",
                1,
                "[]",
                "W",
                "s",
                "warning",
                "{}",
                "w1",
            ),
        )
        project.db.commit()

        listed = client.get(f"/api/projects/{project_id}/notifications").json()
        assert set(listed) == _PAGE_FIELDS
        # Private-consumer confirmation target: the envelope key is literally
        # `notifications` (never items/rows), and every row exposes id+state.
        assert "notifications" in listed
        assert all(
            isinstance(row["id"], int) and isinstance(row["state"], str)
            for row in listed["notifications"]
        )
        assert listed["total"] == 2
        assert [set(row) for row in listed["notifications"]] == [_ITEM_FIELDS] * 2
        system_item = next(
            row for row in listed["notifications"] if row["source_kind"] == "system"
        )
        assert system_item["source_ref"] == {"sys": 1}
        assert system_item["state"] == "unseen"
        assert system_item["seen_at"] is None
        assert system_item["acknowledged_by"] is None

        filtered = client.get(
            f"/api/projects/{project_id}/notifications?source_kind=watch"
        ).json()
        assert [row["source_kind"] for row in filtered["notifications"]] == ["watch"]

        # Summary: watch-only by_source_ref, frozen by SQL — the system item
        # counts in by_source_kind but never enters by_source_ref.
        summary = client.get(f"/api/projects/{project_id}/notifications/summary").json()
        assert set(summary) == _SUMMARY_FIELDS
        assert summary["schema_version"] == "frisket.notifications_summary.v1"
        assert summary["total"] == 2
        assert summary["unseen"] == 2
        assert summary["by_source_kind"] == {"system": 1, "watch": 1}
        assert summary["by_source_ref"] == [
            {"source_kind": "watch", "source_ref": {"watch_id": 5}, "unseen": 1}
        ]

        # Per-item actor-state lifecycle.
        notification_id = system_item["id"]
        # Private-consumer confirmation target: a consumer POSTs with NO body
        # at all and reads actor_id from the response. TestClient sends no
        # request body here — this is the body-free form, not a vestigial {}.
        read_response = client.post(
            f"/api/projects/{project_id}/notifications/{notification_id}/read"
        )
        assert read_response.request.content == b""
        read = read_response.json()
        assert set(read) == _ACTOR_STATE_FIELDS
        assert read["notification_id"] == notification_id
        assert read["actor_id"] == "local:project"
        assert read["state"] == "read"
        assert read["seen_at"] is not None
        assert read["acknowledged_at"] is None
        acked = client.post(
            f"/api/projects/{project_id}/notifications/{notification_id}/ack"
        ).json()
        assert acked["state"] == "acknowledged"
        assert acked["acknowledged_at"] is not None
        unacked = client.post(
            f"/api/projects/{project_id}/notifications/{notification_id}/unack"
        ).json()
        assert unacked["state"] == "read"
        assert unacked["acknowledged_at"] is None

        # FROZEN filter semantics: empty notification_ids and empty source_ref
        # add NO predicate — the everything-filter reaches every item, and
        # marking seen transitions only the still-unseen ones. The system item
        # is already read from the lifecycle above, so exactly the watch item
        # transitions.
        seen = client.post(
            f"/api/projects/{project_id}/notifications/seen",
            json={"notification_ids": [], "source_ref": {}},
        )
        assert seen.status_code == 200
        assert seen.json() == {"seen_count": 1}
        after = client.get(f"/api/projects/{project_id}/notifications/summary").json()
        assert after["unseen"] == 0

        bulk = client.post(
            f"/api/projects/{project_id}/notifications/ack",
            json={"source_kind": "watch"},
        )
        assert bulk.status_code == 200
        assert bulk.json() == {"acknowledged_count": 1}

        # Unknown query keys stay ignored, never rejected.
        for path in (
            f"/api/projects/{project_id}/notifications?unrecognized=1",
            f"/api/projects/{project_id}/notifications/summary?unrecognized=1",
        ):
            assert client.get(path).status_code == 200, path


def test_notification_error_oracles_preserve_existing_http_behavior(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "notification-errors")
    workspace = app.state.workspace
    project_id = str(workspace.create("notification errors")["id"])

    with TestClient(app) as client:
        bad_state = client.get(f"/api/projects/{project_id}/notifications?state=bogus")
        assert bad_state.status_code == 400
        assert bad_state.json() == {"detail": "unsupported notification state"}

        for raw in ("notjson", "[1]"):
            bad_ref = client.get(
                f"/api/projects/{project_id}/notifications?source_ref={raw}"
            )
            assert bad_ref.status_code == 400
            assert bad_ref.json() == {"detail": "source_ref must be a JSON object"}

        # The raw Python int() messages LEAK through filter normalization —
        # frozen byte-for-byte, captured from the unmodified Base routes.
        leak_str = client.post(
            f"/api/projects/{project_id}/notifications/seen",
            json={"notification_ids": ["x"]},
        )
        assert leak_str.status_code == 400
        assert leak_str.json() == {"detail": _INT_LEAK_STR}
        leak_none = client.post(
            f"/api/projects/{project_id}/notifications/ack",
            json={"notification_ids": [None]},
        )
        assert leak_none.status_code == 400
        assert leak_none.json() == {"detail": _INT_LEAK_NONE}

        not_array = client.post(
            f"/api/projects/{project_id}/notifications/seen",
            json={"notification_ids": "nope"},
        )
        assert not_array.status_code == 400
        assert not_array.json() == {"detail": "notification_ids must be an array"}
        ref_not_object = client.post(
            f"/api/projects/{project_id}/notifications/ack",
            json={"source_ref": [1]},
        )
        assert ref_not_object.status_code == 400
        assert ref_not_object.json() == {"detail": "source_ref must be an object"}

        for suffix in ("read", "ack", "unack"):
            missing = client.post(
                f"/api/projects/{project_id}/notifications/9999/{suffix}"
            )
            assert missing.status_code == 404
            assert missing.json() == {"detail": "notification not found"}

        # Existing paging bounds stay route-level 422s.
        for query in ("limit=101", "limit=0", "offset=-1"):
            assert (
                client.get(
                    f"/api/projects/{project_id}/notifications?{query}"
                ).status_code
                == 422
            ), query

        missing_project = client.get("/api/projects/missing/notifications")
        assert missing_project.status_code == 404
        assert missing_project.json() == {"detail": "no project 'missing'"}

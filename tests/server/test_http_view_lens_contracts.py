"""HTTP-06-F5: saved-view and saved-lens routes have exact typed HTTP owners.

Nine of the twelve routes become browser-projected typed owners; the other
three (get_view_ep, get_lens_ep, patch_lens) stay deliberately non-browser and
that negative space is fenced here, not left implicit.
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
from frisket.preview.query import (
    MAX_QUERY_PREVIEW_LIMIT,
    QUERY_PREVIEW_EVALUATOR,
    QUERY_PREVIEW_SCHEMA_VERSION,
)
from frisket.server.app import create_app
from frisket.server.routes.views import register_view_lens_routes


_CONTRACT_MODULE = "frisket.contracts.http.views_lenses"

# One closed row shape serves BOTH saved views and saved lenses: the two
# tables are column-identical (engine/store/schema.py) and both services
# return `dict(row)` with the JSON `spec` decoded in place.
_ROW_FIELDS = frozenset(
    {
        "id",
        "name",
        "sheet_id",
        "spec",
        "op_id",
        "created_at",
        "updated_at",
    }
)
_VIEW_ROW_NULLABLE_FIELDS = frozenset({"op_id"})
_LENS_ROW_NULLABLE_FIELDS = frozenset({"sheet_id", "op_id"})
_DELETE_FIELDS = frozenset({"ok", "deleted"})
_RESOLVE_FIELDS = frozenset(
    {
        "lens_id",
        "schema_version",
        "query",
        "query_hash",
        "sheet_id",
        "row_ids",
        "row_count",
        "total",
        "offset",
        "limit",
        "evaluator",
        "scores",
    }
)
_SCORE_FIELDS = frozenset({"distance", "score"})
_EVALUATOR_FIELDS = frozenset({"kind", "version"})

# Adjudicated seam 4: the resolve response is TWELVE fields. schema_version is
# a required producer field, so freezing it is preservation, not expansion.
assert len(_RESOLVE_FIELDS) == 12

# Saved View creation requires an object filter. The normal FastAPI/Pydantic
# boundary rejects a null body before service dispatch.
_FILTER_NULL_422_BODY = {
    "detail": [
        {
            "type": "dict_type",
            "loc": ["body", "filter"],
            "msg": "Input should be a valid dictionary",
            "input": None,
        }
    ]
}

_VIEW_ERRORS = frozenset({401, 403, 404, 409, 422, 500})
_LENS_ERRORS = frozenset({400, 401, 403, 404, 409, 422, 500})


@dataclass(frozen=True)
class RouteTruth:
    path: str
    method: str
    browser: bool
    errors: frozenset[int] | None = None
    response: str | None = None
    request: str | None = None
    query_defaults: dict[str, Any] = field(default_factory=dict)


ROUTES: dict[str, RouteTruth] = {
    "list_views": RouteTruth(
        path="/api/projects/{pid}/views",
        method="GET",
        browser=True,
        errors=_VIEW_ERRORS,
        response="SavedViewList",
        query_defaults={"sheet_id": None},
    ),
    "create_view": RouteTruth(
        path="/api/projects/{pid}/views",
        method="POST",
        browser=True,
        errors=_VIEW_ERRORS,
        response="SavedView",
        request="SavedViewCreateRequest",
    ),
    # Retained non-browser: typed and fenced, never projected.
    "get_view_ep": RouteTruth(
        path="/api/projects/{pid}/views/{view_id}",
        method="GET",
        browser=False,
        errors=_VIEW_ERRORS,
        response="SavedView",
    ),
    "patch_view": RouteTruth(
        path="/api/projects/{pid}/views/{view_id}",
        method="PATCH",
        browser=True,
        errors=_VIEW_ERRORS,
        response="SavedView",
        request="SavedViewRenameRequest",
    ),
    "replace_view_definition": RouteTruth(
        path="/api/projects/{pid}/views/{view_id}/definition",
        method="PUT",
        browser=True,
        errors=_VIEW_ERRORS,
        response="SavedView",
        request="SavedViewDefinitionReplaceRequest",
    ),
    "delete_view_ep": RouteTruth(
        path="/api/projects/{pid}/views/{view_id}",
        method="DELETE",
        browser=True,
        errors=_VIEW_ERRORS,
        response="SavedViewDelete",
    ),
    "list_lenses": RouteTruth(
        path="/api/projects/{pid}/lenses",
        method="GET",
        browser=True,
        errors=_VIEW_ERRORS,
        response="SavedLensList",
        query_defaults={"sheet_id": None},
    ),
    "create_lens": RouteTruth(
        path="/api/projects/{pid}/lenses",
        method="POST",
        browser=True,
        errors=_LENS_ERRORS,
        response="SavedLens",
        request="SavedLensCreateRequest",
    ),
    "get_lens_ep": RouteTruth(
        path="/api/projects/{pid}/lenses/{lens_id}",
        method="GET",
        browser=False,
        errors=_VIEW_ERRORS,
        response="SavedLens",
    ),
    # patch_lens stays non-browser but MUST bind the request model, so
    # SavedLensPatchRequest cannot be a compliant model that nothing uses.
    "patch_lens": RouteTruth(
        path="/api/projects/{pid}/lenses/{lens_id}",
        method="PATCH",
        browser=False,
        errors=_LENS_ERRORS,
        response="SavedLens",
        request="SavedLensPatchRequest",
    ),
    "delete_lens_ep": RouteTruth(
        path="/api/projects/{pid}/lenses/{lens_id}",
        method="DELETE",
        browser=False,
        errors=_VIEW_ERRORS,
        response="SavedLensDelete",
    ),
    "resolve_lens": RouteTruth(
        path="/api/projects/{pid}/lenses/{lens_id}/resolve",
        method="GET",
        browser=True,
        errors=_LENS_ERRORS,
        response="SavedLensResolved",
        query_defaults={"limit": 50, "offset": 0},
    ),
}

BROWSER_OPERATION_IDS = frozenset(
    {
        "tenant.list_views.get",
        "tenant.create_view.post",
        "tenant.patch_view.patch",
        "tenant.replace_view_definition.put",
        "tenant.delete_view_ep.delete",
        "tenant.list_lenses.get",
        "tenant.create_lens.post",
        "tenant.resolve_lens.get",
    }
)
# Deliberate negative space: these three are NOT browser-visible after F5.
NON_BROWSER_OPERATION_IDS = frozenset(
    {
        "tenant.get_view_ep.get",
        "tenant.get_lens_ep.get",
        "tenant.patch_lens.patch",
        "tenant.delete_lens_ep.delete",
    }
)
OPERATION_IDS = BROWSER_OPERATION_IDS | NON_BROWSER_OPERATION_IDS

_ROLE_BY_METHOD = {
    "GET": "viewer",
    "POST": "editor",
    "PATCH": "editor",
    "PUT": "editor",
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
            "INTENDED_F5_RED: F5 views/lenses HTTP contract module is absent "
            f"({_CONTRACT_MODULE} must exist)",
            pytrace=False,
        )
    return importlib.import_module(_CONTRACT_MODULE)


def _model(module: ModuleType, name: str) -> type[BaseModel]:
    candidate = getattr(module, name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
        pytest.fail(
            f"INTENDED_F5_RED: views_lenses.{name} must be a Pydantic model",
            pytrace=False,
        )
    return candidate


def _assert_closed_annotation(annotation: object, *, label: str) -> None:
    """Reject an unbounded DTO field without banning ``JsonValue`` leaves."""

    if annotation is Any:
        pytest.fail(f"INTENDED_F5_RED: {label} must not use Any", pytrace=False)
    origin = get_origin(annotation)
    if (
        annotation in (dict, list, object)
        or origin in (dict, list)
        and not get_args(annotation)
    ):
        pytest.fail(
            f"INTENDED_F5_RED: {label} must not use an unparameterized object",
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
    """Collect the ge/le constraints FastAPI carries for a query parameter."""

    bounds: dict[str, int] = {}
    for metadata in query_field.field_info.metadata:
        for name in ("ge", "le", "gt", "lt"):
            value = getattr(metadata, name, None)
            if value is not None:
                bounds[name] = value
    return bounds


def _row_payload(row_id: int, *, sheet_id: int | None = 3) -> dict[str, Any]:
    return {
        "id": row_id,
        "name": f"row-{row_id}",
        "sheet_id": sheet_id,
        "spec": {"filter": {"q": "x"}, "sort": [["a", "asc"]]},
        "op_id": 11,
        "created_at": "2026-08-10 00:00:00",
        "updated_at": "2026-08-10 00:01:00",
    }


def _resolve_payload() -> dict[str, Any]:
    return {
        "lens_id": 5,
        "schema_version": "frisket.query_preview.v1",
        "query": {
            "schema_version": "frisket.query.v1",
            "kind": "sheet.filter",
            "scope": {"kind": "sheet", "sheet_id": 3},
            "filter": {},
        },
        "query_hash": "sha256:abc",
        "sheet_id": 3,
        "row_ids": [7, 8],
        "row_count": 2,
        "total": 9,
        "offset": 1,
        "limit": 50,
        "evaluator": {"kind": "frisket.querysets.sheet_filter", "version": "v1"},
        "scores": {
            "7": {"distance": 0.25, "score": 0.75},
            "8": {"distance": None, "score": None},
        },
    }


@pytest.fixture(scope="module")
def routes_by_name(tmp_path_factory: pytest.TempPathFactory) -> dict[str, APIRoute]:
    app = create_app(tmp_path_factory.mktemp("view-lens-http-contracts"))
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


# ---------------------------------------------------------------------------
# policy identity + the nine-member browser fence
# ---------------------------------------------------------------------------


def test_twelve_view_lens_routes_keep_policy_identity_and_nine_browser_members() -> (
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
    assert {entry.id for entry in entries if entry.browser_client} == (
        BROWSER_OPERATION_IDS
    )
    # Negative space: no saved-view/saved-lens route outside the nine may
    # become browser-visible, and the three fenced routes stay off.
    assert {
        entry.id
        for entry in BASE_ENDPOINT_CATALOG
        if entry.route_name in ROUTES and entry.browser_client
    } == BROWSER_OPERATION_IDS
    assert not {
        entry.id
        for entry in BASE_ENDPOINT_CATALOG
        if entry.id in NON_BROWSER_OPERATION_IDS and entry.browser_client
    }


# ---------------------------------------------------------------------------
# route declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_view_lens_routes_declare_exact_typed_route_shapes(
    name: str, routes_by_name: dict[str, APIRoute]
) -> None:
    """All twelve routes become typed owners. Nine are projected; the other
    three are typed and fenced, never browser-visible."""

    assert set(routes_by_name) == set(ROUTES)
    route, truth = routes_by_name[name], ROUTES[name]
    assert route.methods == {truth.method}
    assert route.path == truth.path
    assert (route.status_code or 200) == 200

    response = route.response_model
    assert isinstance(response, type) and issubclass(response, BaseModel), (
        f"INTENDED_F5_RED: {name} must declare a typed response model"
    )
    assert response.__module__ == _CONTRACT_MODULE
    assert response.__name__ == truth.response
    assert truth.errors is not None
    assert set(route.responses) == set(truth.errors)
    assert all(value == {"model": HttpError} for value in route.responses.values())

    body_models = [field.field_info.annotation for field in route.dependant.body_params]
    if truth.request is None:
        assert body_models == []
        assert route.body_field is None
    else:
        assert len(body_models) == 1
        assert body_models[0] is not None, (
            f"INTENDED_F5_RED: {name} must declare its request model"
        )
        assert body_models[0].__module__ == _CONTRACT_MODULE
        assert body_models[0].__name__ == truth.request

    query = {param.name: param for param in route.dependant.query_params}
    assert set(query) == set(truth.query_defaults)
    for query_name, default in truth.query_defaults.items():
        assert query[query_name].default == default
        # resolve_lens is called with limit=5000 by a live caller; the preview
        # spine clamps internally. A route-level le would turn that into 422.
        assert _query_bounds(query[query_name]) == {}, (
            f"{name}.{query_name} must stay an unbounded query parameter"
        )
    if name == "resolve_lens":
        assert query["limit"].field_info.annotation is int
        assert query["offset"].field_info.annotation is int


@pytest.mark.parametrize(
    "name", sorted(name for name, truth in ROUTES.items() if not truth.browser)
)
def test_fenced_view_lens_routes_stay_unprojected(name: str) -> None:
    entry = next(entry for entry in BASE_ENDPOINT_CATALOG if entry.route_name == name)
    assert entry.browser_client is False, (
        f"{name} is retained NON-BROWSER by F5: typing it must not project it"
    )


def test_no_view_lens_contract_model_exists_without_a_route_that_uses_it(
    routes_by_name: dict[str, APIRoute],
) -> None:
    """The unused-compliant-model fence. Every public model the DTO module
    defines must be reachable from one of the twelve live route declarations,
    so a model cannot satisfy the graph tests while nothing binds it."""

    contracts = _contracts()
    declared = {
        name: value
        for name, value in vars(contracts).items()
        if not name.startswith("_")
        and isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == _CONTRACT_MODULE
    }
    assert declared, "INTENDED_F5_RED: views_lenses must define wire models"

    bound: set[type[BaseModel]] = set()
    for route in routes_by_name.values():
        candidates = [route.response_model]
        candidates.extend(
            field.field_info.annotation for field in route.dependant.body_params
        )
        for candidate in candidates:
            if (
                isinstance(candidate, type)
                and issubclass(candidate, BaseModel)
                and candidate.__module__ == _CONTRACT_MODULE
            ):
                bound.add(candidate)

    def _expand(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
        if model in seen:
            return
        seen.add(model)
        for model_field in model.model_fields.values():
            stack = [model_field.annotation]
            while stack:
                annotation = stack.pop()
                if (
                    isinstance(annotation, type)
                    and issubclass(annotation, BaseModel)
                    and annotation.__module__ == _CONTRACT_MODULE
                ):
                    _expand(annotation, seen)
                stack.extend(get_args(annotation))

    reachable: set[type[BaseModel]] = set()
    for model in bound:
        _expand(model, reachable)

    unused = sorted(name for name, value in declared.items() if value not in reachable)
    assert not unused, (
        "INTENDED_F5_RED: every public views_lenses model must be reachable "
        f"from a live route declaration; unused: {unused}"
    )
    assert {value.__name__ for value in reachable} == set(declared)


# ---------------------------------------------------------------------------
# request DTOs stay coercive and keep provided-field tracking
# ---------------------------------------------------------------------------


def test_view_lens_request_models_stay_coercive_and_track_provided_fields() -> None:
    contracts = _contracts()
    create = _model(contracts, "SavedViewCreateRequest")
    rename = _model(contracts, "SavedViewRenameRequest")
    replace = _model(contracts, "SavedViewDefinitionReplaceRequest")
    for model in (create, rename, replace):
        assert model.model_config.get("strict") is True
        assert model.model_config.get("extra") == "forbid"
    assert (
        create.model_validate({"name": " V ", "sheet_id": 3, "filter": {}}).name == "V"
    )
    assert rename.model_validate({"name": " V2 "}).name == "V2"
    replacement = replace.model_validate(
        {"filter": {}, "sort": None, "columns": None, "column_groups": None}
    )
    assert replacement.model_dump() == {
        "filter": {},
        "sort": None,
        "columns": None,
        "column_groups": None,
    }
    for invalid in (
        {"name": "V", "sheet_id": 3},
        {"name": "", "sheet_id": 3, "filter": {}},
        {"name": "V", "sheet_id": "3", "filter": {}},
        {"name": "V", "sheet_id": 3, "filter": {}, "extra": 1},
        {"filter": {}, "sort": None, "columns": None},
        {"name": "V", "filter": {}},
    ):
        model = replace if "sort" in invalid else create
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


# ---------------------------------------------------------------------------
# response DTO graph
# ---------------------------------------------------------------------------


def test_saved_view_and_lens_rows_freeze_closed_seven_field_graphs() -> None:
    contracts = _contracts()
    view = _model(contracts, "SavedView")
    lens = _model(contracts, "SavedLens")
    view_list = _model(contracts, "SavedViewList")
    lens_list = _model(contracts, "SavedLensList")
    view_delete = _model(contracts, "SavedViewDelete")
    lens_delete = _model(contracts, "SavedLensDelete")

    view_annotations = {
        "id": int,
        "name": str,
        "sheet_id": int,
        "spec": dict[str, JsonValue],
        "op_id": int | None,
        "created_at": str,
        "updated_at": str,
    }
    lens_annotations = {**view_annotations, "sheet_id": int | None}
    delete_annotations = {"ok": bool, "deleted": int}

    for model, expected_annotations, nullable_fields in (
        (view, view_annotations, _VIEW_ROW_NULLABLE_FIELDS),
        (lens, lens_annotations, _LENS_ROW_NULLABLE_FIELDS),
    ):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)
        _assert_required_nullable(model, nullable_fields)
        assert set(model.model_fields) == _ROW_FIELDS
        assert {
            name: model_field.annotation
            for name, model_field in model.model_fields.items()
        } == expected_annotations
    for model in (view_delete, lens_delete):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)
        assert set(model.model_fields) == _DELETE_FIELDS
        assert {
            name: model_field.annotation
            for name, model_field in model.model_fields.items()
        } == delete_annotations

    for root_model, row_model in ((view_list, view), (lens_list, lens)):
        assert issubclass(root_model, RootModel)
        assert root_model.model_config.get("strict") is True
        root_annotation = root_model.model_fields["root"].annotation
        assert root_annotation == list[row_model]
        _assert_closed_annotation(root_annotation, label=f"{root_model.__name__}.root")

    payload = _row_payload(4)
    for model, root_model, nullable_fields in (
        (view, view_list, _VIEW_ROW_NULLABLE_FIELDS),
        (lens, lens_list, _LENS_ROW_NULLABLE_FIELDS),
    ):
        assert model.model_validate(payload).model_dump() == payload
        assert root_model.model_validate([payload]).model_dump() == [payload]
        for name in nullable_fields:
            assert (
                model.model_validate({**payload, name: None}).model_dump()[name] is None
            )
        for name in ("created_at", "updated_at", "name", "spec", "id"):
            with pytest.raises(ValidationError):
                model.model_validate({**payload, name: None})
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "spec": ["not-an-object"]})
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "id": "4"})
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "unapproved_row_field": 1})
        _assert_missing_fields_rejected(model, payload, _ROW_FIELDS)
        # spec stays recursive JSON, not a narrowed object.
        for spec in ({}, {"a": [1, {"b": None}]}, {"c": "s"}, {"d": True}):
            assert model.model_validate({**payload, "spec": spec}).spec == spec

    with pytest.raises(ValidationError):
        view.model_validate({**payload, "sheet_id": None})

    delete_payload = {"ok": True, "deleted": 4}
    for model in (view_delete, lens_delete):
        assert model.model_validate(delete_payload).model_dump() == delete_payload
        with pytest.raises(ValidationError):
            model.model_validate({**delete_payload, "deleted": None})
        with pytest.raises(ValidationError):
            model.model_validate({**delete_payload, "deleted": "4"})
        _assert_missing_fields_rejected(model, delete_payload, _DELETE_FIELDS)


def test_lens_resolve_dto_is_the_literal_query_preview_v1_payload() -> None:
    contracts = _contracts()
    resolved = _model(contracts, "SavedLensResolved")
    evaluator = _model(contracts, "SavedLensResolveEvaluator")
    score = _model(contracts, "SavedLensResolveScore")

    for model in (resolved, evaluator, score):
        assert issubclass(model, WireModel)
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)

    assert set(resolved.model_fields) == _RESOLVE_FIELDS
    assert set(evaluator.model_fields) == _EVALUATOR_FIELDS
    assert set(score.model_fields) == _SCORE_FIELDS
    assert {
        name: model_field.annotation
        for name, model_field in resolved.model_fields.items()
    } == {
        "lens_id": int,
        "schema_version": Literal["frisket.query_preview.v1"],
        "query": dict[str, JsonValue],
        "query_hash": str,
        "sheet_id": int,
        "row_ids": list[int],
        "row_count": int,
        "total": int,
        "offset": int,
        "limit": int,
        "evaluator": evaluator,
        "scores": dict[str, score],
    }
    assert {
        name: model_field.annotation
        for name, model_field in evaluator.model_fields.items()
    } == {"kind": str, "version": str}
    assert {
        name: model_field.annotation for name, model_field in score.model_fields.items()
    } == {"distance": float | None, "score": float | None}
    _assert_required_nullable(score, _SCORE_FIELDS)

    # The frozen literal is the producer's own constant, not a second copy.
    assert QUERY_PREVIEW_SCHEMA_VERSION == "frisket.query_preview.v1"
    assert set(QUERY_PREVIEW_EVALUATOR) == _EVALUATOR_FIELDS

    payload = _resolve_payload()
    wire = resolved.model_validate(payload)
    assert wire.model_dump() == payload
    assert isinstance(wire.evaluator, evaluator)
    assert isinstance(wire.scores["7"], score)
    assert wire.scores["8"].distance is None
    assert wire.scores["8"].score is None

    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "schema_version": "frisket.query.v1"})
    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "sheet_id": None})
    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "query_hash": None})
    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "row_ids": ["7"]})
    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "evaluator": {"kind": "k"}})
    with pytest.raises(ValidationError):
        resolved.model_validate(
            {**payload, "evaluator": {**payload["evaluator"], "extra": "x"}}
        )
    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "scores": {"7": {"distance": 0.25}}})
    with pytest.raises(ValidationError):
        resolved.model_validate({**payload, "unapproved_resolve_field": 1})
    _assert_missing_fields_rejected(resolved, payload, _RESOLVE_FIELDS)
    # scores stay string-keyed, exactly as query_preview_payload emits them.
    assert resolved.model_validate({**payload, "scores": {}}).scores == {}


# ---------------------------------------------------------------------------
# live producers keep their current payloads and semantics
# ---------------------------------------------------------------------------


def test_live_view_and_lens_routes_preserve_their_current_payloads(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "live-view-lens")
    workspace = app.state.workspace
    project_id = str(workspace.create("live views and lenses")["id"])
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("rows")

    with TestClient(app) as client:
        created = client.post(
            f"/api/projects/{project_id}/views",
            json={
                "name": "V",
                "sheet_id": sheet_id,
                "filter": {"q": "x"},
                "sort": [["a", "asc"]],
            },
        )
        assert created.status_code == 200, created.text
        view = created.json()
        assert set(view) == _ROW_FIELDS
        assert view["sheet_id"] == sheet_id
        assert view["spec"] == {"filter": {"q": "x"}, "sort": [["a", "asc"]]}
        assert isinstance(view["op_id"], int)
        assert isinstance(view["created_at"], str)
        assert isinstance(view["updated_at"], str)

        listed = client.get(f"/api/projects/{project_id}/views")
        assert listed.status_code == 200
        assert listed.json() == [view]
        scoped = client.get(f"/api/projects/{project_id}/views?sheet_id={sheet_id}")
        assert scoped.json() == [view]
        assert client.get(f"/api/projects/{project_id}/views?sheet_id=99").json() == []

        patched = client.patch(
            f"/api/projects/{project_id}/views/{view['id']}",
            json={"name": "V2"},
        )
        assert patched.status_code == 200
        assert set(patched.json()) == _ROW_FIELDS
        assert patched.json()["name"] == "V2"
        assert patched.json()["spec"] == {"filter": {"q": "x"}, "sort": [["a", "asc"]]}

        replaced = client.put(
            f"/api/projects/{project_id}/views/{view['id']}/definition",
            json={
                "filter": {},
                "sort": None,
                "columns": ["a"],
                "column_groups": None,
            },
        )
        assert replaced.status_code == 200
        assert replaced.json()["name"] == "V2"
        assert replaced.json()["sheet_id"] == sheet_id
        assert replaced.json()["spec"] == {"filter": {}, "columns": ["a"]}

        deleted = client.delete(f"/api/projects/{project_id}/views/{view['id']}")
        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True, "deleted": view["id"]}

        lens_response = client.post(
            f"/api/projects/{project_id}/lenses",
            json={
                "name": "L",
                "query": {"kind": "filter", "sheet_id": sheet_id, "filter": {}},
                "presentation": {"columns": ["a"]},
            },
        )
        assert lens_response.status_code == 200, lens_response.text
        lens = lens_response.json()
        assert set(lens) == _ROW_FIELDS
        assert lens["sheet_id"] == sheet_id
        assert lens["spec"]["schema_version"] == "frisket.lens.v1"
        assert lens["spec"]["presentation"] == {"columns": ["a"]}
        assert client.get(f"/api/projects/{project_id}/lenses").json() == [lens]

        resolved = client.get(f"/api/projects/{project_id}/lenses/{lens['id']}/resolve")
        assert resolved.status_code == 200
        body = resolved.json()
        assert set(body) == _RESOLVE_FIELDS
        assert body["lens_id"] == lens["id"]
        assert body["schema_version"] == QUERY_PREVIEW_SCHEMA_VERSION
        assert body["query"] == lens["spec"]["query"]
        assert body["sheet_id"] == sheet_id
        assert body["row_ids"] == []
        assert body["row_count"] == 0
        assert body["total"] == 0
        assert body["offset"] == 0
        assert body["limit"] == 50
        assert body["evaluator"] == dict(QUERY_PREVIEW_EVALUATOR)
        assert body["scores"] == {}
        assert isinstance(body["query_hash"], str)

        lens_deleted = client.delete(f"/api/projects/{project_id}/lenses/{lens['id']}")
        assert lens_deleted.status_code == 200
        assert lens_deleted.json() == {"ok": True, "deleted": lens["id"]}


def test_resolve_lens_window_stays_unbounded_and_ignores_unknown_query_keys(
    tmp_path: Path,
) -> None:
    """A live caller passes limit=5000; the preview spine clamps, the route
    does not reject. Introducing a route-level upper bound breaks the app."""

    app = create_app(tmp_path / "unbounded-resolve")
    workspace = app.state.workspace
    project_id = str(workspace.create("unbounded resolve")["id"])
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("rows")

    with TestClient(app) as client:
        lens_id = client.post(
            f"/api/projects/{project_id}/lenses",
            json={
                "name": "L",
                "query": {"kind": "filter", "sheet_id": sheet_id, "filter": {}},
            },
        ).json()["id"]

        wide = client.get(
            f"/api/projects/{project_id}/lenses/{lens_id}/resolve?limit=5000&offset=12"
        )
        assert wide.status_code == 200, wide.text
        assert wide.json()["limit"] == MAX_QUERY_PREVIEW_LIMIT
        assert wide.json()["offset"] == 12

        for path in (
            f"/api/projects/{project_id}/views?unrecognized=1",
            f"/api/projects/{project_id}/views/1?unrecognized=1",
            f"/api/projects/{project_id}/lenses?unrecognized=1",
            f"/api/projects/{project_id}/lenses/{lens_id}?unrecognized=1",
            f"/api/projects/{project_id}/lenses/{lens_id}/resolve?unrecognized=1",
        ):
            assert client.get(path).status_code in (200, 404), path
            assert client.get(path).status_code != 422, path


class _RecordingViewLensService:
    """Records what the routes forward, so a request-model seam can be proved
    against the SERVICE boundary rather than against a response body."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.renamed_to: str | None = None

    def create_view(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("create_view")
        return _row_payload(1)

    def rename_view(
        self,
        project_id: str,
        view_id: int,
        *,
        name: str,
    ) -> dict[str, Any]:
        self.calls.append("rename_view")
        self.renamed_to = name
        return _row_payload(view_id)

    def replace_view_definition(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("replace_view_definition")
        return _row_payload(3)


def _recording_client() -> tuple[TestClient, _RecordingViewLensService]:
    from fastapi import FastAPI

    service = _RecordingViewLensService()
    app = FastAPI()
    register_view_lens_routes(app, service=service)
    return TestClient(app), service


def test_create_view_filter_null_is_refused_before_the_service_runs() -> None:
    """The clean-cutover create request requires an object filter."""

    client, service = _recording_client()
    response = client.post(
        "/api/projects/p/views",
        json={"name": "V", "sheet_id": 3, "filter": None},
    )

    assert response.status_code == 422
    assert response.json() == _FILTER_NULL_422_BODY
    assert service.calls == [], (
        "filter:null must be refused by request validation BEFORE service "
        "dispatch; the service must not observe the call at all"
    )

    # Null and other non-object values are refused by the request boundary,
    # before the service can create an incomplete Saved View.
    for rejected in (None, 5, "text", [1]):
        refused = client.post(
            "/api/projects/p/views",
            json={"name": "V", "sheet_id": 3, "filter": rejected},
        )
        assert refused.status_code == 422, f"filter={rejected!r}"
        detail = refused.json()["detail"]
        assert len(detail) == 1, f"filter={rejected!r} must raise ONE error"
        assert detail[0]["loc"] == ["body", "filter"]
        assert detail[0]["type"] == "dict_type"
        assert detail[0]["msg"] == "Input should be a valid dictionary"
        assert detail[0]["input"] == rejected
    assert service.calls == []

    # A valid dict still reaches the service unchanged.
    accepted = client.post(
        "/api/projects/p/views",
        json={"name": "V", "sheet_id": 3, "filter": {"q": "x"}},
    )
    assert accepted.status_code == 200, accepted.text
    assert service.calls == ["create_view"]


def test_create_view_requires_sheet_id_before_the_service_runs() -> None:
    client, service = _recording_client()

    missing = client.post("/api/projects/p/views", json={"name": "V", "filter": {}})
    assert missing.status_code == 422
    assert [error["loc"] for error in missing.json()["detail"]] == [
        ["body", "sheet_id"]
    ]
    assert service.calls == []


def test_patch_view_forwards_only_the_closed_rename_field() -> None:

    client, service = _recording_client()
    rejected = client.patch("/api/projects/p/views/3", json={"sheet_id": 9})
    assert rejected.status_code == 422
    assert service.calls == []

    response = client.patch("/api/projects/p/views/3", json={"name": "V2"})

    assert response.status_code == 200, response.text
    assert service.calls == ["rename_view"]
    assert service.renamed_to == "V2"


def test_replace_view_definition_requires_a_complete_closed_definition() -> None:
    client, service = _recording_client()

    rejected = client.put(
        "/api/projects/p/views/3/definition",
        json={"filter": {}, "sort": None, "columns": None},
    )
    assert rejected.status_code == 422
    assert service.calls == []

    response = client.put(
        "/api/projects/p/views/3/definition",
        json={
            "filter": {},
            "sort": None,
            "columns": None,
            "column_groups": None,
        },
    )
    assert response.status_code == 200, response.text
    assert service.calls == ["replace_view_definition"]


def test_lens_patch_semantics_and_error_oracles_are_unchanged(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "lens-patch-semantics")
    workspace = app.state.workspace
    project_id = str(workspace.create("lens patch semantics")["id"])
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("rows")

    with TestClient(app) as client:
        lens = client.post(
            f"/api/projects/{project_id}/lenses",
            json={
                "name": "L",
                "query": {"kind": "filter", "sheet_id": sheet_id, "filter": {}},
                "presentation": {"columns": ["a"]},
            },
        ).json()
        lens_id = lens["id"]
        before = dict(project.get_lens(lens_id))
        # The op log is the real no-op oracle: an implementation can append a
        # history row and still leave the lens row's op_id looking untouched.
        ops_before = project.history_total()
        op_ids_before = [row["id"] for row in project.history()]

        invalid = client.patch(
            f"/api/projects/{project_id}/lenses/{lens_id}", json={"query": None}
        )
        assert invalid.status_code == 400
        assert invalid.json() == {
            "detail": {
                "code": "invalid_lens_spec",
                "message": "query spec must be an object",
                "field": "query",
            }
        }

        for body in ({"name": None}, {"presentation": None}):
            noop = client.patch(
                f"/api/projects/{project_id}/lenses/{lens_id}", json=body
            )
            assert noop.status_code == 200, noop.text
            assert noop.json() == lens
        after = dict(project.get_lens(lens_id))
        assert after == before, "a null-only lens patch must not churn the row"
        assert project.history_total() == ops_before, (
            "a null-only lens patch (and a refused query:null patch) must "
            "append NO op row: the persisted history count must not move"
        )
        assert [row["id"] for row in project.history()] == op_ids_before
        assert after["op_id"] == before["op_id"]
        assert after["updated_at"] == before["updated_at"]

        assert client.get(f"/api/projects/{project_id}/views/9999").json() == {
            "detail": "view not found"
        }
        assert client.get(f"/api/projects/{project_id}/lenses/9999").json() == {
            "detail": "lens not found"
        }
        for path, method in (
            (f"/api/projects/{project_id}/views/9999", "patch"),
            (f"/api/projects/{project_id}/views/9999", "delete"),
            (f"/api/projects/{project_id}/lenses/9999", "patch"),
            (f"/api/projects/{project_id}/lenses/9999", "delete"),
            (f"/api/projects/{project_id}/lenses/9999/resolve", "get"),
        ):
            call = getattr(client, method)
            response = (
                call(path, json={"name": "V"}) if method == "patch" else call(path)
            )
            assert response.status_code == 404, path
        for suffix in ("/views", "/lenses"):
            missing = client.get(f"/api/projects/missing{suffix}")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "no project 'missing'"}

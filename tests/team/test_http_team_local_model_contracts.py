"""HTTP-06-F3B: team-local model routes have one typed, local HTTP owner."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from types import ModuleType

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.local_providers import LocalEndpointCatalog
from frisket.contracts.http.models import WireModel
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.operational_routes import OperationalBody


@dataclass(frozen=True)
class RouteTruth:
    method: str
    path: str
    success: int
    response_model: str
    request_model: str | None
    errors: frozenset[int]
    browser_client: bool
    error_model: str = "TeamModelHttpError"
    response_module: str = "frisket.contracts.http.team_local_models"


ROUTES = {
    "list_org_local_endpoints": RouteTruth(
        "GET",
        "/api/org/local-endpoints",
        200,
        "LocalEndpointCatalog",
        None,
        frozenset({401, 403, 500}),
        True,
        response_module="frisket.contracts.http.local_providers",
    ),
    "post_org_models_pull": RouteTruth(
        "POST",
        "/api/org/models/pull",
        202,
        "TeamModelPullStartResponse",
        "TeamArtifactPullRequest",
        frozenset({400, 401, 403, 409, 422, 500, 503}),
        True,
    ),
    "list_org_model_pulls": RouteTruth(
        "GET",
        "/api/org/models/pulls",
        200,
        "TeamModelPullListResponse",
        None,
        frozenset({401, 403, 500}),
        True,
    ),
    "get_org_model_pull": RouteTruth(
        "GET",
        "/api/org/models/pulls/{pull_id}",
        200,
        "TeamModelPull",
        None,
        frozenset({401, 403, 404, 422, 500}),
        True,
    ),
    "cancel_org_model_pull": RouteTruth(
        "POST",
        "/api/org/models/pulls/{pull_id}/cancel",
        202,
        "TeamModelPull",
        None,
        frozenset({401, 403, 404, 409, 422, 500}),
        True,
    ),
}

OPERATION_IDS = frozenset(
    f"outer.{name}.{truth.method.lower()}" for name, truth in ROUTES.items()
)
_CONTRACT_MODULE = "frisket.contracts.http.team_local_models"
_PULL_STATUSES = ("pending", "running", "done", "failed", "cancelled", "uninstalled")
_REMOVED_LOCAL_MODEL_PULL_PATHS = frozenset(
    {
        "/api/org/local-models/pull",
        "/api/org/local-models/pulls",
        "/api/org/local-models/pulls/{pull_id}",
        "/api/org/local-models/pulls/{pull_id}/cancel",
    }
)


def _pull_payload(
    *,
    status: str = "running",
    endpoint_origin: str | None = "http://127.0.0.1:11434",
    initiated_by: str | None = "1",
) -> dict[str, object]:
    return {
        "schemaVersion": "frisket.model_pull.v3",
        "id": 7,
        "model": "ollama/@env-a1b2c3d4e5f6/smollm:135m",
        "status": status,
        "phase": None,
        "total_bytes": None,
        "completed_bytes": None,
        "error": None,
        "resolved_digest": None,
        "resolved_size": None,
        "created_at": "2026-08-10T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "endpoint_id": "env-a1b2c3d4e5f6" if endpoint_origin else None,
        "endpoint_origin": endpoint_origin,
        "initiated_by": initiated_by,
        "artifact": None,
    }


async def _mail(_email: str, _link: str) -> bool:
    return True


@pytest.fixture(scope="module")
def routes_by_name(tmp_path_factory: pytest.TempPathFactory) -> dict[str, APIRoute]:
    root = tmp_path_factory.mktemp("team-local-model-contracts")
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{root / 'control.db'}",
            run_queue_database_url=f"sqlite:///{root / 'queue.db'}",
            data_dir=root / "data",
            base_url="http://testserver",
            organization_name="Team local model contracts",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=_mail,
        product_telemetry_destination=None,
    )
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


def test_removed_local_model_pull_paths_are_not_registered(tmp_path) -> None:
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Removed route probe",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=_mail,
        product_telemetry_destination=None,
    )
    app_paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert _REMOVED_LOCAL_MODEL_PULL_PATHS.isdisjoint(app_paths)


def _model_or_red(candidate: object, *, route: str, role: str) -> type[BaseModel]:
    if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
        pytest.fail(
            f"INTENDED_F3B_RED: {route} must declare a typed {role} model",
            pytrace=False,
        )
    return candidate


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_team_local_model_routes_have_exact_typed_wire_truth(
    name: str, routes_by_name: dict[str, APIRoute]
) -> None:
    assert set(routes_by_name) == set(ROUTES)
    route = routes_by_name[name]
    truth = ROUTES[name]

    assert route.methods == {truth.method}
    assert route.path == truth.path
    assert (route.status_code or 200) == truth.success
    response_model = _model_or_red(route.response_model, route=name, role="response")
    assert response_model.__module__ == truth.response_module, (
        f"INTENDED_F3B_RED: {name} must use team_local_models.{truth.response_model}"
    )
    assert response_model.__name__ == truth.response_model
    assert issubclass(response_model, WireModel)
    assert route.response_model_exclude_unset is (name == "list_org_local_endpoints")
    assert set(route.responses) == set(truth.errors)
    error_model = _model_or_red(
        next(iter(route.responses.values()), {}).get("model"),
        route=name,
        role="error response",
    )
    assert error_model.__module__ == _CONTRACT_MODULE
    assert error_model.__name__ == truth.error_model
    assert all(value == {"model": error_model} for value in route.responses.values())

    body_models = [field.field_info.annotation for field in route.dependant.body_params]
    if truth.request_model is None:
        assert body_models == []
        return
    assert len(body_models) == 1, (
        f"INTENDED_F3B_RED: {name} must declare exactly one request model"
    )
    request_model = _model_or_red(body_models[0], route=name, role="request")
    assert request_model.__module__ == _CONTRACT_MODULE, (
        f"INTENDED_F3B_RED: {name} must use team_local_models.{truth.request_model}"
    )
    assert request_model.__name__ == truth.request_model
    # F3B replaces OperationalBody's JSON ownership, not its same-build
    # coercion / ignored-extra compatibility behavior.
    assert request_model.model_config.get("strict") in (None, False)
    assert request_model.model_config.get("extra") in (None, "ignore")


def test_operational_body_admin_defaults_and_ignored_extra_are_frozen() -> None:

    assert OperationalBody.model_validate({}).model_dump() == {
        "email": "",
        "name": "",
        "role": "",
        "provider": "",
        "key": "",
        "value": "",
        "message": "",
        "source": "browser",
        "validation_token": None,
    }
    assert (
        OperationalBody.model_validate(
            {
                "role": b"member",
                "unrelated": "ignored",
            }
        ).role
        == "member"
    )


def _contract_module() -> ModuleType:
    spec = importlib.util.find_spec(_CONTRACT_MODULE)
    assert spec is not None, (
        "INTENDED_F3B_RED: team_local_models contract module must exist"
    )
    return importlib.import_module(_CONTRACT_MODULE)


def test_team_local_model_declarations_are_complete_local_and_browser_exact() -> None:
    import frisket.team.app as team_app

    declarations = tuple(team_app._TEAM_LOCAL_MODEL_DECLARATIONS)
    actual = {
        (
            entry.id,
            entry.route_owner,
            entry.route_name,
            entry.method,
            entry.auth,
            entry.browser_client,
        )
        for entry in declarations
    }
    assert actual == {
        (
            f"outer.{name}.{truth.method.lower()}",
            "outer",
            name,
            truth.method,
            "session_or_pat",
            truth.browser_client,
        )
        for name, truth in ROUTES.items()
    }
    assert {entry.id for entry in declarations} == OPERATION_IDS
    assert sum(entry.browser_client for entry in declarations) == 5
    assert not {
        entry.id for entry in BASE_ENDPOINT_CATALOG if entry.id in OPERATION_IDS
    }, "F3B declarations must stay edition-local, outside BASE_ENDPOINT_CATALOG"


def test_team_local_model_wire_shapes_keep_busy_detail_and_truthful_cancel() -> None:
    """The public contract keeps the active pull and the post-cancel pull DTO."""

    team_local_models = _contract_module()

    cancelled = {
        **_pull_payload(status="cancelled"),
        "finished_at": "2026-08-10T12:01:00+00:00",
        "cancel_requested": True,
    }
    pull = team_local_models.TeamModelPull.model_validate(cancelled)
    assert pull.model_dump()["status"] == "cancelled"
    assert pull.model_dump()["cancel_requested"] is True
    detail = team_local_models.TeamModelPullBusyDetail.model_validate(
        {
            "code": "pull_busy",
            "message": "busy",
            "active": _pull_payload(endpoint_origin=None, initiated_by=None),
        }
    )
    assert detail.model_dump()["active"]["id"] == 7
    with pytest.raises(ValidationError):
        team_local_models.TeamModelPullBusyDetail.model_validate(
            {"code": "pull_busy", "message": "busy", "active": {"id": "bad"}}
        )


def test_unconfigured_catalog_is_the_canonical_empty_endpoint_collection() -> None:
    assert LocalEndpointCatalog.model_validate(
        {"schemaVersion": "frisket.local_endpoints.v1", "endpoints": []}
    ).model_dump() == {
        "schemaVersion": "frisket.local_endpoints.v1",
        "endpoints": [],
    }


def test_team_local_model_dto_accept_reject_and_endpoint_catalog_matrix() -> None:
    team_local_models = _contract_module()

    catalog = LocalEndpointCatalog
    configured_catalog = {
        "schemaVersion": "frisket.local_endpoints.v1",
        "endpoints": [
            {
                "endpoint_id": "env-a1b2c3d4e5f6",
                "label": "Team local server",
                "kind": "local_http",
                "read_only": True,
                "models": [
                    {
                        "id": "ollama/@env-a1b2c3d4e5f6/smollm:135m",
                        "label": "smollm:135m",
                        "price": {"input": 0.0, "output": 0.0},
                        "local": True,
                    }
                ],
                "reachable": True,
                "origin": "http://127.0.0.1:11434",
                "authority": "organization",
                "source": "environment",
                "detail": None,
                "installed_models": ["smollm:135m"],
                "protocol": "ollama_native",
                "auth_status": "ok",
                "token_configured": False,
                "provisioning_token_configured": False,
                "edge_auth": False,
                "pull_enabled": True,
            }
        ],
    }
    assert catalog.model_validate(configured_catalog).model_dump() == configured_catalog
    with pytest.raises(ValidationError):
        catalog.model_validate({"schemaVersion": "wrong", "endpoints": []})
    with pytest.raises(ValidationError):
        catalog.model_validate({**configured_catalog, "extra": True})
    with pytest.raises(ValidationError):
        catalog.model_validate(
            {
                **configured_catalog,
                "endpoints": [{**configured_catalog["endpoints"][0], "models": [7]}],
            }
        )

    pull_payload = _pull_payload()
    pull = team_local_models.TeamModelPull.model_validate(pull_payload)
    assert pull.model_dump() == pull_payload
    assert {
        team_local_models.TeamModelPull.model_validate(
            _pull_payload(status=status)
        ).status
        for status in _PULL_STATUSES
    } == set(_PULL_STATUSES)
    for invalid in (
        {key: value for key, value in pull_payload.items() if key != "id"},
        {**pull_payload, "id": "7"},
        {**pull_payload, "status": "queued"},
        {**pull_payload, "extra": True},
    ):
        with pytest.raises(ValidationError):
            team_local_models.TeamModelPull.model_validate(invalid)

    assert (
        team_local_models.TeamModelPullStartResponse.model_validate(
            {"pull": pull_payload, "deduplicated": False}
        ).model_dump()["deduplicated"]
        is False
    )
    assert (
        team_local_models.TeamModelPullListResponse.model_validate(
            {"pulls": [pull_payload]}
        ).model_dump()["pulls"][0]["id"]
        == 7
    )
    for model, invalid in (
        (team_local_models.TeamModelPullStartResponse, {"pull": pull_payload}),
        (team_local_models.TeamModelPullListResponse, {"pulls": ["not-a-pull"]}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


def test_team_local_model_request_and_coded_error_matrix() -> None:
    team_local_models = _contract_module()

    request = team_local_models.TeamArtifactPullRequest
    assert request.model_config.get("strict") in (None, False)
    assert request.model_config.get("extra") in (None, "ignore")
    assert request.model_validate(
        {
            "ref": b"hf:owner/model",
            "unpinned_acknowledged": 1,
            "extra": "ignored",
        }
    ).model_dump() == {
        "ref": "hf:owner/model",
        "unpinned_acknowledged": True,
    }
    for invalid in (
        {},
        {"ref": None},
        {"ref": "hf:owner/model", "unpinned_acknowledged": None},
    ):
        with pytest.raises(ValidationError):
            request.model_validate(invalid)

    error = team_local_models.TeamModelHttpError
    assert error.model_validate({"detail": "organization membership required"})
    assert error.model_validate(
        {
            "detail": [
                {
                    "type": "int_parsing",
                    "loc": ["path", "pull_id"],
                    "msg": "Input should be a valid integer",
                    "input": "not-an-id",
                }
            ]
        }
    )
    for detail in (
        {
            "code": "model_pull_disabled",
            "message": "in-app model download is not enabled for this organization",
        },
        {"code": "invalid_model_ref", "message": "bad model reference"},
        {
            "code": "unpinned_unacknowledged",
            "message": "an unpinned artifact needs acknowledgement",
        },
        {
            "code": "local_server_unreachable",
            "message": "no local-model endpoint is configured for this organization",
        },
        {
            "code": "local_server_unreachable",
            "message": "no local server answered at http://127.0.0.1:11434",
            "url": "http://127.0.0.1:11434",
        },
        {
            "code": "local_server_unauthorized",
            "message": "the local server requires authentication",
            "url": "http://127.0.0.1:11434",
        },
        {
            "code": "pull_unsupported",
            "message": "your server lists models but does not accept downloads",
            "url": "http://127.0.0.1:11434",
            "protocol": "openai_compatible",
        },
        {"code": "enqueue_failed", "message": "failed to schedule the model pull"},
        {"code": "pull_not_found", "message": "no pull with id 7 in this workspace"},
        {"code": "pull_not_cancellable", "message": "this pull cannot be cancelled"},
        {"code": "pull_busy", "message": "a pull is already in progress"},
        {
            "code": "pull_busy",
            "message": "a pull is already in progress",
            "active": _pull_payload(),
        },
    ):
        assert error.model_validate({"detail": detail})
    with pytest.raises(ValidationError):
        error.model_validate(
            {
                "detail": {
                    "code": "pull_busy",
                    "message": "busy",
                    "active": {"id": "bad"},
                }
            }
        )
    for detail in (
        {"code": "not_a_live_code", "message": "bad"},
        {
            "code": "local_server_unauthorized",
            "message": "bad url",
            "url": 7,
        },
        {
            "code": "pull_unsupported",
            "message": "bad protocol",
            "url": "http://127.0.0.1:11434",
            "protocol": 7,
        },
    ):
        with pytest.raises(ValidationError):
            error.model_validate({"detail": detail})

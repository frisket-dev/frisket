"""HTTP-06-F2: local-provider routes have one typed HTTP owner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError, WireModel
from frisket.server.app import create_app
from scripts.ci import export_web_openapi as exporter


@dataclass(frozen=True)
class RouteTruth:
    method: str
    path: str
    success: int
    response_model: str
    request_model: str | None
    errors: frozenset[int]


ROUTES = {
    "list_providers": RouteTruth(
        "GET",
        "/api/providers",
        200,
        "LocalProviderCatalog",
        None,
        frozenset({404, 409, 500}),
    ),
    "provider_status": RouteTruth(
        "GET",
        "/api/providers/{provider}/status",
        200,
        "LocalProviderEntry",
        None,
        frozenset({404, 500}),
    ),
    "set_provider_key": RouteTruth(
        "PUT",
        "/api/providers/keys/{provider}",
        200,
        "LocalProviderCatalog",
        "ProviderKeyRequest",
        frozenset({400, 422, 500}),
    ),
    "delete_provider_key": RouteTruth(
        "DELETE",
        "/api/providers/keys/{provider}",
        200,
        "LocalProviderCatalog",
        None,
        frozenset({400, 500}),
    ),
    "create_local_endpoint": RouteTruth(
        "POST",
        "/api/providers/local-endpoints",
        200,
        "LocalProviderCatalog",
        "LocalEndpointCreateRequest",
        frozenset({400, 422, 500}),
    ),
    "update_local_endpoint": RouteTruth(
        "PATCH",
        "/api/providers/local-endpoints/{endpoint_id}",
        200,
        "LocalProviderCatalog",
        "LocalEndpointPatchRequest",
        frozenset({400, 404, 409, 422, 500}),
    ),
    "delete_local_endpoint": RouteTruth(
        "DELETE",
        "/api/providers/local-endpoints/{endpoint_id}",
        200,
        "LocalProviderCatalog",
        None,
        frozenset({400, 404, 409, 500}),
    ),
    "list_model_pulls": RouteTruth(
        "GET",
        "/api/providers/models/pulls",
        200,
        "ModelPullListResponse",
        None,
        frozenset({500}),
    ),
    "get_model_pull": RouteTruth(
        "GET",
        "/api/providers/models/pulls/{pull_id}",
        200,
        "ModelPull",
        None,
        frozenset({404, 422, 500}),
    ),
    "cancel_model_pull": RouteTruth(
        "POST",
        "/api/providers/models/pulls/{pull_id}/cancel",
        202,
        "ModelPull",
        None,
        frozenset({404, 422, 500}),
    ),
    "pull_artifact": RouteTruth(
        "POST",
        "/api/providers/models/pull",
        202,
        "ModelPullStartResponse",
        "ArtifactPullRequest",
        frozenset({400, 403, 409, 422, 500, 503}),
    ),
    "uninstall_artifact": RouteTruth(
        "POST",
        "/api/providers/models/uninstall",
        200,
        "ModelPull",
        "ArtifactUninstallRequest",
        frozenset({400, 404, 409, 422, 500}),
    ),
    "validate_provider_key": RouteTruth(
        "POST",
        "/api/providers/{provider}/validate",
        200,
        "ProviderValidationResponse",
        "ProviderValidationRequest",
        frozenset({400, 422, 500}),
    ),
}

OPERATION_IDS = frozenset(
    f"tenant.{name}.{truth.method.lower()}" for name, truth in ROUTES.items()
)
REMOVED_OLLAMA_PULL_PATHS = frozenset(
    {
        "/api/providers/ollama/pull-setting",
        "/api/providers/ollama/pull",
        "/api/providers/ollama/pulls",
        "/api/providers/ollama/pulls/{pull_id}",
        "/api/providers/ollama/pulls/{pull_id}/cancel",
    }
)
REMOVED_PROVIDER_KEY_PATHS = frozenset({"/api/providers/{provider}/key"})


@pytest.fixture(scope="module")
def routes_by_name(tmp_path_factory: pytest.TempPathFactory) -> dict[str, APIRoute]:
    app = create_app(tmp_path_factory.mktemp("local-provider-contracts"))
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_local_provider_route_declares_exact_wire_truth(
    name: str, routes_by_name: dict[str, APIRoute]
) -> None:
    assert set(routes_by_name) == set(ROUTES)
    route = routes_by_name[name]
    truth = ROUTES[name]

    assert route.methods == {truth.method}
    assert route.path == truth.path
    assert (route.status_code or 200) == truth.success
    if name == "provider_status":
        response_models = route.response_field.field_info.annotation.__args__
        assert {model.__name__ for model in response_models} == {
            "PlatformProvider",
            "LocalHttpEndpointProvider",
        }
        assert all(issubclass(model, WireModel) for model in response_models)
    else:
        assert (
            route.response_model.__module__ == "frisket.contracts.http.local_providers"
        )
        assert route.response_model.__name__ == truth.response_model
        assert issubclass(route.response_model, WireModel)
    assert set(route.responses) == set(truth.errors)
    assert all(value == {"model": HttpError} for value in route.responses.values())

    body_models = [field.field_info.annotation for field in route.dependant.body_params]
    if truth.request_model is None:
        assert body_models == []
    else:
        assert len(body_models) == 1
        assert body_models[0].__module__ == "frisket.contracts.http.local_providers"
        assert body_models[0].__name__ == truth.request_model


def test_removed_ollama_pull_paths_are_not_registered(
    tmp_path: Path,
) -> None:
    app_paths = {
        route.path
        for route in create_app(tmp_path / "removed-route-probe").routes
        if isinstance(route, APIRoute)
    }
    assert REMOVED_OLLAMA_PULL_PATHS.isdisjoint(app_paths)


def test_ambiguous_provider_key_path_is_not_registered(
    tmp_path: Path,
) -> None:
    app_paths = {
        route.path
        for route in create_app(tmp_path / "removed-provider-key-route").routes
        if isinstance(route, APIRoute)
    }
    assert REMOVED_PROVIDER_KEY_PATHS.isdisjoint(app_paths)


def test_new_local_endpoint_bodies_are_strict_and_closed(
    routes_by_name: dict[str, APIRoute],
) -> None:
    for name in ("create_local_endpoint", "update_local_endpoint"):
        body_models = [
            field.field_info.annotation
            for field in routes_by_name[name].dependant.body_params
        ]
        assert len(body_models) == 1
        assert body_models[0].model_config.get("strict") is True
        assert body_models[0].model_config.get("extra") == "forbid"

    patch_model = (
        routes_by_name["update_local_endpoint"]
        .dependant.body_params[0]
        .field_info.annotation
    )
    assert patch_model.model_json_schema()["minProperties"] == 1


def test_sparse_response_models_match_actual_producer_nullability(
    routes_by_name: dict[str, APIRoute],
) -> None:
    """Optional producer keys stay omittable without becoming nullable."""

    catalog_routes = {
        "list_providers",
        "provider_status",
        "set_provider_key",
        "delete_provider_key",
        "create_local_endpoint",
        "update_local_endpoint",
        "delete_local_endpoint",
    }
    assert all(
        routes_by_name[name].response_model_exclude_unset for name in catalog_routes
    )
    assert routes_by_name["validate_provider_key"].response_model_exclude_unset

    catalog_model = routes_by_name["list_providers"].response_model
    validation_model = routes_by_name["validate_provider_key"].response_model
    assert catalog_model is not None
    assert validation_model is not None

    local_endpoint = {
        "endpoint_id": "studio",
        "label": "LM Studio",
        "kind": "local_http",
        "read_only": False,
        "models": [],
        "reachable": False,
        "origin": "http://127.0.0.1:1234",
        "authority": "instance",
        "source": "stored",
        "detail": None,
        "protocol": "unknown",
        "auth_status": "unknown",
        "token_configured": False,
        "provisioning_token_configured": False,
        "edge_auth": False,
        "pull_enabled": False,
    }
    sparse_catalog = catalog_model.model_validate(
        {
            "schemaVersion": "frisket.providers.v1",
            "tier": "local",
            "providers": [local_endpoint],
        }
    )
    sparse_wire = sparse_catalog.model_dump(mode="json", exclude_unset=True)
    assert "network" not in sparse_wire
    assert "installed_models" not in sparse_wire["providers"][0]

    populated_catalog = catalog_model.model_validate(
        {
            "schemaVersion": "frisket.providers.v1",
            "tier": "local",
            "providers": [{**local_endpoint, "installed_models": ["qwen3:8b"]}],
            "network": "off",
        }
    ).model_dump(mode="json", exclude_unset=True)
    assert populated_catalog["network"] == "off"
    assert populated_catalog["providers"][0]["installed_models"] == ["qwen3:8b"]

    with pytest.raises(ValidationError):
        catalog_model.model_validate(
            {
                "schemaVersion": "frisket.providers.v1",
                "tier": "local",
                "providers": [{**local_endpoint, "installed_models": None}],
            }
        )
    with pytest.raises(ValidationError):
        catalog_model.model_validate(
            {
                "schemaVersion": "frisket.providers.v1",
                "tier": "local",
                "providers": [local_endpoint],
                "network": None,
            }
        )

    validation = {
        "provider": "openai",
        "ok": False,
        "reachable": False,
        "status": None,
        "detail": None,
    }
    sparse_validation = validation_model.model_validate(validation).model_dump(
        mode="json", exclude_unset=True
    )
    assert sparse_validation == validation
    assert (
        validation_model.model_validate(
            {**validation, "validation_token": "validation-token"}
        ).validation_token
        == "validation-token"
    )

    for missing in ("provider", "detail"):
        with pytest.raises(ValidationError):
            validation_model.model_validate(
                {key: value for key, value in validation.items() if key != missing}
            )
    with pytest.raises(ValidationError):
        validation_model.model_validate({**validation, "provider": None})
    with pytest.raises(ValidationError):
        validation_model.model_validate({**validation, "validation_token": None})


def test_local_provider_routes_are_browser_members_of_one_policy_owner() -> None:
    members = {
        entry.id: entry
        for entry in BASE_ENDPOINT_CATALOG
        if entry.id in OPERATION_IDS and entry.browser_client
    }
    assert set(members) == set(OPERATION_IDS)
    assert all(entry.route_owner == "tenant" for entry in members.values())
    assert all(entry.auth == "session_or_pat" for entry in members.values())


def test_local_provider_routes_remain_local_in_64_operation_successor_export(
    tmp_path: Path,
) -> None:
    document = exporter.export_real_compositions(tmp_path / "state")
    operations = {
        operation["operationId"]: operation
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }

    assert OPERATION_IDS <= operations.keys()
    assert all(
        operations[operation_id]["x-frisket-editions"] == ["local"]
        for operation_id in OPERATION_IDS
    )

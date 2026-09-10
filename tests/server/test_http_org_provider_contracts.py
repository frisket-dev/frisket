"""HTTP-06-F3A: team organization-provider HTTP contract ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, RootModel, ValidationError
from starlette.routing import Mount

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError, WireModel
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import audit_log
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


@dataclass(frozen=True)
class RouteTruth:
    owner: str
    method: str
    path: str
    response_model: str
    request_model: str | None
    errors: frozenset[int]


ROUTES = {
    "provider_catalog": RouteTruth(
        "tenant",
        "GET",
        "/api/org/provider-catalog",
        "OrganizationProviderCatalog",
        None,
        frozenset({401, 500}),
    ),
    "list_org_keys": RouteTruth(
        "outer",
        "GET",
        "/api/org/keys",
        "OrganizationKeyList",
        None,
        frozenset({401, 403, 500}),
    ),
    "set_org_key": RouteTruth(
        "outer",
        "POST",
        "/api/org/keys",
        "OrganizationKeySave",
        "OrganizationKeySaveRequest",
        frozenset({400, 401, 403, 422, 500}),
    ),
    "validate_org_key": RouteTruth(
        "outer",
        "POST",
        "/api/org/keys/validate",
        "OrganizationKeyValidation",
        "OrganizationKeyValidationRequest",
        frozenset({400, 401, 403, 404, 422, 500, 501}),
    ),
    "delete_org_key": RouteTruth(
        "outer",
        "DELETE",
        "/api/org/keys/{provider}",
        "OrganizationKeyDelete",
        None,
        frozenset({400, 401, 403, 500}),
    ),
}

OPERATION_IDS = frozenset(
    {
        "tenant.provider_catalog.get",
        "outer.list_org_keys.get",
        "outer.set_org_key.post",
        "outer.validate_org_key.post",
        "outer.delete_org_key.delete",
    }
)


async def _mail(_email: str, _link: str) -> bool:
    return True


def _routes(routes: list[Any], owner: str = "outer") -> list[tuple[str, APIRoute]]:
    result: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, Mount):
            result.extend(
                _routes(list(route.routes), "tenant" if owner == "outer" else owner)
            )
        elif isinstance(route, APIRoute):
            result.append((owner, route))
    return result


@pytest.fixture(scope="module")
def routes_by_name(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[str, APIRoute]]:
    root = tmp_path_factory.mktemp("organization-provider-contracts")
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{root / 'control.db'}",
            run_queue_database_url=f"sqlite:///{root / 'queue.db'}",
            data_dir=root / "data",
            base_url="http://testserver",
            organization_name="Organization provider contracts",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=_mail,
        product_telemetry_destination=None,
    )
    return {
        route.name: (owner, route)
        for owner, route in _routes(list(app.routes))
        if route.name in ROUTES
    }


def test_exact_policy_identity_auth_and_browser_membership_are_declared_once() -> None:
    entries = [entry for entry in BASE_ENDPOINT_CATALOG if entry.id in OPERATION_IDS]
    assert {
        (
            entry.id,
            entry.route_owner,
            entry.route_name,
            entry.method,
            entry.auth,
            entry.browser_client,
        )
        for entry in entries
    } == {
        (
            "tenant.provider_catalog.get",
            "tenant",
            "provider_catalog",
            "GET",
            "session_or_pat",
            True,
        ),
        (
            "outer.list_org_keys.get",
            "outer",
            "list_org_keys",
            "GET",
            "session_or_pat",
            True,
        ),
        (
            "outer.set_org_key.post",
            "outer",
            "set_org_key",
            "POST",
            "session_or_pat",
            True,
        ),
        (
            "outer.validate_org_key.post",
            "outer",
            "validate_org_key",
            "POST",
            "session_or_pat",
            True,
        ),
        (
            "outer.delete_org_key.delete",
            "outer",
            "delete_org_key",
            "DELETE",
            "session_or_pat",
            True,
        ),
    }


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_team_organization_provider_routes_have_one_typed_wire_owner(
    name: str, routes_by_name: dict[str, tuple[str, APIRoute]]
) -> None:
    assert set(routes_by_name) == set(ROUTES)
    owner, route = routes_by_name[name]
    truth = ROUTES[name]

    assert owner == truth.owner
    assert route.methods == {truth.method}
    assert route.path == truth.path
    assert (route.status_code or 200) == 200
    assert route.response_model is not None, (
        f"INTENDED_F3A_RED: {name} must declare its response model"
    )
    assert (
        route.response_model.__module__
        == "frisket.contracts.http.organization_providers"
    )
    assert route.response_model.__name__ == truth.response_model
    if name == "list_org_keys":
        assert issubclass(route.response_model, RootModel)
    else:
        assert issubclass(route.response_model, WireModel)
    assert set(route.responses) == set(truth.errors)
    assert all(value == {"model": HttpError} for value in route.responses.values())

    body_models = [field.field_info.annotation for field in route.dependant.body_params]
    if truth.request_model is None:
        assert body_models == []
    else:
        assert len(body_models) == 1
        assert body_models[0] is not None, (
            f"INTENDED_F3A_RED: {name} must declare its request model"
        )
        assert (
            body_models[0].__module__ == "frisket.contracts.http.organization_providers"
        )
        assert body_models[0].__name__ == truth.request_model
        # This same-build family keeps OperationalBody's coercive,
        # ignore-extra request behavior; DTO ownership must not tighten it.
        assert body_models[0].model_config.get("strict") in (None, False)
        assert body_models[0].model_config.get("extra") in (None, "ignore")


def _response_model(
    routes_by_name: dict[str, tuple[str, APIRoute]], name: str, expected: str
) -> type[BaseModel]:
    model = routes_by_name[name][1].response_model
    if model is None:
        pytest.fail(f"INTENDED_F3A_RED: {name} has no response model", pytrace=False)
    if (
        model.__module__ != "frisket.contracts.http.organization_providers"
        or model.__name__ != expected
    ):
        pytest.fail(
            f"INTENDED_F3A_RED: {name} must use organization_providers.{expected}",
            pytrace=False,
        )
    assert issubclass(model, BaseModel)
    return model


def _request_model(
    routes_by_name: dict[str, tuple[str, APIRoute]], name: str, expected: str
) -> type[BaseModel]:
    body_models = [
        field.field_info.annotation
        for field in routes_by_name[name][1].dependant.body_params
    ]
    if len(body_models) != 1 or body_models[0] is None:
        pytest.fail(
            f"INTENDED_F3A_RED: {name} must declare one request model", pytrace=False
        )
    model = body_models[0]
    if (
        model.__module__ != "frisket.contracts.http.organization_providers"
        or model.__name__ != expected
    ):
        pytest.fail(
            f"INTENDED_F3A_RED: {name} must use organization_providers.{expected}",
            pytrace=False,
        )
    assert issubclass(model, BaseModel)
    return model


def test_organization_provider_response_dto_accept_reject_matrix(
    routes_by_name: dict[str, tuple[str, APIRoute]],
) -> None:
    catalog = _response_model(
        routes_by_name, "provider_catalog", "OrganizationProviderCatalog"
    )
    keys = _response_model(routes_by_name, "list_org_keys", "OrganizationKeyList")
    save = _response_model(routes_by_name, "set_org_key", "OrganizationKeySave")
    validation = _response_model(
        routes_by_name, "validate_org_key", "OrganizationKeyValidation"
    )
    delete = _response_model(routes_by_name, "delete_org_key", "OrganizationKeyDelete")

    full_provider = {
        "id": "openai",
        "label": "OpenAI",
        "secret_name": "OPENAI_API_KEY",
        "kind": "llm",
        "policy_fields": ["spend_cap_usd"],
    }
    assert catalog.model_validate(
        {"schemaVersion": "frisket.provider_catalog.v1", "providers": [full_provider]}
    ).model_dump() == {
        "schemaVersion": "frisket.provider_catalog.v1",
        "providers": [full_provider],
    }
    with pytest.raises(ValidationError):
        catalog.model_validate({"schemaVersion": "frisket.provider_catalog.v1"})
    with pytest.raises(ValidationError):
        catalog.model_validate(
            {"schemaVersion": "frisket.provider_catalog.v1", "providers": None}
        )
    for invalid_item in (
        {key: value for key, value in full_provider.items() if key != "id"},
        {**full_provider, "label": None},
        {**full_provider, "kind": 1.5},
        {**full_provider, "extra": True},
    ):
        with pytest.raises(ValidationError):
            catalog.model_validate(
                {
                    "schemaVersion": "frisket.provider_catalog.v1",
                    "providers": [invalid_item],
                }
            )

    assert issubclass(keys, RootModel)
    assert [
        item.model_dump()
        for item in keys.model_validate([{"provider": "openai", "hint": "…abcd"}]).root
    ] == [{"provider": "openai", "hint": "…abcd"}]
    for invalid in (
        [{"provider": "openai"}],
        [{"provider": 1.5, "hint": "…abcd"}],
        [{"provider": None, "hint": "…abcd"}],
        [{"provider": "openai", "hint": "…abcd", "extra": True}],
    ):
        with pytest.raises(ValidationError):
            keys.model_validate(invalid)

    assert save.model_validate(
        {"provider": "openai", "hint": "…abcd"}
    ).model_dump() == {
        "provider": "openai",
        "hint": "…abcd",
    }
    for invalid in (
        {"provider": "openai"},
        {"provider": "openai", "hint": None},
        {"provider": "openai", "hint": "…abcd", "ok": True},
    ):
        with pytest.raises(ValidationError):
            save.model_validate(invalid)

    assert validation.model_validate(
        {
            "provider": "openai",
            "ok": False,
            "reachable": False,
            "status": None,
            "detail": None,
            "validation_token": None,
        }
    ).model_dump() == {
        "provider": "openai",
        "ok": False,
        "reachable": False,
        "status": None,
        "detail": None,
        "validation_token": None,
    }
    validation_payload = {
        "provider": "openai",
        "ok": True,
        "reachable": True,
        "status": 200,
        "detail": None,
        "validation_token": None,
    }
    for field in validation_payload:
        with pytest.raises(ValidationError):
            validation.model_validate(
                {
                    key: value
                    for key, value in validation_payload.items()
                    if key != field
                }
            )
    for invalid in (
        {**validation_payload, "provider": None},
        {**validation_payload, "ok": None},
        {**validation_payload, "reachable": None},
        {**validation_payload, "status": 200.5},
        {**validation_payload, "detail": 1.5},
        {**validation_payload, "validation_token": 1.5},
    ):
        with pytest.raises(ValidationError):
            validation.model_validate(invalid)

    assert delete.model_validate({"deleted": True}).model_dump() == {"deleted": True}
    for invalid in ({}, {"deleted": None}, {"deleted": True, "ok": True}):
        with pytest.raises(ValidationError):
            delete.model_validate(invalid)


def test_organization_key_request_dto_compatibility_matrix(
    routes_by_name: dict[str, tuple[str, APIRoute]],
) -> None:
    save = _request_model(routes_by_name, "set_org_key", "OrganizationKeySaveRequest")
    validation = _request_model(
        routes_by_name, "validate_org_key", "OrganizationKeyValidationRequest"
    )

    assert save.model_config.get("strict") in (None, False)
    assert save.model_config.get("extra") in (None, "ignore")
    assert save.model_validate({}).model_dump() == {
        "provider": "",
        "key": "",
        "validation_token": None,
    }
    assert save.model_validate(
        {"provider": "openai", "key": b"secret", "extra": "ignored"}
    ).model_dump() == {"provider": "openai", "key": "secret", "validation_token": None}
    assert (
        save.model_validate(
            {"provider": "openai", "key": "secret", "validation_token": None}
        ).validation_token
        is None
    )
    assert (
        save.model_validate(
            {"provider": "openai", "key": "secret", "validation_token": b"token"}
        ).validation_token
        == "token"
    )
    for invalid in (
        {"provider": 1.5, "key": "secret"},
        {"provider": None, "key": "secret"},
        {"provider": "openai", "key": None},
        {"provider": "openai", "key": "secret", "validation_token": 1.5},
    ):
        with pytest.raises(ValidationError):
            save.model_validate(invalid)

    assert validation.model_config.get("strict") in (None, False)
    assert validation.model_config.get("extra") in (None, "ignore")
    assert validation.model_validate({}).model_dump() == {"provider": "", "key": ""}
    assert validation.model_validate(
        {"provider": "openai", "key": b"secret", "extra": "ignored"}
    ).model_dump() == {"provider": "openai", "key": "secret"}
    for invalid in (
        {"provider": 1.5},
        {"provider": None},
        {"provider": "openai", "key": None},
    ):
        with pytest.raises(ValidationError):
            validation.model_validate(invalid)


def test_org_key_save_wire_keeps_its_public_hint_shape_not_a_cross_edition_union(
    routes_by_name: dict[str, tuple[str, APIRoute]],
) -> None:
    """The public route's audited hint response is not a generic save result."""

    model = _response_model(routes_by_name, "set_org_key", "OrganizationKeySave")
    assert model.model_validate(
        {"provider": "openai", "hint": "…abcd"}
    ).model_dump() == {
        "provider": "openai",
        "hint": "…abcd",
    }
    with pytest.raises(ValidationError):
        model.model_validate({"ok": True, "provider": "openai"})


def _login(client: TestClient, app: Any, email: str) -> None:
    sign_in_with_magic_link(app, client, email)


def test_real_team_auth_hint_token_save_delete_and_audit_control(
    tmp_path: Path,
) -> None:
    validated: list[tuple[str, str]] = []

    async def validator(provider: str, key: str) -> bool:
        validated.append((provider, key))
        return True

    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Organization provider control",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=_mail,
        validate_provider_key=validator,
        product_telemetry_destination=None,
    )
    owner = claim_server(app)
    member = TestClient(app)
    seed_member_invite(app, "member@example.com")
    _login(owner, app, "owner@example.com")
    _login(member, app, "member@example.com")

    assert TestClient(app).get("/api/org/keys").status_code == 401
    catalog = owner.get("/api/org/provider-catalog")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["providers"], "the real catalog must remain non-empty"
    candidate = "sk-f3a-candidate"
    checked = owner.post(
        "/api/org/keys/validate", json={"provider": "openai", "key": candidate}
    )
    assert checked.status_code == 200, checked.text
    token = checked.json()["validation_token"]
    assert isinstance(token, str) and token
    denied = member.post(
        "/api/org/keys",
        json={"provider": "openai", "key": candidate, "validation_token": token},
    )
    assert denied.status_code == 403, denied.text
    saved = owner.post(
        "/api/org/keys",
        json={"provider": "openai", "key": candidate, "validation_token": token},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"provider": "openai", "hint": "...date"}
    assert member.get("/api/org/keys").json() == [saved.json()]
    deleted = owner.delete("/api/org/keys/openai")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}
    with app.state.control_engine.connect() as cx:
        actions = set(cx.execute(sa.select(audit_log.c.action)).scalars())
    assert {"org_key_validation_requested", "org_key_set", "org_key_deleted"} <= actions
    assert validated == [("openai", candidate)]

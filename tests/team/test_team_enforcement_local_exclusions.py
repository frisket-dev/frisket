"""HTTP-06-F2: team policy excludes only explicit local-only identities."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from frisket.contracts.http.endpoint_catalog import EndpointPolicy
from frisket.team import enforcement


EXPECTED_LOCAL_ONLY_ENDPOINT_IDS = frozenset(
    {
        "tenant.cancel_model_pull.post",
        "tenant.create_local_endpoint.post",
        "tenant.discover_local_endpoints.post",
        "tenant.delete_local_endpoint.delete",
        "tenant.delete_provider_key.delete",
        "tenant.get_model_pull.get",
        "tenant.list_model_pulls.get",
        "tenant.list_providers.get",
        "tenant.pull_artifact.post",
        "tenant.set_provider_key.put",
        "tenant.uninstall_artifact.post",
        "tenant.update_local_endpoint.patch",
        "tenant.validate_provider_key.post",
        "tenant.update_runtime_config.patch",
    }
)
# Test-only: proves enforcement consumes the owner instead of hardcoding the set.
OWNER_CONSUMPTION_SENTINEL_ID = "tenant.owner_consumption_sentinel.get"


def _policy(operation_id: str) -> EndpointPolicy:
    owner, route_name, method = operation_id.split(".")
    return EndpointPolicy(
        id=operation_id,
        route_owner=owner,  # type: ignore[arg-type]
        route_name=route_name,
        method=method.upper(),
        auth="session_or_pat",
    )


def _probe_app(
    *, include_local_provider: bool = False, include_arbitrary: bool = False
) -> FastAPI:
    app = FastAPI()

    @app.get("/api/team-policy-probe", name="team_policy_probe")
    def team_policy_probe() -> dict[str, bool]:
        return {"ok": True}

    if include_local_provider:

        @app.get("/api/providers", name="list_providers")
        def list_providers() -> dict[str, bool]:
            return {"ok": True}

    if include_arbitrary:

        @app.get("/api/arbitrary-probe", name="arbitrary_unregistered_probe")
        def arbitrary_unregistered_probe() -> dict[str, bool]:
            return {"ok": True}

    return app


def _install_catalog(
    monkeypatch: pytest.MonkeyPatch, *, include_arbitrary: bool = False
) -> None:
    entries = [
        _policy("tenant.team_policy_probe.get"),
        *(_policy(operation_id) for operation_id in EXPECTED_LOCAL_ONLY_ENDPOINT_IDS),
        _policy(OWNER_CONSUMPTION_SENTINEL_ID),
    ]
    if include_arbitrary:
        entries.append(_policy("tenant.arbitrary_unregistered_probe.get"))
    monkeypatch.setattr(enforcement, "BASE_ENDPOINT_CATALOG", tuple(entries))
    monkeypatch.setattr(
        enforcement,
        "LOCAL_CONFIGURATION_ENDPOINT_IDS",
        EXPECTED_LOCAL_ONLY_ENDPOINT_IDS | {OWNER_CONSUMPTION_SENTINEL_ID},
        raising=False,
    )


def test_team_composition_omits_all_explicit_local_only_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_catalog(monkeypatch)

    compiled = enforcement._compile(_probe_app(), resolvers={})

    decision = compiled.resolve("tenant", "team_policy_probe", "GET")
    assert decision.id == "tenant.team_policy_probe.get"


def test_registering_an_excluded_provider_route_on_team_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_catalog(monkeypatch)

    with pytest.raises(
        enforcement.TeamEnforcementError,
        match="unclassified /api route",
    ):
        enforcement._compile(
            _probe_app(include_local_provider=True),
            resolvers={},
        )


def test_arbitrary_unregistered_declaration_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_catalog(monkeypatch, include_arbitrary=True)

    with pytest.raises(
        enforcement.TeamEnforcementError,
        match="policy declarations are not registered routes",
    ):
        enforcement._compile(_probe_app(), resolvers={})


def test_arbitrary_undeclared_registered_route_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_catalog(monkeypatch)

    with pytest.raises(
        enforcement.TeamEnforcementError,
        match="unclassified /api route",
    ):
        enforcement._compile(
            _probe_app(include_arbitrary=True),
            resolvers={},
        )

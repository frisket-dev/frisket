"""Team-only, edition-scoped catalog contribution. ``BASE_ENDPOINT_CATALOG``
(``frisket.contracts.http.endpoint_catalog``) is shared across editions
(an external managed composition's own route-policy compiler also consumes
it) -- adding team-only routes there would require that composition to have
registered the identical route or fail closed at ITS startup. This module's
``local_declarations`` parameter is the team-only alternative: a
contribution the compiler accepts alongside the base catalog, entirely
private to ``frisket.team.policy`` and never touched by any other edition.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request

from frisket.contracts.http.endpoint_catalog import (
    BASE_ENDPOINT_CATALOG,
    declare_endpoints,
)
from frisket.team.policy import (
    TeamOuterPolicyError,
    compile_team_policies,
    install_outer_policy,
)


CROSS_OWNER_BASE_POLICY_FACTS = {
    ("create_project", "POST"): (
        "tenant.create_project.post",
        "tenant",
        "session_or_pat",
        True,
        False,
        None,
        (),
        False,
        None,
    ),
    ("list_projects", "GET"): (
        "tenant.list_projects.get",
        "tenant",
        "session_or_pat",
        True,
        False,
        None,
        (),
        False,
        None,
    ),
    ("delete_project", "DELETE"): (
        "tenant.delete_project.delete",
        "tenant",
        "session_or_pat",
        True,
        False,
        "owner",
        (),
        False,
        None,
    ),
    ("update_project_network", "PATCH"): (
        "tenant.update_project_network.patch",
        "tenant",
        "session_or_pat",
        True,
        False,
        "owner",
        (),
        False,
        None,
    ),
    ("update_project_sensitivity", "PATCH"): (
        "tenant.update_project_sensitivity.patch",
        "tenant",
        "session_or_pat",
        False,
        False,
        "owner",
        (),
        False,
        None,
    ),
}


def _team_only_declarations():
    return declare_endpoints(
        "outer",
        "session_or_pat",
        (("team_only_probe", "GET"),),
    )


def test_route_matching_a_local_declaration_compiles_without_a_base_entry() -> None:
    app = FastAPI()

    @app.get("/api/org/team-only-probe", name="team_only_probe")
    def team_only_probe():
        return {}

    policies = compile_team_policies(
        app,
        auth_declarations={},
        local_declarations=_team_only_declarations(),
    )
    assert policies[("GET", "/api/org/team-only-probe")] == "session_or_pat"


def test_same_route_without_local_declarations_still_fails_closed() -> None:
    """The base catalog is unchanged -- the SAME route with no local
    declaration passed in must still refuse to compile, proving the new
    parameter is additive and does not weaken the base fail-closed check."""
    app = FastAPI()

    @app.get("/api/org/team-only-probe", name="team_only_probe")
    def team_only_probe():
        return {}

    with pytest.raises(TeamOuterPolicyError, match="neutral base declaration"):
        compile_team_policies(app, auth_declarations={})


def test_multiple_base_name_method_matches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import frisket.team.policy as team_policy

    app = FastAPI()

    @app.get("/api/probe", name="probe")
    def probe():
        return {}

    ambiguous = (
        *declare_endpoints("outer", "public", (("probe", "GET"),)),
        *declare_endpoints("tenant", "public", (("probe", "GET"),)),
    )
    monkeypatch.setattr(team_policy, "BASE_ENDPOINT_CATALOG", ambiguous)
    with pytest.raises(TeamOuterPolicyError, match="exactly one neutral base"):
        compile_team_policies(app, auth_declarations={})


def test_unlisted_cross_owner_base_reuse_fails_closed() -> None:
    app = FastAPI()

    @app.get("/api/synthetic-sixth", name="actions_schema")
    def synthetic_sixth():
        return {}

    with pytest.raises(TeamOuterPolicyError, match="cross-owner base reuse"):
        compile_team_policies(app, auth_declarations={})


def test_stale_local_declaration_for_an_unregistered_route_fails_closed() -> None:
    app = FastAPI()

    with pytest.raises(TeamOuterPolicyError, match="stale"):
        compile_team_policies(
            app,
            auth_declarations={},
            local_declarations=_team_only_declarations(),
        )


def test_live_route_path_moves_without_a_policy_edit() -> None:
    declarations = _team_only_declarations()
    for path in ("/api/org/team-only-probe", "/api/org/moved-team-only-probe"):
        app = FastAPI()

        @app.get(path, name="team_only_probe")
        def team_only_probe():
            return {}

        policies = compile_team_policies(
            app,
            auth_declarations={},
            local_declarations=declarations,
        )
        assert policies[("GET", path)] == "session_or_pat"


def test_duplicate_registered_identity_and_wire_fail_closed() -> None:
    duplicate_identity = FastAPI()

    @duplicate_identity.get("/api/org/one", name="team_only_probe")
    def first_identity():
        return {}

    @duplicate_identity.get("/api/org/two", name="team_only_probe")
    def second_identity():
        return {}

    with pytest.raises(TeamOuterPolicyError, match="duplicate registered identity"):
        compile_team_policies(
            duplicate_identity,
            auth_declarations={},
            local_declarations=_team_only_declarations(),
        )

    duplicate_wire = FastAPI()

    @duplicate_wire.get("/api/org/same", name="first_probe")
    def first_wire():
        return {}

    @duplicate_wire.get("/api/org/same", name="second_probe")
    def second_wire():
        return {}

    declarations = declare_endpoints(
        "outer",
        "session_or_pat",
        (("first_probe", "GET"), ("second_probe", "GET")),
    )
    with pytest.raises(TeamOuterPolicyError, match="ambiguous wire template"):
        compile_team_policies(
            duplicate_wire,
            auth_declarations={},
            local_declarations=declarations,
        )


def test_real_team_cross_owner_base_reuse_is_closed_and_stable(tmp_path) -> None:
    import frisket.team.app as team_app
    import frisket.team.policy as team_policy

    async def send_magic_email(_email: str, _link: str) -> bool:
        return True

    app = team_app.create_team_app(
        team_app.TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Policy Test",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=send_magic_email,
        product_telemetry_destination=None,
    )
    local_declarations = (
        *team_app._TEAM_LOCAL_MODEL_DECLARATIONS,
        *team_app._TEAM_BROWSER_AUTH_DECLARATIONS,
        *team_app._TEAM_READINESS_DECLARATIONS,
        *team_app._TEAM_OPERATOR_ADMIN_DECLARATIONS,
    )
    local_identities = {
        (entry.route_owner, entry.route_name, entry.method)
        for entry in local_declarations
    }
    cross_owner_facts = {}
    for method, path, name in team_policy._routes(app):
        if path.startswith("/auth") or ("outer", name, method) in local_identities:
            continue
        matches = [
            entry
            for entry in BASE_ENDPOINT_CATALOG
            if entry.route_name == name and entry.method == method
        ]
        assert len(matches) == 1, (method, path, name, matches)
        entry = matches[0]
        if entry.route_owner == "outer":
            continue
        cross_owner_facts[(name, method)] = (
            entry.id,
            entry.route_owner,
            entry.auth,
            entry.browser_client,
            entry.forwards_to_tenant,
            entry.project_role,
            entry.resolvers,
            entry.reserves_funding,
            entry.pat_forbidden_detail,
        )
    assert cross_owner_facts == CROSS_OWNER_BASE_POLICY_FACTS


def test_install_outer_policy_enforces_a_local_declaration_route(monkeypatch) -> None:
    app = FastAPI()

    @app.get("/api/org/team-only-probe", name="team_only_probe")
    def team_only_probe(request: Request):
        return {"ok": True}

    calls: list[bool] = []

    def require_user(request: Request, *, browser=False, admin=False):
        calls.append(True)
        return {"id": 1, "email": "owner@example.com"}

    install_outer_policy(
        app,
        auth_declarations={},
        require_user=require_user,
        local_declarations=_team_only_declarations(),
    )

    from fastapi.testclient import TestClient

    client = TestClient(app)
    response = client.get("/api/org/team-only-probe")
    assert response.status_code == 200
    assert calls, "session_or_pat policy from the local declaration must enforce auth"

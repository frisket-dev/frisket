"""Egress-gate correctness and client-input bypass resistance.

Load-bearing property: a client-supplied ``capabilities`` field cannot grant
permission to perform network effects. The gate reads only two
server-derived facts: the static catalog ``required_capabilities`` for the
always-remote kinds, and the code-owned engine tables for engine-dependent
kinds. This file proves reject/allow tracks ONLY project policy + resolved
engine, and that crafting the client ``capabilities`` field (omitting or
including ``external:*``) never changes the gate's outcome.

A client that can send action-run request bodies (a team-edition member's
browser or a script with a session token) controls every request field,
including ``capabilities`` and the engine
string; it does not control server code, catalog tables, or the project's
stored policy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner, NetworkDisabled
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _project_id(client: TestClient, name: str = "Egress Gate") -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _set_network(client: TestClient, project_id: str, mode: str) -> None:
    response = client.patch(f"/api/projects/{project_id}/network", json={"mode": mode})
    assert response.status_code == 200, response.text


def _geocode_action(capabilities: list[str] | None, key: str) -> dict[str, Any]:
    return {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        **({"capabilities": capabilities} if capabilities is not None else {}),
        "params": {"source": "address", "engine": "nominatim"},
        "idempotency_key": f"enrich_geocode@sha256:bypass-{key}",
    }


def _run(client: TestClient, project_id: str, body: dict[str, Any]):
    return client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)


def _error_codes(response) -> list[str]:
    return [e["code"] for e in response.json().get("errors", [])]


def _seed(client: TestClient, project_id: str) -> None:
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("data")
    cols = {"address": project.add_column(sheet, "address")}
    project.add_rows(sheet, [{"address": "1600 Pennsylvania Ave"}], cols)


def test_policy_off_rejects_always_remote_kind_without_client_capabilities(
    tmp_path,
) -> None:
    """The typed action's server declaration gates before any run/job exists."""
    client = _client(tmp_path)
    pid = _project_id(client)
    _seed(client, pid)
    _set_network(client, pid, "off")

    response = _run(client, pid, _geocode_action(None, "declared"))
    assert response.json()["status"] == "failed"
    assert _error_codes(response) == ["network_disabled"]
    project = client.app.state.workspace.get(pid)
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_omitting_external_capability_does_not_open_the_gate(tmp_path) -> None:
    """THE bypass attempt: the client strips ``external:geocode`` from its
    declared capabilities, hoping the gate keys off the client field. The
    gate must still fire (network_disabled, not missing_capability) because
    it reads the CATALOG's required_capabilities, never the request's."""
    client = _client(tmp_path)
    pid = _project_id(client)
    _seed(client, pid)
    _set_network(client, pid, "off")

    response = _run(client, pid, _geocode_action(["project:write"], "stripped"))
    assert response.json()["status"] == "failed"
    assert _error_codes(response) == ["network_disabled"]


def test_policy_on_never_produces_network_disabled(tmp_path) -> None:
    """Under ``on`` (and the un-configured default), the same request —
    including one that dishonestly inflates its capabilities — never sees the
    network gate. Whatever else happens downstream (precheck failures, live
    dispatch) is out of this gate's scope."""
    client = _client(tmp_path)
    pid = _project_id(client)
    _seed(client, pid)

    # Un-configured default is "on".
    for caps, key in (
        (None, "on-canonical"),
        (["project:write", "external:geocode"], "on-declared"),
        (["project:write"], "on-stripped"),
    ):
        response = _run(client, pid, _geocode_action(caps, key))
        assert "network_disabled" not in _error_codes(response), response.text

    _set_network(client, pid, "on")
    response = _run(client, pid, _geocode_action(None, "on-expl"))
    assert "network_disabled" not in _error_codes(response), response.text


# --- engine-dependent class: the runner gate reads the engine TABLE ---------


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    sheet = p.add_sheet("data")
    cols = {"statement": p.add_column(sheet, "statement")}
    p.add_rows(sheet, [{"statement": "Hello"}], cols)
    p._egress_test_sheet = sheet  # stash for specs
    yield p
    p.close()


def _translate_plan(project: Project, engine: str):
    return build_typed_map_rows_plan(
        project,
        typed_action_for_request(
            {
                "action_id": "map.translate",
                "scope": {"kind": "sheet_rows", "sheet_id": project._egress_test_sheet},
                "params": {
                    "source": ["statement"],
                    "engine": engine,
                    "target_language": "Spanish",
                    **({"language": ["en"]} if engine == "opus_mt" else {}),
                },
                "output_names": {"translation": "es"},
                "idempotency_key": "translate-network-bypass",
            }
        ),
    )


def test_validate_spec_rejects_remote_engine_from_server_table(project) -> None:
    """engine="deepl" resolves remote via the server-side table; the spec
    carries no capabilities field at all, and nothing the client could add to
    the spec changes the classification."""
    project.set_network_policy(mode="off")
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )
    plan = _translate_plan(project, "deepl")
    spec = plan.spec_dict()
    # A client-side attempt to look "local": junk keys must not matter.
    spec["capabilities"] = ["project:write"]
    with pytest.raises(NetworkDisabled):
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            spec,
            program=plan.program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )


def test_validate_spec_allows_local_engine_under_off(project) -> None:
    """opus_mt is local-tier: policy off must NOT block it — and a client
    declaring external capabilities cannot make a local engine gate."""
    project.set_network_policy(mode="off")
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )
    plan = _translate_plan(project, "opus_mt")
    spec = plan.spec_dict()
    spec["capabilities"] = ["external:web_search"]  # noise, must be ignored
    try:
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            spec,
            program=plan.program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
    except NetworkDisabled:  # pragma: no cover - the failure this test guards
        pytest.fail("local engine must not be network-gated")
    except Exception:
        # Other validation outcomes (e.g. runtime not installed) are fine;
        # only NetworkDisabled would be the gate misfiring.
        pass


def test_execute_row_reasserts_policy_for_direct_callers(project) -> None:
    """``row_execution.execute_row`` is a public entry point: reachable
    (map_runner.py, preview.py) without _validate_spec having run on this
    spec. Its own re-assert is the fence that stops a remote engine egressing
    on such a path — call it directly with the policy ``off`` and it must
    raise. Deleting this test is how the bypass class comes back."""
    project.set_network_policy(mode="off")
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )
    plan = _translate_plan(project, "deepl")
    spec = plan.spec_dict()
    from frisket.ops.base import OpContext
    from frisket.engine.runner.row_execution import execute_row

    ctx = OpContext(project=project, http=None, extras={})
    with pytest.raises(NetworkDisabled):
        asyncio.run(
            execute_row(
                runner.project,
                runner.router,
                runner._throttle,
                plan.program,
                {"statement": "Hello"},
                spec,
                ctx,
                row_id=1,
            )
        )

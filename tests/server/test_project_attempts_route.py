"""The project-keyed attempt-receipt reader.

``execution_attempts.run_id`` is nullable with ``ON DELETE SET NULL`` so
the record that a user consented and was charged outlives the run's data. But
every reader was ``WHERE run_id=?``, so what the ruling preserved was a record
nothing could open: after ``compact()`` the orphan joins no run, and the
run-keyed route correctly never returns it.

These tests pin the fix at the wire: the orphan is unreachable through the
run-keyed route (still true, and correct) and reachable through the
project-keyed one, which is also where the run detail surface asks its own
question so the two cannot give different answers about one attempt.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.execution_composition_helpers import open_attempt_authority
from frisket.contracts.http.project_attempts import ProjectAttemptsPage
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.server.app import create_app
from frisket.server.services.project_attempts import ProjectAttemptsService


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _seed_attempt(
    client: TestClient, pid: str, sheet_name: str = "S"
) -> tuple[int, str]:
    """One LOCAL (unrouted) run with a real minted attempt.

    Unrouted work is the free case by construction: no cost promise means
    ``OperatorBorneZeroCost``, so this is also the fixture for "settlement
    produced an absence, not a charge".
    """
    project = client.app.state.workspace.get(pid)
    return _seed_attempt_on_project(project, sheet_name=sheet_name)


def _seed_attempt_on_project(project, *, sheet_name: str = "S") -> tuple[int, str]:
    sheet_id = project.add_sheet(sheet_name)
    column_id = project.add_column(sheet_id, "story")
    row_ids = project.add_rows(sheet_id, [{"story": "Ada"}], {"story": column_id})
    plan = build_typed_map_rows_plan(
        project,
        typed_action_for_request(
            {
                "action_id": "map.classify",
                "idempotency_key": f"attempt-fixture-{sheet_id}",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["story"],
                    "engine": "local_semantic",
                    "fields": [
                        {
                            "name": "topic",
                            "type": "category",
                            "labels": ["person", "other"],
                        }
                    ],
                },
            }
        ),
    )
    op_id = project.db.execute(
        "INSERT INTO ops (kind, spec) VALUES (?,?)", ("classify", "{}")
    ).lastrowid
    run_id = int(
        project.db.execute(
            "INSERT INTO runs (op_id, sheet_id, action_kind, params) VALUES (?,?,?,?)",
            (op_id, sheet_id, "map.classify", "{}"),
        ).lastrowid
    )
    project.db.commit()
    commitment = open_attempt_authority(project).mint(
        recipe=plan.program, spec=plan.spec_dict(), run_id=run_id, scope=tuple(row_ids)
    )
    return run_id, commitment.attempt_id


def _new_project(client: TestClient) -> str:
    return str(client.post("/api/projects", json={"name": "Receipts"}).json()["id"])


def test_a_compacted_run_leaves_a_receipt_only_the_project_reader_can_open(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid = _new_project(client)
    run_id, attempt_id = _seed_attempt(client, pid)

    # Reachable both ways while the run is live.
    run_scoped = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/attempts")
    assert run_scoped.status_code == 200, run_scoped.text
    assert [a["attempt_id"] for a in run_scoped.json()["attempts"]] == [attempt_id]

    # Compaction: the run's data is reclaimed, the authorization is not.
    project = client.app.state.workspace.get(pid)
    project.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
    project.db.commit()
    assert (
        project.db.execute(
            "SELECT run_id FROM execution_attempts WHERE id=?", (attempt_id,)
        ).fetchone()["run_id"]
        is None
    )

    # The run-keyed reader now returns nothing — correct, and exactly the
    # reason a second reader had to exist.
    orphaned_run_scoped = client.get(
        f"/api/projects/{pid}/actions/runs/{run_id}/attempts"
    )
    assert orphaned_run_scoped.status_code == 200
    assert orphaned_run_scoped.json()["attempts"] == []

    # The project-keyed reader opens it, with the honest null run id.
    page = client.get(f"/api/projects/{pid}/actions/attempts")
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == 1
    (receipt,) = body["attempts"]
    assert receipt["attempt_id"] == attempt_id
    assert receipt["run_id"] is None
    assert receipt["consent"] is None
    # The charge is not guessed at from what survived the cascade.
    assert receipt["settlement"]["unsettleable"] == "metering_reclaimed"
    assert receipt["settlement"]["charge_usd"] is None


def test_the_run_filter_answers_with_the_same_receipt_as_the_run_route(
    tmp_path: Path,
) -> None:
    """One attempt, two surfaces, one answer: the run detail panel's
    ``?run_id=`` query and the run-keyed route render the same receipt."""
    client = _client(tmp_path)
    pid = _new_project(client)
    run_id, attempt_id = _seed_attempt(client, pid)
    other_run_id, other_attempt_id = _seed_attempt(client, pid, sheet_name="S2")

    filtered = client.get(f"/api/projects/{pid}/actions/attempts?run_id={run_id}")
    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == ProjectAttemptsService(client.app.state.workspace).page(
        pid,
        run_id=run_id,
        offset=0,
        limit=25,
    )
    assert filtered.json()["run_id"] == run_id
    assert filtered.json()["total"] == 1
    assert (
        filtered.json()["attempts"]
        == (
            client.get(f"/api/projects/{pid}/actions/runs/{run_id}/attempts").json()[
                "attempts"
            ]
        )
    )

    unfiltered = client.get(f"/api/projects/{pid}/actions/attempts")
    assert unfiltered.json()["total"] == 2
    assert {a["attempt_id"] for a in unfiltered.json()["attempts"]} == {
        attempt_id,
        other_attempt_id,
    }
    assert unfiltered.json()["run_id"] is None
    assert other_run_id != run_id


def test_a_free_local_run_settles_to_an_absence_not_a_zero_charge(
    tmp_path: Path,
) -> None:
    """What the receipts panel keys its "no charges" state off. An unrouted
    local run has an operator-borne-zero basis, so settlement names no SKU and
    the panel must render no money row — a fabricated $0.00 line is the very
    thing ``settle`` refuses to produce."""
    client = _client(tmp_path)
    pid = _new_project(client)
    run_id, _ = _seed_attempt(client, pid)

    (receipt,) = client.get(
        f"/api/projects/{pid}/actions/attempts?run_id={run_id}"
    ).json()["attempts"]
    assert receipt["cost_basis"] == {"kind": "operator_borne_zero"}
    settlement = receipt["settlement"]
    assert settlement["pricing_key"] is None
    assert settlement["charge_usd"] == "0"
    assert "unit_rate" not in settlement
    assert settlement["price_card_version"] == receipt["price_card_version"]


def _record_model_call(
    project: object,
    *,
    run_id: int,
    attempt_id: str,
    provider: str,
    credential_source: str,
    cost_source: str,
) -> None:
    import uuid

    project.db.execute(  # type: ignore[attr-defined]
        "INSERT INTO model_calls "
        "(id, fact_version, run_id, capability, engine, provider, provider_kind, "
        " credential_source, cost_source, attempt_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            "v1",
            run_id,
            "chat",
            "gpt-5-mini",
            provider,
            "platform_api",
            credential_source,
            cost_source,
            attempt_id,
        ),
    )
    project.db.commit()  # type: ignore[attr-defined]


def test_a_run_on_the_users_own_provider_key_is_not_reported_as_free(
    tmp_path: Path,
) -> None:
    """The BYOK receipt. ``operator_borne_zero`` says the PLATFORM charged
    nothing; it says nothing about whether the user's own key was billed, and
    the Charges panel used to read it as "this ran at no cost to you". The
    distinguishing fact lives on the calls the attempt authorized, so the
    receipt carries it rather than leaving the client to guess."""
    client = _client(tmp_path)
    pid = _new_project(client)
    run_id, attempt_id = _seed_attempt(client, pid)
    project = client.app.state.workspace.get(pid)
    _record_model_call(
        project,
        run_id=run_id,
        attempt_id=attempt_id,
        provider="openai",
        credential_source="project_key",
        cost_source="pricing_data",
    )

    (receipt,) = client.get(
        f"/api/projects/{pid}/actions/attempts?run_id={run_id}"
    ).json()["attempts"]
    assert receipt["cost_basis"] == {"kind": "operator_borne_zero"}
    assert receipt["borne_by"] == {"credentialed_providers": ["openai"]}


def test_a_local_model_call_is_not_mistaken_for_a_users_provider_key(
    tmp_path: Path,
) -> None:
    """``ModelCallMeta.local`` pins ``credential_source='local'`` — the same
    token an env-held provider key uses — so credential provenance alone would
    report in-process work as a billed key. ``cost_source`` is what separates
    them, and this is the test that goes red if that condition is dropped."""
    client = _client(tmp_path)
    pid = _new_project(client)
    run_id, attempt_id = _seed_attempt(client, pid)
    project = client.app.state.workspace.get(pid)
    _record_model_call(
        project,
        run_id=run_id,
        attempt_id=attempt_id,
        provider="local",
        credential_source="local",
        cost_source="free_local",
    )

    (receipt,) = client.get(
        f"/api/projects/{pid}/actions/attempts?run_id={run_id}"
    ).json()["attempts"]
    assert receipt["borne_by"] == {"credentialed_providers": []}


def test_an_attempt_with_no_recorded_calls_says_nothing_about_who_paid(
    tmp_path: Path,
) -> None:
    """Absence of metering is not evidence of local execution. ``None`` here
    keeps the panel from upgrading "we have no record" into "it was free"."""
    client = _client(tmp_path)
    pid = _new_project(client)
    run_id, _ = _seed_attempt(client, pid)

    (receipt,) = client.get(
        f"/api/projects/{pid}/actions/attempts?run_id={run_id}"
    ).json()["attempts"]
    assert receipt["borne_by"] is None


def test_project_attempts_route_registered_after_the_run_keyed_reader(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "ws")
    routes = [
        (sorted(route.methods or []), route.path)
        for route in app.router.routes
        if isinstance(route, APIRoute)
    ]
    previous_route = (
        ["GET"],
        "/api/projects/{pid}/actions/runs/{run_id}/attempts",
    )
    index = routes.index(previous_route)
    assert routes[index + 1] == (["GET"], "/api/projects/{pid}/actions/attempts")


def test_paging_bounds_are_enforced_by_the_shared_page_limit(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid = _new_project(client)
    _seed_attempt(client, pid)
    assert (
        client.get(f"/api/projects/{pid}/actions/attempts?limit=0").status_code == 422
    )
    assert (
        client.get(f"/api/projects/{pid}/actions/attempts?limit=101").status_code == 422
    )
    page = client.get(f"/api/projects/{pid}/actions/attempts?limit=1&offset=1").json()
    assert page["attempts"] == [] and page["total"] == 1 and page["has_more"] is False


def test_project_attempts_route_exports_its_explicit_receipt_page_contract(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "ws")
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/projects/{pid}/actions/attempts"
        and "GET" in (route.methods or set())
    )
    assert route.response_model is ProjectAttemptsPage

    operation = app.openapi()["paths"]["/api/projects/{pid}/actions/attempts"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProjectAttemptsPage"
    }
    assert set(operation["responses"]) == {"200", "401", "403", "404", "422", "500"}

    schemas = app.openapi()["components"]["schemas"]
    receipt = schemas["ProjectAttemptReceipt"]["properties"]
    assert receipt["target"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/ProjectAttemptTarget"},
            {"type": "null"},
        ]
    }
    assert receipt["consent"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/ProjectAttemptConsent"},
            {"type": "null"},
        ]
    }
    assert receipt["settlement"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/ProjectAttemptSettlement"},
            {"type": "null"},
        ]
    }

"""Action API-key and credential gate.

An action whose provider requires a key must be blocked before execution when
that key is absent, and the refusal must link the user to provider settings.

The merged public catalog projects required_credentials from the typed
Census capability. The public typed request must refuse before reservation
when the key is absent, with HTTP 400 rather than a cost-consent challenge.
Both environment and project-secret resolution remain covered here.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.actions.registry import ACTION_REGISTRY
from frisket.credentials import missing_required_credentials, resolve_credential
from frisket.ai.llm import ModelRouter
from frisket.team.security.secrets import encrypt_secret
from frisket.server.action_enqueue import MISSING_ACTION_CREDENTIAL_ERROR_CODE
from frisket.server.app import create_app
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.engine.store import Project


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _census_action(
    *,
    sheet_id: int = 1,
    idempotency_key: str = "census_demographics@sha256:key-gate",
) -> dict[str, Any]:
    return {
        "action_id": "enrich.census_demographics",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "point",
            "geography": "tract",
            "include_moe": False,
        },
        "idempotency_key": idempotency_key,
    }


def test_census_demographics_declares_required_credential(tmp_path) -> None:
    definition = ACTION_REGISTRY.get("enrich.census_demographics")
    assert definition.catalog_entry()["required_credentials"] == ["CENSUS_API_KEY"]
    # Exercise the actual served merged catalog, not the legacy-only registry.
    with _client(tmp_path) as client:
        response = client.get("/api/actions/v1/catalog")
        assert response.status_code == 200, response.text
        payload = response.json()
    entry = next(
        item
        for item in payload["actions"]
        if item["kind"] == "enrich.census_demographics"
    )
    assert entry["required_credentials"] == ["CENSUS_API_KEY"]
    error_codes = {error["code"] for error in entry["errors"]}
    assert MISSING_ACTION_CREDENTIAL_ERROR_CODE in error_codes


def test_only_census_demographics_declares_a_required_credential(tmp_path) -> None:
    # The mechanism is generic; verify it stays opt-in (empty default) for
    # every other action rather than silently gating unrelated actions.
    with _client(tmp_path) as client:
        response = client.get("/api/actions/v1/catalog")
        assert response.status_code == 200, response.text
        gated = sorted(
            entry["kind"]
            for entry in response.json()["actions"]
            if entry["required_credentials"]
        )
    assert gated == ["enrich.census_demographics"]


def test_resolve_credential_prefers_env_var(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "env.frisket")
    try:
        monkeypatch.setenv("CENSUS_API_KEY", "env-value")
        assert resolve_credential(project, "CENSUS_API_KEY") == "env-value"
        assert missing_required_credentials(project, ["CENSUS_API_KEY"]) == []
    finally:
        project.close()


def test_resolve_credential_falls_back_to_project_secret(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "secret.frisket")
    try:
        monkeypatch.delenv("CENSUS_API_KEY", raising=False)
        assert missing_required_credentials(project, ["CENSUS_API_KEY"]) == [
            "CENSUS_API_KEY"
        ]
        project.set_secret(
            name="CENSUS_API_KEY",
            encrypted=encrypt_secret("secret-value"),
            hint="cens***",
        )
        assert resolve_credential(project, "CENSUS_API_KEY") == "secret-value"
        assert missing_required_credentials(project, ["CENSUS_API_KEY"]) == []
    finally:
        project.close()


def test_queued_run_refuses_without_credential_distinct_from_cost_gate(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Census Gate"}).json()["id"]

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_census_action(),
    )
    assert response.status_code == 400, response.text
    assert response.status_code != 402  # distinct from the cost-gate envelope
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.status != "needs_confirmation"
    assert result.action.kind == "enrich.census_demographics"
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.code == MISSING_ACTION_CREDENTIAL_ERROR_CODE
    assert error.details["missing_credentials"] == ["CENSUS_API_KEY"]
    assert error.details["settings_scope"] == "project"
    assert error.details["settings_section"] == "secrets"
    assert "CENSUS_API_KEY" in error.message
    # No run/receipt reservation should have been made — the gate fires
    # before any idempotency-key reservation, not just before execution.
    project = client.app.state.workspace.get(project_id)
    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_queued_run_succeeds_past_gate_once_project_secret_configured(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Census Gate Ok"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    point = project.add_column(sheet_id, "point", type="geo_point")
    project.add_rows(
        sheet_id, [{"point": {"lat": 38.9, "lon": -77.03}}], {"point": point}
    )
    catalog_url = f"/api/projects/{project_id}/actions/v1/catalog"
    missing_entry = next(
        entry
        for entry in client.get(catalog_url).json()["actions"]
        if entry["kind"] == "enrich.census_demographics"
    )
    assert missing_entry["ui_hints"]["missing_credentials"] == ["CENSUS_API_KEY"]
    project.set_secret(
        name="CENSUS_API_KEY",
        encrypted=encrypt_secret("test-project-key"),
        hint="test***",
    )
    configured_entry = next(
        entry
        for entry in client.get(catalog_url).json()["actions"]
        if entry["kind"] == "enrich.census_demographics"
    )
    assert not configured_entry["ui_hints"].get("missing_credentials")

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_census_action(sheet_id=sheet_id),
    )
    # Census is a free public API. With a valid source and project credential,
    # it is admitted normally without a needless confirmation challenge.
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.receipt_id is not None
    assert all(
        error.code != MISSING_ACTION_CREDENTIAL_ERROR_CODE for error in result.errors
    )
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_missing_action_credential_status_maps_off_cost_gate_402() -> None:
    result = ActionResult(
        action={"kind": "enrich.census_demographics", "action_id": "act_1"},
        status="failed",
        project_id="p1",
        errors=[
            {
                "schema_version": "frisket.action_error.v1",
                "code": MISSING_ACTION_CREDENTIAL_ERROR_CODE,
                "message": "needs a key",
                "action_kind": "enrich.census_demographics",
            }
        ],
    )
    assert v1_action_result_http_status(result) == 400

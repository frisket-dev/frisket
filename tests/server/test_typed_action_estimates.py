from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.actions.core import RegisteredAction
from frisket.ai.llm import ModelRouter
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.server.app import create_app


@pytest.fixture
def project_client(tmp_path):
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )
    project_id = client.post("/api/projects", json={"name": "Typed estimate"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    source_id = project.add_column(sheet_id, "source", type="text")
    project.add_rows(
        sheet_id,
        [{"source": "2026-09-04"}, {"source": "2026-09-05"}],
        {"source": source_id},
    )
    return client, project_id, sheet_id


@pytest.mark.parametrize(
    ("action_id", "params", "output_names"),
    [
        (
            "map.template",
            {"template": {"text": "Published: {{source}}"}},
            {"rendered": "label"},
        ),
        (
            "map.clean_dates",
            {"source": "source", "format": "%Y-%m-%d"},
            {"cleaned": "published"},
        ),
    ],
)
def test_estimate_uses_typed_request(
    project_client,
    monkeypatch: pytest.MonkeyPatch,
    action_id: str,
    params: dict[str, str],
    output_names: dict[str, str],
) -> None:
    client, project_id, sheet_id = project_client
    validations = 0
    bind_request = RegisteredAction.bind_request

    def count_validation(self, request):
        nonlocal validations
        validations += 1
        return bind_request(self, request)

    monkeypatch.setattr(RegisteredAction, "bind_request", count_validation)
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={
            "action": {
                "action_id": action_id,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": params,
                "output_names": output_names,
                "idempotency_key": f"estimate-{action_id}",
            }
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == {"kind": action_id}
    assert body["estimate"]["rows"] == 2
    assert body["estimate"]["cost"] == 0
    assert validations == 1


def test_typed_estimate_validates_semantic_inputs(project_client) -> None:
    client, project_id, sheet_id = project_client

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={
            "action": {
                "action_id": "map.clean_dates",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "missing"},
                "output_names": {"cleaned": "published"},
                "idempotency_key": "estimate-missing-source",
            }
        },
    )

    assert response.status_code == 400
    assert "referenced input columns do not exist" in response.text


def test_column_transform_estimate_runs_source_type_preflight(project_client) -> None:
    client, project_id, sheet_id = project_client

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={
            "action": {
                "action_id": "resolve.fill_missing",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "source", "method": "mean"},
                "output_names": {"cleaned": "filled"},
                "idempotency_key": "estimate-fill-text",
            }
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_params"


@pytest.mark.parametrize("gate", ["existing", "claimed"])
def test_column_transform_estimate_runs_output_gate(project_client, gate: str) -> None:
    client, project_id, sheet_id = project_client
    project = client.app.state.workspace.get(project_id)
    if gate == "existing":
        project.add_column(sheet_id, "filled")
        expected_code = "output_column_exists"
    else:
        _claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["filled"],
            action_kind="test.blocker",
        )
        assert conflict is None
        expected_code = "output_column_busy"

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={
            "action": {
                "action_id": "resolve.fill_missing",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "source", "method": "down"},
                "output_names": {"cleaned": "filled"},
                "idempotency_key": f"estimate-fill-{gate}",
            }
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == expected_code


def test_column_transform_param_preflight_uses_same_source_diagnostics(
    project_client,
) -> None:
    client, project_id, sheet_id = project_client
    project = client.app.state.workspace.get(project_id)
    project.add_column(sheet_id, "count", type="number")

    mean = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={
            "action": {
                "action_id": "resolve.fill_missing",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "source", "method": "mean"},
            }
        },
    )
    incompatible = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={
            "action": {
                "action_id": "resolve.substitute",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "count", "mapping": {"x": "y"}},
            }
        },
    )

    assert mean.status_code == 200, mean.text
    assert mean.json()["diagnostics"] == {
        "__all__": {
            "ok": False,
            "message": "method 'mean' requires a number or integer column",
        }
    }
    assert incompatible.status_code == 200, incompatible.text
    assert incompatible.json()["diagnostics"] == {
        "source": {
            "ok": False,
            "message": "source column has an incompatible type",
        }
    }


def test_typed_param_preflight_accepts_action_id_and_returns_pydantic_diagnostics(
    project_client,
) -> None:
    client, project_id, _sheet_id = project_client

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={
            "action": {
                "action_id": "map.to_geo_point",
                "params": {
                    "latitude_column": "source",
                    "longitude_column": "source",
                },
            }
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["action"] == {"kind": "map.to_geo_point"}
    assert response.json()["diagnostics"] == {
        "__all__": {
            "ok": False,
            "message": "latitude and longitude must come from different columns",
        }
    }


def test_typed_param_preflight_validates_template_refs_in_request_scope(
    project_client,
) -> None:
    client, project_id, sheet_id = project_client

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={
            "action": {
                "action_id": "map.template",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"template": {"text": "Published: {{unknown}}"}},
            }
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["diagnostics"] == {
        "template": {
            "ok": False,
            "message": 'No column named "unknown" on this sheet.',
        }
    }

    valid = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={
            "action": {
                "action_id": "map.template",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"template": {"text": "Published: {{source}}"}},
            }
        },
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["diagnostics"] == {}

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionError
from frisket.server.app import create_app


def test_project_evidence_cell_not_found_keeps_typed_action_error(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Evidence misses"}).json()[
        "id"
    ]

    response = client.get(f"/api/projects/{project_id}/cells/999/888/evidence")

    assert response.status_code == 404
    error = ActionError.model_validate(response.json())
    assert error.schema_version == "frisket.action_error.v1"
    assert error.code == "cell_not_found"
    assert error.field == "cell"
    assert error.details == {"row_id": 999, "column_id": 888}


def test_project_evidence_missing_project_keeps_detail_wrapped_action_error(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))

    response = client.get("/api/projects/missing-project/cells/999/888/evidence")

    assert response.status_code == 404
    error = ActionError.model_validate(response.json()["detail"])
    assert error.schema_version == "frisket.action_error.v1"
    assert error.code == "project_not_found"
    assert error.field == "project_id"
    assert error.details == {"project_id": "missing-project"}

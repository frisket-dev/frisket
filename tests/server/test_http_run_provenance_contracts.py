"""HTTP contract shape for row-scoped trace and project provenance reads."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _operation(document: dict[str, object], path: str) -> dict[str, object]:
    paths = document["paths"]
    assert isinstance(paths, dict)
    path_item = paths[path]
    assert isinstance(path_item, dict)
    operation = path_item["get"]
    assert isinstance(operation, dict)
    return operation


def test_row_trace_and_provenance_contracts_are_typed_and_query_bounded(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "ws")
    document = app.openapi()

    row = _operation(
        document, "/api/projects/{pid}/actions/runs/{run_id}/trace/rows/{row_id}"
    )
    assert row["responses"].keys() >= {"200", "404", "422"}
    row_schema = row["responses"]["200"]["content"]["application/json"]["schema"]
    assert row_schema == {"$ref": "#/components/schemas/RunTraceRowEvidence"}

    provenance = _operation(document, "/api/projects/{pid}/provenance")
    assert provenance["responses"].keys() >= {"200", "404", "422"}
    provenance_schema = provenance["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert provenance_schema == {"$ref": "#/components/schemas/ProvenanceManifest"}
    params = {item["name"]: item for item in provenance["parameters"]}
    assert params["runs_offset"]["schema"]["minimum"] == 0
    assert params["receipts_offset"]["schema"]["minimum"] == 0
    assert params["runs_limit"]["schema"]["minimum"] == 1
    assert params["runs_limit"]["schema"]["maximum"] == 100
    assert params["receipts_limit"]["schema"]["minimum"] == 1
    assert params["receipts_limit"]["schema"]["maximum"] == 100


def test_run_provenance_contracts_preserve_existing_route_bytes(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "contracts"}).json()["id"]

    invalid = client.get(
        f"/api/projects/{project_id}/actions/runs/not-an-int/trace/rows/1"
    )
    assert invalid.status_code == 422
    unknown_run = client.get(
        f"/api/projects/{project_id}/actions/runs/999/trace/rows/1"
    )
    assert unknown_run.status_code == 404
    assert client.get(f"/api/projects/{project_id}/provenance").status_code == 200

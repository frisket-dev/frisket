from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytest.importorskip(
    "followthemoney",
    reason="requires the entities extra: pip install 'frisket-data[entities]'",
)

from frisket.server.app import create_app


def _jsonl(path: Path, *items: dict[str, Any]) -> Path:
    path.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in items) + "\n",
        encoding="utf-8",
    )
    return path


def _ftm_fixture(path: Path) -> Path:
    return _jsonl(
        path,
        {
            "id": "person-1",
            "schema": "Person",
            "properties": {"name": ["Jane Smith"]},
        },
        {
            "id": "company-1",
            "schema": "Company",
            "properties": {"name": ["Acme LLC"]},
        },
        {
            "id": "company-2",
            "schema": "Company",
            "properties": {"name": ["Beta Ltd"]},
        },
        {
            "id": "membership-1",
            "schema": "Membership",
            "properties": {
                "member": ["person-1"],
                "organization": ["company-1"],
                "role": ["Director"],
            },
        },
        {
            "id": "membership-2",
            "schema": "Membership",
            "properties": {
                "member": ["person-1"],
                "organization": ["company-2"],
                "role": ["Advisor"],
            },
        },
    )


def _seed_followthemoney(
    project: Any, source_path: Path, *, dataset_name: str
) -> dict[str, Any]:
    """Seed the FtM-shaped sheets the graph route reads by importing the fixture via
    the frisket.ftm plugin WritePlan mechanism (the core import.followthemoney action
    kind was removed in ftm-bundled-plugin-v1; the WritePlan path builds identical
    sheets)."""
    from frisket.features.followthemoney.import_planner import (
        plan_followthemoney_import,
    )
    from frisket.features.followthemoney.migration_harness import import_via_write_plan

    plan = plan_followthemoney_import(
        source_path.read_bytes(), dataset_name=dataset_name
    )
    import_via_write_plan(project, plan)
    sheet_ids = {
        row["name"]: int(row["id"])
        for row in project.db.execute("SELECT id, name FROM sheets").fetchall()
    }

    def _refs(sheets: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            sheet["schema"]: {
                "sheet_id": sheet_ids[sheet["sheet_name"]],
                "sheet_name": sheet["sheet_name"],
                "schema_name": sheet["schema"],
                "row_count": sheet["row_count"],
            }
            for sheet in sheets
        }

    return {
        "schema_sheets": _refs(plan["sheets"]),
        "relationship_sheets": _refs(plan["relationship_sheets"]),
    }


def _import_fixture(client: TestClient, tmp_path: Path) -> tuple[str, dict[str, Any]]:
    source_path = _ftm_fixture(tmp_path / "graph-http.ftm.jsonl")
    pid = client.post("/api/projects", json={"name": "Graph HTTP"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    summary = _seed_followthemoney(
        project, source_path, dataset_name="Graph HTTP fixture"
    )
    return pid, summary


def _columns(project: Any, sheet_id: int) -> dict[str, Any]:
    return {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchall()
    }


def _set_relationship_review_states(
    project: Any, membership_ref: dict[str, Any], states: dict[str, str]
) -> None:
    sheet_id = int(membership_ref["sheet_id"])
    columns = _columns(project, sheet_id)
    review_col = project.add_column(sheet_id, "review_state", type="text")
    ftm_values = project.get_values(sheet_id, int(columns["_ftm_id"]["id"]))
    project.apply_edits(
        [
            {"row_id": row_id, "column_id": review_col, "value": state}
            for row_id, ftm_id in ftm_values.items()
            if (state := states.get(str(ftm_id))) is not None
        ],
        label="graph fixture review states",
    )


def test_graph_neighborhood_route_returns_bounded_ftm_projection(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, summary = _import_fixture(client, tmp_path)
    project = client.app.state.workspace.get(pid)
    _set_relationship_review_states(
        project,
        summary["relationship_sheets"]["Membership"],
        {"membership-1": "verified", "membership-2": "rejected"},
    )

    response = client.get(
        f"/api/projects/{pid}/graph/neighborhood",
        params={
            "anchor_id": "person-1",
            "depth": 1,
            "review_state": "verified",
            "limit_nodes": 50,
            "limit_edges": 50,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.graph_neighborhood.v1"
    assert payload["anchor"]["id"] == "person-1"
    assert [node["id"] for node in payload["nodes"]] == ["person-1", "company-1"]
    assert [edge["id"] for edge in payload["edges"]] == ["membership-1"]
    assert payload["edges"][0]["endpoint_refs"]["source"]["property"] == "member"
    assert payload["edges"][0]["endpoint_refs"]["target"]["property"] == "organization"
    assert payload["limits"] == {
        "depth": 1,
        "nodes": 50,
        "edges": 50,
        "returned_nodes": 2,
        "returned_edges": 1,
    }
    assert payload["diagnostics"] == []


def test_graph_neighborhood_route_maps_invalid_anchor_to_typed_404(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, _summary = _import_fixture(client, tmp_path)

    response = client.get(
        f"/api/projects/{pid}/graph/neighborhood",
        params={"anchor_id": "missing-entity"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "anchor_not_found",
        "message": "FtM entity anchor not found: missing-entity",
    }


def test_graph_neighborhood_route_rejects_non_entity_anchor_kind(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid, _summary = _import_fixture(client, tmp_path)

    response = client.get(
        f"/api/projects/{pid}/graph/neighborhood",
        params={"anchor_kind": "row", "anchor_id": "person-1"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "unsupported_anchor_kind",
        "message": "graph neighborhood v1 supports only anchor_kind=entity",
    }

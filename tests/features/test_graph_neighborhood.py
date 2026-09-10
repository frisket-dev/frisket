from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytest.importorskip(
    "followthemoney",
    reason="requires the entities extra: pip install 'frisket[entities]'",
)

from frisket.features.graph.neighborhood import build_graph_neighborhood
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
            "id": "person-2",
            "schema": "Person",
            "properties": {"name": ["Alex Jones"]},
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
                "startDate": ["2020-03-04"],
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
        {
            "id": "membership-3",
            "schema": "Membership",
            "properties": {
                "member": ["person-2"],
                "organization": ["company-1"],
                "role": ["Officer"],
            },
        },
    )


def _seed_followthemoney(
    project: Any, source_path: Path, *, dataset_name: str
) -> dict[str, Any]:
    """Seed the FtM-shaped sheets the graph feature reads by importing the fixture
    through the frisket.ftm plugin's WritePlan mechanism (the core import action kind
    was removed in ftm-bundled-plugin-v1; the WritePlan path builds identical sheets)."""
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


def _import_fixture(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    client = TestClient(create_app(tmp_path / "ws"))
    source_path = _ftm_fixture(tmp_path / "graph.ftm.jsonl")
    pid = client.post("/api/projects", json={"name": "Graph"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    summary = _seed_followthemoney(project, source_path, dataset_name="Graph fixture")
    return project, summary


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
    edits = [
        {"row_id": row_id, "column_id": review_col, "value": state}
        for row_id, ftm_id in ftm_values.items()
        if (state := states.get(str(ftm_id))) is not None
    ]
    project.apply_edits(edits, label="graph fixture review states")


def _counts(project: Any) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "cells", "edits", "ops")
    }


def test_neighborhood_projects_imported_ftm_links_with_refs_and_limit_diagnostics(
    tmp_path: Path,
) -> None:
    project, summary = _import_fixture(tmp_path)
    membership_ref = summary["relationship_sheets"]["Membership"]
    _set_relationship_review_states(
        project,
        membership_ref,
        {
            "membership-1": "verified",
            "membership-2": "verified",
            "membership-3": "verified",
        },
    )
    before = _counts(project)

    payload = build_graph_neighborhood(
        project,
        anchor_id="person-1",
        review_states={"verified"},
        limit_nodes=2,
        limit_edges=10,
    )

    assert _counts(project) == before
    assert payload["schema_version"] == "frisket.graph_neighborhood.v1"
    assert payload["anchor"]["id"] == "person-1"
    assert payload["anchor"]["schema"] == "Person"
    assert [node["id"] for node in payload["nodes"]] == ["person-1", "company-1"]
    assert payload["nodes"][0]["label"] == "Jane Smith"
    assert (
        payload["nodes"][0]["row_ref"]["sheet_id"]
        == summary["schema_sheets"]["Person"]["sheet_id"]
    )
    assert payload["edges"][0]["id"] == "membership-1"
    assert payload["edges"][0]["source"] == "person-1"
    assert payload["edges"][0]["target"] == "company-1"
    assert payload["edges"][0]["schema"] == "Membership"
    assert payload["edges"][0]["label"] == "belongs to"
    assert payload["edges"][0]["review_state"] == "verified"
    assert payload["edges"][0]["endpoint_refs"] == {
        "source": {
            "property": "member",
            "ftm_id": "person-1",
            "node_id": "person-1",
        },
        "target": {
            "property": "organization",
            "ftm_id": "company-1",
            "node_id": "company-1",
        },
    }
    assert payload["edges"][0]["row_ref"]["sheet_id"] == membership_ref["sheet_id"]
    assert payload["truncated"] is True
    assert payload["limits"]["nodes"] == 2
    assert {item["code"] for item in payload["diagnostics"]} == {"node_limit_reached"}


def test_neighborhood_filters_review_state_and_depth(tmp_path: Path) -> None:
    project, summary = _import_fixture(tmp_path)
    _set_relationship_review_states(
        project,
        summary["relationship_sheets"]["Membership"],
        {
            "membership-1": "verified",
            "membership-2": "rejected",
            "membership-3": "verified",
        },
    )

    default_payload = build_graph_neighborhood(project, anchor_id="person-1")
    assert [edge["id"] for edge in default_payload["edges"]] == ["membership-1"]

    rejected_payload = build_graph_neighborhood(
        project,
        anchor_id="person-1",
        review_states={"rejected"},
    )
    assert [edge["id"] for edge in rejected_payload["edges"]] == ["membership-2"]

    two_hop_payload = build_graph_neighborhood(
        project,
        anchor_id="person-1",
        depth=2,
        review_states={"verified"},
    )
    assert [edge["id"] for edge in two_hop_payload["edges"]] == [
        "membership-1",
        "membership-3",
    ]
    assert {node["id"] for node in two_hop_payload["nodes"]} == {
        "person-1",
        "company-1",
        "person-2",
    }

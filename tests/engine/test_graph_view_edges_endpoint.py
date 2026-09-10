from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.materialization import (
    EdgeTableColumnSpec,
    EdgeTablePlan,
    MaterializedEdgeRecord,
    write_edge_table,
)


PROJECT_ID = "graphview"
_KEYGEN = itertools.count(1)


def _client(ws: Path) -> TestClient:
    return TestClient(create_app(ws, router=ModelRouter(cache=None, cache_mode="off")))


def _run_action(project: Project, action: dict[str, Any]):
    from frisket.engine.executor import actions as executor_actions

    return executor_actions.run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
    )


def _sheet_id_by_name(project: Project, name: str) -> int:
    row = project.db.execute(
        "SELECT id FROM sheets WHERE name=? AND hidden=0", (name,)
    ).fetchone()
    assert row is not None, f"sheet {name!r} not found"
    return int(row["id"])


def _seed_join_project(ws: Path) -> dict[str, Any]:
    """A derive.join output: symmetric join_left/join_right membership."""
    project = Project.create(ws / f"{PROJECT_ID}.frisket", name="Graph View")
    try:
        left_id = project.add_sheet("States")
        left_cols = {
            "state_fips": project.add_column(left_id, "state_fips", type="integer"),
            "state_name": project.add_column(left_id, "state_name", type="text"),
        }
        project.add_rows(
            left_id,
            [
                {"state_fips": 1, "state_name": "Alabama"},
                {"state_fips": 2, "state_name": "Alaska"},
                {"state_fips": 6, "state_name": "California"},
            ],
            left_cols,
        )
        right_id = project.add_sheet("Population")
        right_cols = {
            "state_fips": project.add_column(right_id, "state_fips", type="integer"),
            "region": project.add_column(right_id, "region", type="text"),
        }
        project.add_rows(
            right_id,
            [
                {"state_fips": 1, "region": "South"},
                {"state_fips": 2, "region": "West"},
                {"state_fips": 6, "region": "West"},
            ],
            right_cols,
        )
        action = {
            "action_id": "derive.join",
            "scope": {"kind": "sheet_rows", "sheet_id": left_id},
            "sheet_name": "States x Population",
            "params": {
                "right": {"sheet_id": right_id},
                "join_keys": [
                    {"left_column": "state_fips", "right_column": "state_fips"}
                ],
                "how": "inner",
                "indicator": False,
            },
            "idempotency_key": f"derive_join@sha256:{next(_KEYGEN)}",
        }
        _run_action(project, action)
        join_sheet_id = _sheet_id_by_name(project, "States x Population")
        # a plain CSV-shaped sheet with no membership
        csv_id = project.add_sheet("Notes")
        note_cols = {"note": project.add_column(csv_id, "note", type="text")}
        project.add_rows(csv_id, [{"note": "hello"}], note_cols)
        return {
            "left_id": left_id,
            "right_id": right_id,
            "left_cols": left_cols,
            "right_cols": right_cols,
            "join_sheet_id": join_sheet_id,
            "csv_id": csv_id,
        }
    finally:
        project.close()


def _seed_link_project(ws: Path) -> dict[str, Any]:
    """A directed derive.link_table output: edge_source -> edge_target."""
    project = Project.create(ws / f"{PROJECT_ID}.frisket", name="Graph View")
    try:
        people_id = project.add_sheet("People")
        people_cols = {
            "name": project.add_column(people_id, "name", type="text"),
            "team": project.add_column(people_id, "team", type="text"),
        }
        row_ids = project.add_rows(
            people_id,
            [
                {"name": "Alice", "team": "red"},
                {"name": "Bob", "team": "blue"},
                {"name": "Carol", "team": "red"},
            ],
            people_cols,
        )
        alice, bob, carol = row_ids
        cur = project.db.cursor()
        write_edge_table(
            cur,
            EdgeTablePlan(
                action_kind="derive.link_table",
                label="knows",
                target_sheet_name="Knows",
                parent_sheet_id=people_id,
                op_spec={"kind": "derive.link_table"},
                columns=[EdgeTableColumnSpec(name="relation", type="text")],
                edges=[
                    MaterializedEdgeRecord(
                        source_row_id=alice,
                        target_row_id=bob,
                        values={"relation": "manages"},
                    ),
                    MaterializedEdgeRecord(
                        source_row_id=bob,
                        target_row_id=carol,
                        values={"relation": "mentors"},
                    ),
                ],
            ),
        )
        project.db.commit()
        link_sheet_id = _sheet_id_by_name(project, "Knows")
        return {
            "people_id": people_id,
            "people_cols": people_cols,
            "row_ids": row_ids,
            "link_sheet_id": link_sheet_id,
        }
    finally:
        project.close()


def _get_graph(client: TestClient, sheet_id: int, **params: Any) -> dict[str, Any]:
    r = client.get(f"/api/projects/{PROJECT_ID}/sheets/{sheet_id}/graph", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# Join tables: symmetric membership -> undirected default
# --------------------------------------------------------------------------


def test_join_sheet_yields_nodes_edges_from_membership(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed_join_project(ws)
    client = _client(ws)

    body = _get_graph(client, ids["join_sheet_id"])
    assert body["schema_version"] == "frisket.sheet_graph.v1"
    assert body["materialized_kind"] == "join"
    assert body["direction"] == "undirected"

    # 3 join rows -> 3 edges; nodes = distinct endpoint refs (3 states + 3 pop = 6)
    assert body["limits"]["returned_edges"] == 3
    assert len(body["edges"]) == 3
    assert len(body["nodes"]) == 6
    assert body["truncated"] is False

    # every edge is undirected and connects a States node to a Population node
    left_id, right_id = ids["left_id"], ids["right_id"]
    node_sheets = {n["id"]: n["sheet_id"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert edge["direction"] == "undirected"
        assert {node_sheets[edge["source"]], node_sheets[edge["target"]]} == {
            left_id,
            right_id,
        }
        assert edge["row_ref"]["sheet_id"] == ids["join_sheet_id"]


def test_join_resolves_requested_node_columns(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed_join_project(ws)
    client = _client(ws)

    body = _get_graph(
        client,
        ids["join_sheet_id"],
        node_label_column_id=ids["left_cols"]["state_name"],
        node_color_column_id=ids["right_cols"]["region"],
    )
    labels = {n["label"] for n in body["nodes"] if n["sheet_id"] == ids["left_id"]}
    assert {"Alabama", "Alaska", "California"} <= labels
    colors = {
        n["color_value"] for n in body["nodes"] if n["sheet_id"] == ids["right_id"]
    }
    assert {"South", "West"} <= colors


# --------------------------------------------------------------------------
# Link tables: directed edge_source -> edge_target
# --------------------------------------------------------------------------


def test_link_sheet_is_directed_with_correct_tail_head(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed_link_project(ws)
    client = _client(ws)

    body = _get_graph(
        client,
        ids["link_sheet_id"],
        node_label_column_id=ids["people_cols"]["name"],
    )
    assert body["materialized_kind"] == "edge"
    assert body["direction"] == "directed"
    assert len(body["edges"]) == 2
    assert len(body["nodes"]) == 3

    label_by_id = {n["id"]: n["label"] for n in body["nodes"]}
    directed = {
        (label_by_id[e["source"]], label_by_id[e["target"]]) for e in body["edges"]
    }
    # tail=edge_source, head=edge_target — Alice manages Bob, Bob mentors Carol
    assert directed == {("Alice", "Bob"), ("Bob", "Carol")}
    for edge in body["edges"]:
        assert edge["direction"] == "directed"


def test_edge_label_resolves_from_edge_sheet_column(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed_link_project(ws)
    client = _client(ws)
    body = _get_graph(client, ids["link_sheet_id"])
    labels = {e["label"] for e in body["edges"]}
    # default edge label = first text column on the edge sheet (relation)
    assert labels == {"manages", "mentors"}


# --------------------------------------------------------------------------
# Honesty: non-edge (CSV) sheet + truncation
# --------------------------------------------------------------------------


def test_csv_sheet_returns_empty_with_diagnostic(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed_join_project(ws)
    client = _client(ws)

    body = _get_graph(client, ids["csv_id"])
    assert body["nodes"] == []
    assert body["edges"] == []
    assert body["materialized_kind"] is None
    codes = {d["code"] for d in body["diagnostics"]}
    assert "not_an_edge_sheet" in codes


def test_truncation_caps_edges(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed_link_project(ws)
    client = _client(ws)

    body = _get_graph(client, ids["link_sheet_id"], limit_edges=1)
    assert body["truncated"] is True
    assert body["limits"]["returned_edges"] == 1
    assert len(body["edges"]) == 1

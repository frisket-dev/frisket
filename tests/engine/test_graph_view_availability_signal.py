from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.features.graph.sheet_graph import (
    build_sheet_graph,
    sheet_materialized_kind,
)
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.materialization import (
    EdgeTableColumnSpec,
    EdgeTablePlan,
    MaterializedEdgeRecord,
    write_edge_table,
)
from test_cluster_receipt_reader import seeded as seeded


PROJECT_ID = "graphavail"
_KEYGEN = itertools.count(1)


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
    assert row is not None, name
    return int(row["id"])


def _seed(ws: Path) -> dict[str, int]:
    project = Project.create(ws / f"{PROJECT_ID}.frisket", name="Graph Avail")
    try:
        # --- plain CSV-shaped sheets (no membership) ---
        left_id = project.add_sheet("States")
        left_cols = {
            "state_fips": project.add_column(left_id, "state_fips", type="integer"),
            "state_name": project.add_column(left_id, "state_name", type="text"),
        }
        project.add_rows(
            left_id,
            [{"state_fips": 1, "state_name": "Alabama"}],
            left_cols,
        )
        right_id = project.add_sheet("Population")
        right_cols = {
            "state_fips": project.add_column(right_id, "state_fips", type="integer"),
            "region": project.add_column(right_id, "region", type="text"),
        }
        project.add_rows(
            right_id,
            [{"state_fips": 1, "region": "South"}],
            right_cols,
        )

        # --- join table (join_left/join_right) ---
        _run_action(
            project,
            {
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
            },
        )
        join_id = _sheet_id_by_name(project, "States x Population")

        # --- link table (edge_source/edge_target) ---
        people_id = project.add_sheet("People")
        people_cols = {"name": project.add_column(people_id, "name", type="text")}
        prows = project.add_rows(
            people_id,
            [{"name": "Alice"}, {"name": "Bob"}],
            people_cols,
        )
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
                        source_row_id=prows[0],
                        target_row_id=prows[1],
                        values={"relation": "knows"},
                    )
                ],
            ),
        )
        project.db.commit()
        link_id = _sheet_id_by_name(project, "Knows")

        return {
            "left_id": left_id,
            "right_id": right_id,
            "people_id": people_id,
            "join_id": join_id,
            "link_id": link_id,
        }
    finally:
        project.close()


def _list_sheets(client: TestClient) -> dict[int, dict[str, Any]]:
    r = client.get(f"/api/projects/{PROJECT_ID}/sheets")
    assert r.status_code == 200, r.text
    return {int(s["id"]): s for s in r.json()}


def test_sheet_list_exposes_materialized_kind_for_edge_and_join(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    by_id = _list_sheets(client)

    assert by_id[ids["join_id"]]["materialized_kind"] == "join"
    assert by_id[ids["link_id"]]["materialized_kind"] == "edge"


def test_plain_csv_sheets_carry_no_signal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    client = TestClient(
        create_app(ws, router=ModelRouter(cache=None, cache_mode="off"))
    )
    by_id = _list_sheets(client)

    # plain imported/base sheets must NOT offer the graph view
    for key in ("left_id", "right_id", "people_id"):
        assert "materialized_kind" not in by_id[ids[key]], key


def test_helper_requires_both_op_kind_and_membership(tmp_path: Path) -> None:
    """Op kind alone is insufficient — the membership roles must be present."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ids = _seed(ws)
    project = Project(ws / f"{PROJECT_ID}.frisket")
    try:
        assert (
            sheet_materialized_kind(project, ids["join_id"], op_kind="derive.join")
            == "join"
        )
        assert (
            sheet_materialized_kind(
                project, ids["link_id"], op_kind="derive.link_table"
            )
            == "edge"
        )
        # a base sheet with no membership never earns the signal, even if we
        # pretend an edge/join op produced it
        assert (
            sheet_materialized_kind(project, ids["left_id"], op_kind="derive.join")
            is None
        )
        # an unrelated op kind is never edge/join
        assert (
            sheet_materialized_kind(project, ids["join_id"], op_kind="import.csv")
            is None
        )
        assert (
            sheet_materialized_kind(project, ids["link_id"], op_kind="resolve.entities")
            is None
        )
    finally:
        project.close()


def test_resolved_entities_are_aggregate_rows_not_graph_edges(seeded):
    project, _, _, _, receipt_id = seeded
    result = _run_action(
        project,
        {
            "action_id": "resolve.entities",
            "scope": {"kind": "project"},
            "sheet_name": "Entities",
            "idempotency_key": "entities-graph",
            "params": {"source": {"kind": "cluster_values", "receipt_id": receipt_id}},
        },
    )
    assert result.status == "completed", result.errors
    sheet_id = result.outputs[0].sheet_id
    assert project.row_count(sheet_id) > 0
    roles = {
        row[0]
        for row in project.db.execute(
            "SELECT DISTINCT role FROM materialized_row_sources WHERE op_id=?",
            (result.op_ids[0],),
        )
    }
    assert roles == {"aggregate_source"}
    assert (
        sheet_materialized_kind(project, sheet_id, op_kind="resolve.entities") is None
    )
    graph = build_sheet_graph(project, sheet_id=sheet_id)
    assert graph["nodes"] == [] and graph["edges"] == []
    assert graph["materialized_kind"] is None
    assert graph["diagnostics"][0]["code"] == "not_an_edge_sheet"

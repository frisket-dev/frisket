"""GET /api/projects/{pid}/lineage — the Monitor Lineage DAG (Workbench IA inc 7).

A dedicated PRODUCT endpoint (``/debug`` stays debug-only) that generalizes
ProjectDebugService.debug's lineage shape into a three-tier DAG:
sources -> sheets -> AI columns. Stale sheet nodes and the edges into them are
flagged from the lazy staleness resolver so the UI paints them amber without
inventing state. These tests drive real actions and assert the tiers + a stale
node appear.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.executor.actions import run_action_spec


def _seed_parent() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Filings",
        "params": {
            "columns": [
                {"name": "title", "type": "text"},
                {"name": "rows_json", "type": "json"},
            ],
            "rows": [
                {"title": "Q1", "rows_json": [{"vendor": "Acme"}]},
                {"title": "Q2", "rows_json": [{"vendor": "Globex"}]},
            ],
            "source": {
                "kind": "inline",
                "label": "lineage seed",
                "fingerprint": "sha256:lineage-seed",
            },
        },
        "idempotency_key": "lineage_seed@sha256:v1",
    }


def _derive_column(*, sheet_id: int, column_id: int) -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Vendors",
        "params": {
            "source": {"kind": "column", "sheet_id": sheet_id, "column_id": column_id},
        },
        "idempotency_key": "lineage_derive@sha256:v1",
    }


def _build(client: TestClient) -> tuple[str, int, int, list[int]]:
    pid = client.post("/api/projects", json={"name": "Lineage"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    run_action_spec(project, _seed_parent(), project_id=pid)
    parent_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Filings'").fetchone()[
            "id"
        ]
    )
    col_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='rows_json'", (parent_id,)
        ).fetchone()["id"]
    )
    run_action_spec(
        project, _derive_column(sheet_id=parent_id, column_id=col_id), project_id=pid
    )
    row_ids = [
        int(r["id"])
        for r in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (parent_id,)
        )
    ]
    return pid, parent_id, col_id, row_ids


def test_lineage_endpoint_returns_sheet_tier_and_derive_edge(client) -> None:
    pid, parent_id, _, _ = _build(client)

    resp = client.get(f"/api/projects/{pid}/lineage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == pid
    assert "op_cursor" in body

    nodes = {n["id"]: n for n in body["nodes"]}
    vendors_id = int(
        client.app.state.workspace.get(pid)
        .db.execute("SELECT id FROM sheets WHERE name='Vendors'")
        .fetchone()["id"]
    )
    root_node = nodes[f"sheet:{parent_id}"]
    child_node = nodes[f"sheet:{vendors_id}"]
    assert root_node["kind"] == "sheet"
    assert root_node["derived"] is False
    assert root_node["syncState"] is None  # root sheets never carry syncState
    assert child_node["derived"] is True
    assert child_node["syncState"] == "synced"
    # HOW IT'S MADE: the real op kind is carried, not a hardcoded 'derive'.
    assert child_node["op_kind"] == "derive.table_from_list"

    edges = body["edges"]
    derive_edge = next(
        e
        for e in edges
        if e["from"] == f"sheet:{parent_id}" and e["to"] == f"sheet:{vendors_id}"
    )
    assert derive_edge["kind"] == "derive"
    assert derive_edge["stale"] is False
    sheets = client.get(f"/api/projects/{pid}/sheets").json()
    assert (
        next(sheet for sheet in sheets if sheet["id"] == vendors_id)["refreshable"]
        is True
    )


def test_lineage_endpoint_reports_unknown_sources_without_refresh_capability(
    client,
) -> None:
    pid, parent_id, _, _ = _build(client)
    project = client.app.state.workspace.get(pid)
    op_id = project.append_op(
        "media.extract_document", {"action_id": "media.extract_document"}
    )
    snapshot_id = project.add_sheet(
        "Extracted once", parent_sheet_id=parent_id, parent_op_id=op_id
    )
    project.db.commit()

    body = client.get(f"/api/projects/{pid}/lineage").json()
    snapshot = next(
        node for node in body["nodes"] if node["id"] == f"sheet:{snapshot_id}"
    )
    assert snapshot["derived"] is True
    assert snapshot["syncState"] == "unknown"
    edge = next(edge for edge in body["edges"] if edge["to"] == snapshot["id"])
    assert edge["stale"] is False
    listed = client.get(f"/api/projects/{pid}/sheets").json()
    listed_snapshot = next(sheet for sheet in listed if sheet["id"] == snapshot_id)
    assert listed_snapshot["refreshable"] is False
    assert listed_snapshot["syncState"] == "unknown"


def test_lineage_endpoint_flags_stale_node_and_edge_amber(client) -> None:
    pid, parent_id, col_id, row_ids = _build(client)
    project = client.app.state.workspace.get(pid)
    # Edit the parent -> the derived sheet is stale, so its node + inbound edge
    # must be flagged for the UI to paint amber.
    run_action_spec(
        project,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {
                        "row_id": row_ids[0],
                        "column_id": col_id,
                        "value": [{"vendor": "CHANGED"}],
                    }
                ]
            },
            "idempotency_key": "lineage_edit@v1",
        },
        project_id=pid,
    )

    body = client.get(f"/api/projects/{pid}/lineage").json()
    vendors_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Vendors'").fetchone()[
            "id"
        ]
    )
    child_node = next(n for n in body["nodes"] if n["id"] == f"sheet:{vendors_id}")
    assert child_node["syncState"] == "stale"
    assert child_node["stale_reason"] == "parent_changed"
    derive_edge = next(e for e in body["edges"] if e["to"] == f"sheet:{vendors_id}")
    assert derive_edge["stale"] is True


def test_lineage_endpoint_includes_source_tier(client) -> None:
    pid, parent_id, _, _ = _build(client)
    project = client.app.state.workspace.get(pid)
    # A source that lands rows on the root sheet is the first DAG tier.
    project.db.execute(
        "INSERT INTO sources (name, kind, sheet_id, last_status) "
        "VALUES ('PACER feed', 'url', ?, 'ok')",
        (parent_id,),
    )
    project.db.commit()

    body = client.get(f"/api/projects/{pid}/lineage").json()
    source_nodes = [n for n in body["nodes"] if n["kind"] == "source"]
    assert len(source_nodes) == 1
    src = source_nodes[0]
    assert src["name"] == "PACER feed"
    assert src["sheet_id"] == parent_id
    source_edge = next(e for e in body["edges"] if e["from"] == src["id"])
    assert source_edge["to"] == f"sheet:{parent_id}"
    assert source_edge["kind"] == "source"


def test_retained_output_reports_stale_sources_when_recipe_is_unavailable(
    client,
) -> None:
    from frisket.engine.store.changes import start_change, close_change
    from frisket.engine.store.consumed_sources import (
        read_consumed_source,
        store_capture,
    )

    pid, parent_id, column_id, rows = _build(client)
    project = client.app.state.workspace.get(pid)
    owner = project.append_op("uninstalled_plugin.derive", {})
    child = project.add_sheet(
        "Retained output", parent_sheet_id=parent_id, parent_op_id=owner
    )
    _, _, capture = read_consumed_source(project, parent_id, [column_id])
    with project.transaction_scope():
        start_change(project.db, owner)
        store_capture(project, capture, owner_op_id=owner, output_sheet_id=child)
        close_change(project.db, owner)
    project.apply_edits(
        [{"row_id": rows[0], "column_id": column_id, "value": [{"vendor": "Changed"}]}]
    )
    listed = next(
        sheet
        for sheet in client.get(f"/api/projects/{pid}/sheets").json()
        if sheet["id"] == child
    )
    assert listed["refreshable"] is False
    assert listed["syncState"] == "stale"
    graph = client.get(f"/api/projects/{pid}/lineage").json()
    assert (
        next(node for node in graph["nodes"] if node["id"] == f"sheet:{child}")[
            "syncState"
        ]
        == "stale"
    )
    assert (
        next(edge for edge in graph["edges"] if edge["to"] == f"sheet:{child}")["stale"]
        is True
    )

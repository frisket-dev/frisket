from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.querysets import resolve_sheet_filter_rows


def _seed_project(client: TestClient) -> tuple[str, int, dict[str, int]]:
    pid = client.post("/api/projects", json={"name": "QuerySpec rowsets"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("tasks")
    columns = {
        "title": project.add_column(sheet_id, "title"),
        "status": project.add_column(sheet_id, "status"),
        "published": project.add_column(sheet_id, "published", type="date"),
        "active": project.add_column(sheet_id, "active", type="boolean"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "A start",
                "status": "todo",
                "published": "2026-01-01",
                "active": True,
            },
            {
                "title": "B middle",
                "status": "todo",
                "published": "2026-02-01",
                "active": False,
            },
            {
                "title": "C edited",
                "status": "todo",
                "published": "2026-03-01",
                "active": True,
            },
            {
                "title": "D done",
                "status": "done",
                "published": "2026-04-01",
                "active": True,
            },
        ],
        columns,
    )
    project.apply_edits(
        [
            {
                "row_id": row_ids[2],
                "column_id": columns["status"],
                "value": "done",
            }
        ],
        label="mark C done",
    )
    return (
        pid,
        sheet_id,
        {
            "a": row_ids[0],
            "b": row_ids[1],
            "c": row_ids[2],
            "d": row_ids[3],
        },
    )


def test_shared_sheet_filter_rowset_drives_grid_locate_and_watch(tmp_path: Path):
    client = _client(tmp_path)
    pid, sheet_id, rows = _seed_project(client)
    project = client.app.state.workspace.get(pid)
    filter_spec = {
        "status": {"eq": "done"},
        "published": {"gte": "2026-03-01"},
    }
    sort_spec = [{"column": "published", "dir": "asc"}]

    resolved = resolve_sheet_filter_rows(
        project,
        sheet_id,
        filter_=json.dumps(filter_spec),
        sort=json.dumps(sort_spec),
        limit=50,
    )
    assert resolved.total == 2
    assert resolved.row_ids == [rows["c"], rows["d"]]

    boolean_resolved = resolve_sheet_filter_rows(
        project,
        sheet_id,
        filter_=json.dumps({"active": {"eq": "true"}}),
        limit=50,
    )
    assert boolean_resolved.row_ids == [rows["a"], rows["c"], rows["d"]]

    grid = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/data",
        params={"filter": json.dumps(filter_spec), "sort": json.dumps(sort_spec)},
    )
    assert grid.status_code == 200, grid.text
    assert [row["id"] for row in grid.json()["rows"]] == resolved.row_ids

    located = client.get(
        f"/api/projects/{pid}/sheets/{sheet_id}/rows/{rows['d']}/locate",
        params={
            "filter": json.dumps(filter_spec),
            "sort": json.dumps(sort_spec),
            "page_size": 1,
        },
    )
    assert located.status_code == 200, located.text
    assert located.json()["found"] is True
    assert located.json()["index"] == 1
    assert located.json()["page_offset"] == 1

    watch = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Done tasks",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": filter_spec,
            },
        },
    )
    assert watch.status_code == 200, watch.text
    run = client.post(f"/api/projects/{pid}/watches/{watch.json()['id']}/run")
    assert run.status_code == 200, run.text
    assert [hit["row_id"] for hit in run.json()["hits"]] == resolved.row_ids

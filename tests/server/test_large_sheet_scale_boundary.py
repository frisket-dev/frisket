from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]


def test_sheet_row_locate_uses_current_filter_sort_scope(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Locate scale"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("events")
    title_col = project.add_column(sheet_id, "title")
    status_col = project.add_column(sheet_id, "status")
    rank_col = project.add_column(sheet_id, "rank", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": f"Scale row {index}",
                "status": "open" if index % 2 == 0 else "closed",
                "rank": index,
            }
            for index in range(12)
        ],
        {"title": title_col, "status": status_col, "rank": rank_col},
    )

    filter_spec = json.dumps({"status": {"eq": "open"}})
    sort_spec = json.dumps([{"column": "rank", "dir": "desc"}])
    located = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/rows/{row_ids[0]}/locate",
        params={
            "filter": filter_spec,
            "sort": sort_spec,
            "page_size": 3,
        },
    )

    assert located.status_code == 200, located.text
    body = located.json()
    assert body == {
        "schema_version": "frisket.sheet_row_location.v1",
        "sheet_id": sheet_id,
        "row_id": row_ids[0],
        "found": True,
        "index": 5,
        "page_offset": 3,
        "page_size": 3,
    }

    page = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={
            "filter": filter_spec,
            "sort": sort_spec,
            "offset": body["page_offset"],
            "limit": body["page_size"],
        },
    )
    assert page.status_code == 200, page.text
    assert row_ids[0] in [row["id"] for row in page.json()["rows"]]

    hidden_by_filter = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/rows/{row_ids[1]}/locate",
        params={
            "filter": filter_spec,
            "sort": sort_spec,
            "page_size": 3,
        },
    )
    assert hidden_by_filter.status_code == 200, hidden_by_filter.text
    assert hidden_by_filter.json()["found"] is False

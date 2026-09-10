from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _seed_history(
    tmp_path: Path,
) -> tuple[TestClient, str, list[int], int]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Operation history paging"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    op_ids: list[int] = []
    for index in range(125):
        op_ids.append(
            project.append_op(
                "map" if index % 3 else "edit",
                {"index": index},
                label=f"history op {index + 1}",
            )
        )
    for _ in range(5):
        assert project.undo() is not None
    return client, project_id, op_ids, project.op_cursor


def test_operation_history_route_returns_bounded_page_with_cursor_targets(
    tmp_path: Path,
) -> None:
    client, project_id, op_ids, cursor = _seed_history(tmp_path)

    default_page = client.get(
        f"/api/projects/{project_id}/history",
        params={"limit": 20},
    )
    assert default_page.status_code == 200, default_page.text
    body = default_page.json()
    assert body["schema_version"] == "frisket.history_page.v1"
    assert body["order"] == "asc"
    assert body["limit"] == 20
    assert body["total"] == len(op_ids)
    assert len(body["ops"]) <= 20
    assert body["cursor_index"] == op_ids.index(cursor)
    assert body["cursor_op"]["id"] == cursor
    assert body["cursor_op_loaded"] is True
    assert body["undo_target"]["id"] == cursor
    assert body["redo_target"]["id"] == cursor + 1
    assert body["revision"] == {
        "total": len(op_ids),
        "max_op_id": op_ids[-1],
        "op_cursor": cursor,
    }
    assert all("index" in op for op in body["ops"])
    assert body["ops"] == sorted(body["ops"], key=lambda op: op["index"])

    first_page = client.get(
        f"/api/projects/{project_id}/history",
        params={"offset": 0, "limit": 20},
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert first_body["offset"] == 0
    assert first_body["has_more_before"] is False
    assert first_body["has_more_after"] is True
    assert first_body["prev_offset"] is None
    assert first_body["next_offset"] == 20
    assert first_body["cursor_op"]["id"] == cursor
    assert first_body["cursor_op_loaded"] is False
    assert [op["id"] for op in first_body["ops"]] == op_ids[:20]

    tail_page = client.get(
        f"/api/projects/{project_id}/history",
        params={"offset": 120, "limit": 20},
    )
    assert tail_page.status_code == 200, tail_page.text
    tail_body = tail_page.json()
    assert tail_body["offset"] == 120
    assert tail_body["has_more_after"] is False
    assert tail_body["next_offset"] is None
    assert [op["id"] for op in tail_body["ops"]] == op_ids[120:]

    for params in (
        {"offset": -1, "limit": 20},
        {"offset": 0, "limit": 0},
        {"offset": 0, "limit": 101},
    ):
        invalid = client.get(f"/api/projects/{project_id}/history", params=params)
        assert invalid.status_code == 422

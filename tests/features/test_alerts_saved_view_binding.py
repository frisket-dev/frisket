"""Alerts bind one saved selection; edits and ordering cannot silently diverge."""

from __future__ import annotations
import sqlite3
import pytest
from helpers import make_client


def scene(tmp_path):
    client = make_client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Views"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("rows")
    col = project.add_column(sheet, "value", type="number")
    rows = project.add_rows(
        sheet, [{"value": 1}, {"value": 2}, {"value": 3}], {"value": col}
    )
    selection = {
        "scope": {"kind": "sheet", "sheet_id": sheet},
        "order": [{"column_id": col, "dir": "desc"}],
        "limit": 2,
    }
    response = client.post(
        f"/api/projects/{pid}/views", json={"name": "Top two", "selection": selection}
    )
    assert response.status_code == 200, response.text
    view = response.json()
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Changes",
            "view_id": view["id"],
            "detection_policy": {"kind": "conditions", "entered": True, "exited": True},
        },
    )
    assert response.status_code == 200, response.text
    return client, pid, project, sheet, col, rows, view, response.json()


def test_alert_grid_and_saved_definition_share_full_membership(tmp_path):
    client, pid, project, sheet, col, rows, view, alert = scene(tmp_path)
    prefix = f"/api/projects/{pid}"
    grid = client.get(f"{prefix}/views/{view['id']}/resolve?limit=1").json()
    assert grid["total"] == 2
    assert [ref["row_id"] for ref in grid["refs"]] == rows[2:]
    check = client.post(f"{prefix}/watches/{alert['id']}/run").json()
    assert check["run"]["matched_rows"] == 2
    assert check["run"]["new_rows"] == 0
    selected = {**view["spec"]["selection"], "limit": 1}
    assert (
        client.put(
            f"{prefix}/views/{view['id']}/definition", json={"selection": selected}
        ).status_code
        == 200
    )
    next_check = client.post(f"{prefix}/watches/{alert['id']}/run").json()
    assert next_check["run"]["matched_rows"] == 1
    assert next_check["run"]["new_rows"] == 0
    project.apply_edits([{"row_id": rows[0], "column_id": col, "value": 99}])
    final = client.post(f"{prefix}/watches/{alert['id']}/run").json()
    assert final["run"]["matched_rows"] == 1
    assert final["run"]["new_rows"] == 1
    assert {
        event["event_kind"] for event in project.alert_check_events(final["run"]["id"])
    } == {"row_entered", "row_exited"}
    assert client.delete(f"{prefix}/views/{view['id']}").status_code == 200
    assert project.get_alert(alert["id"]) is None
    assert client.get(f"{prefix}/notifications").json()["total"] == 0


def test_one_alert_per_view_and_owned_query_is_rejected(tmp_path):
    client, pid, project, _, _, _, view, alert = scene(tmp_path)
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={"name": "Duplicate", "view_id": view["id"]},
    )
    assert response.status_code == 400
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={"name": "Legacy", "query": {"kind": "filter"}, "view_id": view["id"]},
    )
    assert response.status_code == 422
    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute("INSERT INTO watches(name) VALUES('No view')")
    project.db.rollback()
    changed = client.patch(
        f"/api/projects/{pid}/watches/{alert['id']}",
        json={
            "detection_policy": {
                "kind": "conditions",
                "entered": True,
                "count_changed": True,
            }
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["detection_policy"]["count_changed"] is True

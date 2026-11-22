from __future__ import annotations

from tests.alert_helpers import post_alert

from pathlib import Path

from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.search_index import drain_index
from frisket.notifications.producers import (
    run_transition_notification_candidate,
)


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_alert(client: TestClient) -> tuple[str, dict, dict, list[dict]]:
    pid = client.post("/api/projects", json={"name": "Lane 5 notifications"}).json()[
        "id"
    ]
    import_response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert import_response.status_code == 200, import_response.text
    drain_index(client.app.state.workspace.get(pid))
    alert_response = post_alert(
        client,
        f"/api/projects/{pid}/alerts",
        json={
            "name": "Budget watch",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget", "limit": 10},
        },
    )
    assert alert_response.status_code == 200, alert_response.text
    alert = alert_response.json()
    from alert_helpers import seed_empty_alert_baseline

    seed_empty_alert_baseline(client.app.state.workspace.get(pid), alert["id"])
    run_response = client.post(f"/api/projects/{pid}/alerts/{alert['id']}/check")
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()["run"]
    events_response = client.get(
        f"/api/projects/{pid}/alerts/{alert['id']}/checks/{run['id']}/events",
        params={"limit": 10},
    )
    assert events_response.status_code == 200, events_response.text
    return pid, alert, run, events_response.json()["events"]


def test_alert_check_events_project_to_generic_notifications_without_mutating_facts(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, alert, run, events = _seed_alert(client)
    event_ids = [event["id"] for event in events]
    assert event_ids

    page = client.get(f"/api/projects/{pid}/notifications").json()
    assert page["schema_version"] == "frisket.notifications_page.v1"
    assert page["total"] == 1
    item = page["notifications"][0]
    assert item["source_kind"] == "alert"
    assert item["source_ref"] == {"alert_id": alert["id"], "run_id": run["id"]}
    assert item["source_event_ids"] == event_ids
    project = client.app.state.workspace.get(pid)
    stored = project.db.execute(
        "SELECT dedupe_key FROM notification_items WHERE id=?",
        (item["id"],),
    ).fetchone()
    assert stored["dedupe_key"] == f"alert:{alert['id']}:run:{run['id']}"
    assert "," not in stored["dedupe_key"]
    assert len(stored["dedupe_key"]) < 140
    assert item["event_kinds"] == ["row_entered"]
    assert item["event_count"] == len(event_ids)
    assert item["deep_link"] == {
        "kind": "alert_check",
        "alert_id": alert["id"],
        "run_id": run["id"],
        "event_ids": event_ids,
    }
    assert item["state"] == "unseen"

    summary = client.get(f"/api/projects/{pid}/notifications/summary").json()
    assert summary["schema_version"] == "frisket.notifications_summary.v1"
    assert summary["unseen"] == 1
    assert summary["by_source_kind"] == {"alert": 1}
    assert summary["by_source_ref"] == [
        {
            "source_kind": "alert",
            "source_ref": {"alert_id": alert["id"]},
            "unseen": 1,
        }
    ]

    before_events = client.get(
        f"/api/projects/{pid}/alerts/{alert['id']}/checks/{run['id']}/events",
        params={"limit": 10},
    ).json()["events"]
    read = client.post(f"/api/projects/{pid}/notifications/{item['id']}/read")
    assert read.status_code == 200, read.text
    assert read.json()["state"] == "read"
    ack = client.post(f"/api/projects/{pid}/notifications/{item['id']}/ack")
    assert ack.status_code == 200, ack.text
    assert ack.json()["state"] == "acknowledged"
    unack = client.post(f"/api/projects/{pid}/notifications/{item['id']}/unack")
    assert unack.status_code == 200, unack.text
    assert unack.json()["state"] == "read"
    after_events = client.get(
        f"/api/projects/{pid}/alerts/{alert['id']}/checks/{run['id']}/events",
        params={"limit": 10},
    ).json()["events"]
    assert after_events == before_events

    rerun_response = client.post(f"/api/projects/{pid}/alerts/{alert['id']}/check")
    assert rerun_response.status_code == 200, rerun_response.text
    assert rerun_response.json()["run"]["new_rows"] == 0
    assert client.get(f"/api/projects/{pid}/notifications").json()["total"] == 1


def test_run_candidates_emit_generic_notification_items(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Run notifications"}).json()["id"]
    run_candidate = run_transition_notification_candidate(
        project_id=pid,
        run_id=12,
        status="failed",
        action_kind="map.template",
        error="provider unavailable",
    )
    assert run_candidate is not None
    run_emit = client.post(
        f"/api/projects/{pid}/notifications/emit", json=run_candidate
    )
    assert run_emit.status_code == 200, run_emit.text

    page = client.get(f"/api/projects/{pid}/notifications").json()
    by_kind = {item["source_kind"]: item for item in page["notifications"]}
    assert by_kind["run"]["source_ref"] == {
        "project_id": pid,
        "run_id": 12,
        "status": "failed",
    }
    assert by_kind["run"]["deep_link"] == {
        "kind": "run",
        "project_id": pid,
        "run_id": 12,
    }

    summary = client.get(f"/api/projects/{pid}/notifications/summary").json()
    assert summary["by_source_kind"] == {"run": 1}

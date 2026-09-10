from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import make_client as _client


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_watch_events(client: TestClient) -> tuple[str, dict[str, Any], list[dict]]:
    pid = client.post("/api/projects", json={"name": "Notification routes"}).json()[
        "id"
    ]
    import_response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert import_response.status_code == 200, import_response.text
    watch_response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Budget watch",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget", "limit": 10},
        },
    )
    assert watch_response.status_code == 200, watch_response.text
    watch = watch_response.json()
    run_response = client.post(f"/api/projects/{pid}/watches/{watch['id']}/run")
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()["run"]
    events_response = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{run['id']}/events",
        params={"limit": 10},
    )
    assert events_response.status_code == 200, events_response.text
    return pid, {"watch": watch, "run": run}, events_response.json()["events"]


def _emit_watch_candidate(
    client: TestClient,
    pid: str,
    watch_id: int,
    run_id: int,
    event_ids: list[int],
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{pid}/notifications/emit",
        json={
            "source_kind": "watch",
            "source_ref": {"watch_id": watch_id, "run_id": run_id},
            "source_event_ids": event_ids,
            "dedupe_key": f"watch:{watch_id}:run:{run_id}:events:route-policy",
            "title": f"Budget watch found {len(event_ids)} new rows",
            "summary": f'{len(event_ids)} rows newly matched Search "budget".',
            "severity": "warning",
            "deep_link": {
                "kind": "watch_run",
                "watch_id": watch_id,
                "run_id": run_id,
                "event_ids": event_ids,
            },
            "payload": {"safe": "context"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_channel(client: TestClient, pid: str, kind: str, name: str) -> dict:
    body: dict[str, Any]
    if kind == "email":
        body = {
            "kind": "email",
            "name": name,
            "to": "alerts@example.com",
            "secret_ref": "env:RESEND_API_KEY",
        }
    else:
        body = {
            "kind": "slack",
            "name": name,
            "webhook_secret_ref": "env:SLACK_WEBHOOK_URL",
            "webhook_host": "hooks.slack.com",
            "channel_label": "#alerts",
        }
    response = client.post(f"/api/projects/{pid}/notification-channels", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_emit_plans_only_matching_external_immediate_routes(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    event_ids = [event["id"] for event in events]
    watch_id = int(refs["watch"]["id"])
    run_id = int(refs["run"]["id"])
    project = client.app.state.workspace.get(pid)

    no_route_emit = _emit_watch_candidate(client, pid, watch_id, run_id, event_ids)
    assert no_route_emit["schema_version"] == "frisket.notification_emit_result.v2"
    assert no_route_emit["planned_delivery_requests"] == []
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_requests"
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_attempts"
        ).fetchone()["n"]
        == 0
    )

    slack = _create_channel(client, pid, "slack", "Watch Slack")
    email = _create_channel(client, pid, "email", "Watch digest")
    matching_route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Budget Slack",
            "channel_id": slack["id"],
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id},
            "event_kinds": ["row_entered"],
            "severity_min": "info",
            "delivery_mode": "immediate",
        },
    )
    assert matching_route.status_code == 200, matching_route.text
    digest_route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Budget daily digest",
            "channel_id": email["id"],
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id},
            "delivery_mode": "digest",
            "digest_cadence": "daily",
        },
    )
    assert digest_route.status_code == 200, digest_route.text
    disabled_route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Disabled email",
            "channel_id": email["id"],
            "enabled": False,
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id},
            "delivery_mode": "immediate",
        },
    )
    assert disabled_route.status_code == 200, disabled_route.text
    nonmatching_route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Other watch",
            "channel_id": email["id"],
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id + 999},
            "delivery_mode": "immediate",
        },
    )
    assert nonmatching_route.status_code == 200, nonmatching_route.text

    planned = _emit_watch_candidate(client, pid, watch_id, run_id, event_ids)
    assert planned["deduped"] is True
    assert planned["planned_delivery_requests"] == [
        {
            "id": 1,
            "route_id": matching_route.json()["id"],
            "channel_id": slack["id"],
            "delivery_kind": "item",
            "status": "queued",
        }
    ]
    request_page = client.get(
        f"/api/projects/{pid}/notification-delivery-requests"
    ).json()
    assert request_page["schema_version"] == (
        "frisket.notification_delivery_requests.v1"
    )
    assert request_page["total"] == 1
    assert request_page["delivery_requests"][0]["dedupe_key"] == (
        f"notification:item:{planned['notification_id']}:"
        f"route:{matching_route.json()['id']}:channel:{slack['id']}"
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_attempts"
        ).fetchone()["n"]
        == 0
    )

    second_route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Budget email immediate",
            "channel_id": email["id"],
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id},
            "delivery_mode": "immediate",
        },
    )
    assert second_route.status_code == 200, second_route.text
    repeated = _emit_watch_candidate(client, pid, watch_id, run_id, event_ids)
    assert [req["route_id"] for req in repeated["planned_delivery_requests"]] == [
        matching_route.json()["id"],
        second_route.json()["id"],
    ]
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_requests"
        ).fetchone()["n"]
        == 2
    )


def test_route_api_rejects_user_owner_kind_without_hosted_session_and_in_app_routes(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Route validation"}).json()["id"]
    channels = client.get(f"/api/projects/{pid}/notification-channels").json()[
        "channels"
    ]
    in_app = next(channel for channel in channels if channel["kind"] == "in_app")
    email = _create_channel(client, pid, "email", "Owner email")

    user_owned = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Personal digest",
            "channel_id": email["id"],
            "owner_kind": "user",
            "owner_ref": "user:1",
        },
    )
    assert user_owned.status_code == 403
    assert (
        user_owned.json()["detail"]
        == "user notification route requires a hosted browser session"
    )

    implicit_in_app = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "In app route",
            "channel_id": in_app["id"],
            "source_kind": "watch",
        },
    )
    assert implicit_in_app.status_code == 400
    assert implicit_in_app.json()["detail"] == "in-app notification route is implicit"

    route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={"name": "Email route", "channel_id": email["id"]},
    )
    assert route.status_code == 200, route.text
    route_test = client.post(
        f"/api/projects/{pid}/notification-routes/{route.json()['id']}/test"
    )
    assert route_test.status_code == 200, route_test.text
    assert route_test.json()["delivery_kind"] == "test"
    assert route_test.json()["route_id"] == route.json()["id"]

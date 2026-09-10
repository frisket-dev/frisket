from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.server.notifications.service import emit_notification_candidate


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_watch_events(client: TestClient) -> tuple[str, dict[str, Any], list[dict]]:
    pid = client.post("/api/projects", json={"name": "Notifications"}).json()["id"]
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
            "dedupe_key": f"watch:{watch_id}:run:{run_id}:events:{','.join(map(str, sorted(event_ids)))}",
            "title": f"Budget watch found {len(event_ids)} new rows",
            "summary": f'{len(event_ids)} rows newly matched Search "budget".',
            "severity": "warning",
            "deep_link": {
                "kind": "watch_run",
                "watch_id": watch_id,
                "run_id": run_id,
                "event_ids": event_ids,
            },
            "payload": {"internal_note": "route-created"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_notification_tables_routes_and_actor_state_for_watch_events(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    event_ids = [event["id"] for event in events]

    project = client.app.state.workspace.get(pid)
    tables = {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "notification_items",
        "notification_actor_state",
        "notification_channels",
        "notification_routes",
        "notification_delivery_requests",
        "notification_delivery_attempts",
    }.issubset(tables)
    item_columns = {
        row["name"]
        for row in project.db.execute("PRAGMA table_info(notification_items)")
    }
    assert {
        "source_kind",
        "source_ref",
        "source_event_ids",
        "dedupe_key",
        "title",
        "summary",
        "severity",
        "deep_link",
    }.issubset(item_columns)

    projected_page = client.get(f"/api/projects/{pid}/notifications").json()
    assert projected_page["total"] == 1
    projected_id = projected_page["notifications"][0]["id"]

    emit = _emit_watch_candidate(
        client,
        pid,
        int(refs["watch"]["id"]),
        int(refs["run"]["id"]),
        event_ids,
    )
    assert emit["schema_version"] == "frisket.notification_emit_result.v2"
    assert emit["deduped"] is True
    assert emit["notification_id"] == projected_id
    assert emit["planned_delivery_requests"] == []

    listing = client.get(f"/api/projects/{pid}/notifications")
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert page["schema_version"] == "frisket.notifications_page.v1"
    assert page["order"] == "desc"
    assert page["total"] == 1
    item = page["notifications"][0]
    assert item["source_kind"] == "watch"
    assert item["source_ref"] == {
        "watch_id": refs["watch"]["id"],
        "run_id": refs["run"]["id"],
    }
    assert item["source_event_ids"] == event_ids
    assert item["event_count"] == len(event_ids)
    assert item["event_kinds"] == ["row_entered"]
    assert item["severity"] == "warning"
    assert item["state"] == "unseen"
    assert item["seen_at"] is None
    assert item["read_at"] is None
    assert item["acknowledged_at"] is None
    assert item["acknowledged_by"] is None

    summary = client.get(f"/api/projects/{pid}/notifications/summary")
    assert summary.status_code == 200, summary.text
    summary_body = summary.json()
    assert summary_body["schema_version"] == "frisket.notifications_summary.v1"
    assert summary_body["total"] == 1
    assert summary_body["unseen"] == 1
    assert summary_body["by_severity"] == {"warning": 1}
    assert summary_body["by_source_kind"] == {"watch": 1}
    assert summary_body["by_source_ref"] == [
        {
            "source_kind": "watch",
            "source_ref": {"watch_id": refs["watch"]["id"]},
            "unseen": 1,
        }
    ]

    seen = client.post(
        f"/api/projects/{pid}/notifications/seen",
        json={"notification_ids": [emit["notification_id"]]},
    )
    assert seen.status_code == 200, seen.text
    assert seen.json() == {"seen_count": 1}

    read = client.post(
        f"/api/projects/{pid}/notifications/{emit['notification_id']}/read"
    )
    assert read.status_code == 200, read.text
    assert read.json()["state"] == "read"

    ack = client.post(
        f"/api/projects/{pid}/notifications/{emit['notification_id']}/ack"
    )
    assert ack.status_code == 200, ack.text
    ack_state = ack.json()
    assert ack_state["state"] == "acknowledged"
    assert ack_state["acknowledged_by"] == "local:project"
    assert ack_state["seen_at"]
    assert ack_state["read_at"]
    assert ack_state["acknowledged_at"]

    deduped = _emit_watch_candidate(
        client,
        pid,
        int(refs["watch"]["id"]),
        int(refs["run"]["id"]),
        list(reversed(event_ids)),
    )
    assert deduped["deduped"] is True
    after_dedupe = client.get(f"/api/projects/{pid}/notifications").json()[
        "notifications"
    ][0]
    assert after_dedupe["state"] == "acknowledged"
    assert after_dedupe["acknowledged_at"] == ack_state["acknowledged_at"]

    unack = client.post(
        f"/api/projects/{pid}/notifications/{emit['notification_id']}/unack"
    )
    assert unack.status_code == 200, unack.text
    assert unack.json()["state"] == "read"
    assert unack.json()["acknowledged_at"] is None

    bulk_ack = client.post(
        f"/api/projects/{pid}/notifications/ack",
        json={"source_kind": "watch", "source_ref": {"watch_id": refs["watch"]["id"]}},
    )
    assert bulk_ack.status_code == 200, bulk_ack.text
    assert bulk_ack.json() == {"acknowledged_count": 1}

    stored_events = client.get(
        f"/api/projects/{pid}/watches/{refs['watch']['id']}/runs/{refs['run']['id']}/events",
        params={"limit": 10},
    ).json()["events"]
    assert [event["id"] for event in stored_events] == event_ids


def test_notification_channels_redact_secrets_without_emit_time_attempts(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    event_ids = [event["id"] for event in events]

    email = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "email",
            "name": "Ops email",
            "to": "alerts@example.com",
            "enabled": True,
            "secret_ref": "env:RESEND_API_KEY",
        },
    )
    assert email.status_code == 200, email.text
    email_body = email.json()
    assert email_body["kind"] == "email"
    assert email_body["config"]["to"] == "alerts@example.com"
    assert email_body["has_secret"] is True
    assert "secret_ref" not in email_body
    assert "RESEND_API_KEY" not in json.dumps(email_body)

    slack = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "slack",
            "name": "Ops Slack",
            "webhook_secret_ref": "env:SLACK_WEBHOOK_URL",
            "webhook_url": "https://hooks.slack.com/services/T000/B000/SECRET",
            "channel_label": "#alerts",
            "enabled": True,
        },
    )
    assert slack.status_code == 200, slack.text
    slack_body = slack.json()
    assert slack_body["kind"] == "slack"
    assert slack_body["config"] == {
        "channel_label": "#alerts",
        "webhook_host": "hooks.slack.com",
    }
    assert slack_body["has_secret"] is True
    assert "SECRET" not in json.dumps(slack_body)
    assert "hooks.slack.com/services" not in json.dumps(slack_body)

    patched = client.patch(
        f"/api/projects/{pid}/notification-channels/{slack_body['id']}",
        json={"enabled": False},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is False

    emit = _emit_watch_candidate(
        client,
        pid,
        int(refs["watch"]["id"]),
        int(refs["run"]["id"]),
        event_ids,
    )
    assert emit["planned_delivery_requests"] == []

    manual = client.post(
        f"/api/projects/{pid}/notifications/{emit['notification_id']}/deliver",
        json={"channel_id": email_body["id"]},
    )
    assert manual.status_code == 400, manual.text
    assert manual.json()["detail"] == (
        "direct notification delivery is disabled; use notification routes"
    )

    channels = client.get(f"/api/projects/{pid}/notification-channels")
    assert channels.status_code == 200, channels.text
    channels_body = channels.json()
    assert channels_body["schema_version"] == "frisket.notification_channels.v1"
    assert [channel["kind"] for channel in channels_body["channels"]] == [
        "in_app",
        "email",
        "slack",
    ]
    assert "SLACK_WEBHOOK_URL" not in json.dumps(channels_body)

    project = client.app.state.workspace.get(pid)
    stored_slack = project.db.execute(
        "SELECT secret_ref, config_json FROM notification_channels WHERE id=?",
        (slack_body["id"],),
    ).fetchone()
    assert stored_slack["secret_ref"] == "env:SLACK_WEBHOOK_URL"
    assert "SECRET" not in stored_slack["config_json"]
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_attempts"
        ).fetchone()["n"]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_requests"
        ).fetchone()["n"]
        == 0
    )


def test_identical_notification_does_not_write_but_changed_content_updates(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        pid = client.post("/api/projects", json={"name": "Duplicate notices"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(pid)
        candidate = {
            "source_kind": "plugin.acme",
            "source_ref": {"plugin_id": "acme", "run_id": "one"},
            "dedupe_key": "plugin:acme:one",
            "title": "Result ready",
            "summary": "The result is available.",
            "severity": "info",
            "payload": {},
        }
        first = emit_notification_candidate(project, candidate)
        notification_id = first["notification_id"]
        # Make timestamp churn observable without waiting for a clock tick.
        old_updated_at = "2000-01-01 00:00:00"
        project.db.execute(
            "UPDATE notification_items SET updated_at=? WHERE id=?",
            (old_updated_at, notification_id),
        )
        project.db.commit()
        before = tuple(project.db.iterdump())

        duplicate = emit_notification_candidate(project, candidate)
        assert duplicate["notification_id"] == notification_id
        assert duplicate["deduped"] is True
        assert tuple(project.db.iterdump()) == before

        emit_notification_candidate(
            project, {**candidate, "summary": "The result has changed."}
        )
        item = project.notification_item(notification_id)
        assert item["summary"] == "The result has changed."
        assert item["updated_at"] != old_updated_at


def test_notification_emit_handles_unique_dedupe_race(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Race notifications"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    original_insert = project.insert_notification_item
    calls = 0

    def racing_insert(**kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        original_insert(**kwargs)
        raise sqlite3.IntegrityError(
            "UNIQUE constraint failed: notification_items.dedupe_key"
        )

    project.insert_notification_item = racing_insert  # type: ignore[method-assign]
    result = emit_notification_candidate(
        project,
        {
            "source_kind": "plugin.acme.race",
            "source_ref": {"plugin_id": "acme", "run_id": "race-1"},
            "source_event_ids": [],
            "dedupe_key": "plugin:acme:race:1",
            "title": "Plugin race",
            "summary": "A concurrent writer won the insert.",
            "severity": "info",
            "deep_link": {"kind": "plugin_source", "plugin_id": "acme"},
            "payload": {},
        },
    )
    assert calls == 1
    assert result["deduped"] is True
    row = project.db.execute(
        "SELECT COUNT(*) AS count FROM notification_items WHERE dedupe_key=?",
        ("plugin:acme:race:1",),
    ).fetchone()
    assert int(row["count"]) == 1


def test_notification_list_rejects_malformed_source_ref_filter(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Source ref validation"}).json()[
        "id"
    ]
    response = client.get(
        f"/api/projects/{pid}/notifications",
        params={"source_ref": "not-json"},
    )
    assert response.status_code == 400
    assert "source_ref must be a JSON object" in response.text

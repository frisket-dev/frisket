from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.server.notifications.candidates import validate_notification_candidate
from frisket.server.notifications.service import (
    LOCAL_ACTOR_ID,
    emit_notification_candidate,
)
from frisket.server.app import create_app


def _project(tmp_path: Path):
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Plugin notifications"}).json()[
        "id"
    ]
    return client, pid, client.app.state.workspace.get(pid)


def test_plugin_candidates_flow_through_core_service_not_channel_direct_send(
    tmp_path: Path,
) -> None:
    _client, _pid, project = _project(tmp_path)
    channel = project.create_notification_channel(
        kind="slack",
        name="Plugin alerts",
        enabled=True,
        config={"channel_label": "#plugins", "webhook_host": "hooks.slack.com"},
        secret_ref="env:PLUGIN_SLACK_WEBHOOK",
    )
    assert channel["has_secret"] is True
    assert "PLUGIN_SLACK_WEBHOOK" not in json.dumps(channel)

    candidate = validate_notification_candidate(
        {
            "source_kind": "plugin.acme.threshold",
            "source_ref": {"plugin_id": "acme", "run_id": "plugin-run-7"},
            "source_event_ids": [],
            "dedupe_key": "plugin:acme:threshold:plugin-run-7",
            "title": "Plugin threshold crossed",
            "summary": "The plugin reported one threshold crossing.",
            "severity": "critical",
            "deep_link": {
                "kind": "plugin_source",
                "plugin_id": "acme",
                "run_id": "plugin-run-7",
            },
            "payload": {
                "safe_context": {"column": "amount"},
                "webhook_url": "https://hooks.slack.com/services/T000/B000/SECRET",
                "api_token": "secret-token",
            },
        }
    )

    result = emit_notification_candidate(project, candidate)
    assert result["schema_version"] == "frisket.notification_emit_result.v2"
    assert result["deduped"] is False
    assert result["notification_id"] > 0
    assert result["planned_delivery_requests"] == []

    row = project.db.execute(
        "SELECT source_kind, source_ref, payload FROM notification_items WHERE id=?",
        (result["notification_id"],),
    ).fetchone()
    assert row["source_kind"] == "plugin.acme.threshold"
    assert json.loads(row["source_ref"]) == {
        "plugin_id": "acme",
        "run_id": "plugin-run-7",
    }
    stored_payload = json.loads(row["payload"])
    assert stored_payload == {"safe_context": {"column": "amount"}}
    assert "SECRET" not in row["payload"]
    assert "secret-token" not in row["payload"]

    attempt_count = project.db.execute(
        "SELECT COUNT(*) AS count FROM notification_delivery_attempts "
        "WHERE notification_id=?",
        (result["notification_id"],),
    ).fetchone()
    request_count = project.db.execute(
        "SELECT COUNT(*) AS count FROM notification_delivery_requests "
        "WHERE notification_id=?",
        (result["notification_id"],),
    ).fetchone()
    assert int(attempt_count["count"]) == 0
    assert int(request_count["count"]) == 0

    state = project.notification_actor_state(result["notification_id"], LOCAL_ACTOR_ID)
    assert state["state"] == "unseen"
    assert state["acknowledged_at"] is None


def test_candidate_validation_rejects_invalid_and_watch_source_refs_are_checked(
    tmp_path: Path,
) -> None:
    _client, _pid, project = _project(tmp_path)

    with pytest.raises(ValueError, match="source_kind"):
        validate_notification_candidate(
            {
                "source_kind": "",
                "source_ref": {"id": 1},
                "dedupe_key": "bad",
                "title": "Bad",
                "summary": "Bad",
            }
        )

    with pytest.raises(ValueError, match="severity"):
        validate_notification_candidate(
            {
                "source_kind": "plugin.acme",
                "source_ref": {"id": 1},
                "dedupe_key": "bad",
                "title": "Bad",
                "summary": "Bad",
                "severity": "urgent",
            }
        )

    with pytest.raises(ValueError, match="watch notification source events"):
        emit_notification_candidate(
            project,
            {
                "source_kind": "watch",
                "source_ref": {"watch_id": 999, "run_id": 999},
                "source_event_ids": [999],
                "dedupe_key": "watch:999:run:999:events:999",
                "title": "Invalid watch event",
                "summary": "Invalid",
                "severity": "info",
                "deep_link": {"kind": "watch_run", "watch_id": 999, "run_id": 999},
                "payload": {},
            },
        )

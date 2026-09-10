"""HTTP coverage for the typed error-intake seam (Team app).

Pins the accepted adjudication: client-errors returns 202 with a REAL stored
row id (`{accepted, ok, id}`), persists the four browser-sent fields through
the existing sanitizers, and keeps its 413/429 controls; diagnostic-bundle
returns 200 `{ok, report_id, bundle}` with `ok` first and the raw sanitized
request contract untouched. The admin feed uses its canonical browser envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.team.schema import client_errors
from tests.team_setup_helpers import claim_server


SECRET = "sk-super-secret-value-1234567890"


def _team_app(tmp_path: Path) -> Any:
    from frisket.team.app import TeamConfig, create_team_app

    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Investigations Desk",
        admin_emails={"owner@example.com"},
    )
    app = create_team_app(config)
    return app


def _engine(tmp_path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{tmp_path / 'control.db'}")


BROWSER_BODY = {
    "source": "browser",
    "severity": "warning",
    "name": "TypeError",
    "message": "boom",
    "stack": f"TypeError: boom\n  at run (app.js:1)\napi_key={SECRET}",
    "route": "/p/demo/s/2",
    "context": {"trace_id": "trace-1", "api_key": SECRET, "browser": "Firefox"},
}


def test_client_errors_return_stored_id_and_persist_sanitized_fields(
    tmp_path,
) -> None:
    app = _team_app(tmp_path)
    browser = claim_server(app, workspace_name="Investigations Desk")

    response = browser.post("/api/client-errors", json=BROWSER_BODY)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert list(payload.keys()) == ["accepted", "ok", "id"]
    assert payload["accepted"] is True
    assert payload["ok"] is True
    assert isinstance(payload["id"], int) and payload["id"] > 0
    assert SECRET not in response.text

    with _engine(tmp_path).connect() as cx:
        row = (
            cx.execute(
                sa.select(client_errors).where(client_errors.c.id == payload["id"])
            )
            .mappings()
            .one()
        )
    assert row["severity"] == "warning"
    assert row["name"] == "TypeError"
    assert row["message"] == "boom"
    assert row["route"] == "/p/demo/s/2"
    assert "TypeError: boom" in row["stack"]
    assert SECRET not in row["stack"], "stack must pass the secret redactor"
    context = json.loads(row["context_json"])
    assert context["trace_id"] == "trace-1"
    assert context["browser"] == "Firefox"
    assert "api_key" not in context, "credential branches are dropped entirely"
    assert row["user_id"] is not None

    # A second report gets a strictly newer id — the id is the row identity,
    # not a constant.
    second = browser.post("/api/client-errors", json=BROWSER_BODY)
    assert second.status_code == 202
    second_id = second.json()["id"]
    assert second_id > payload["id"]

    # The unversioned admin feed is the canonical browser envelope. It exposes
    # the same sanitized facts under their one current response shape.
    feed = browser.get("/api/admin/errors")
    assert feed.status_code == 200, feed.text
    feed_payload = feed.json()
    assert set(feed_payload) == {"schema_version", "errors"}
    assert feed_payload["schema_version"] == "frisket.admin_errors.v1"
    assert [event["record_id"] for event in feed_payload["errors"]] == [
        second_id,
        payload["id"],
    ]
    latest = feed_payload["errors"][0]
    assert set(latest) == {
        "id",
        "record_id",
        "kind",
        "source",
        "severity",
        "name",
        "message",
        "stack",
        "route",
        "org_id",
        "user_id",
        "project_id",
        "sheet_id",
        "run_id",
        "job_id",
        "trace_id",
        "context",
        "bundle",
        "at",
    }
    assert latest["id"] == f"client:{second_id}"
    assert latest["kind"] == "client"
    assert latest["source"] == "browser"
    assert latest["severity"] == "warning"
    assert latest["name"] == "TypeError"
    assert latest["message"] == "boom"
    assert "TypeError: boom" in latest["stack"]
    assert latest["route"] == "/p/demo/s/2"
    assert latest["user_id"] == row["user_id"]
    assert latest["context"] == {"trace_id": "trace-1", "browser": "Firefox"}
    assert latest["bundle"] is None
    assert SECRET not in feed.text


def test_anonymous_client_errors_still_accepted_with_id(tmp_path) -> None:
    app = _team_app(tmp_path)
    claim_server(app, workspace_name="Investigations Desk")
    anonymous = TestClient(app)

    response = anonymous.post(
        "/api/client-errors", json={"message": "anon boom", "name": "Error"}
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["id"], int) and payload["id"] > 0
    with _engine(tmp_path).connect() as cx:
        row = (
            cx.execute(
                sa.select(client_errors).where(client_errors.c.id == payload["id"])
            )
            .mappings()
            .one()
        )
    assert row["user_id"] is None
    assert row["severity"] == "error", "default severity preserved"
    assert row["context_json"] == "{}"


def test_client_error_rate_and_size_limits_unchanged(tmp_path) -> None:
    app = _team_app(tmp_path)
    claim_server(app, workspace_name="Investigations Desk")
    anonymous = TestClient(app)

    for _ in range(20):
        accepted = anonymous.post("/api/client-errors", json={"message": "x"})
        assert accepted.status_code == 202
    limited = anonymous.post("/api/client-errors", json={"message": "x"})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "client error report rate limit exceeded"

    oversize = anonymous.post(
        "/api/client-errors",
        content=json.dumps({"message": "y" * 70_000}),
        headers={"Content-Type": "application/json"},
    )
    assert oversize.status_code == 413
    assert oversize.json()["detail"] == "client error report is too large"


def test_diagnostic_bundle_gains_ok_and_echoes_recent_ids(tmp_path) -> None:
    app = _team_app(tmp_path)
    browser = claim_server(app, workspace_name="Investigations Desk")

    reported = browser.post("/api/client-errors", json=BROWSER_BODY)
    assert reported.status_code == 202
    error_id = reported.json()["id"]

    description = "The grid froze after I sorted the date column."
    diagnostic = browser.post(
        "/api/diagnostic-bundle",
        json={
            "message": description,
            "route": "/p/demo",
            "include_raw_values": True,
            "context": {"trace_id": "trace-1"},
            "recent_client_error_ids": [error_id],
        },
    )
    assert diagnostic.status_code == 200, diagnostic.text
    payload = diagnostic.json()
    assert list(payload.keys()) == ["ok", "report_id", "bundle"]
    assert payload["ok"] is True
    assert isinstance(payload["report_id"], int) and payload["report_id"] > 0
    bundle = payload["bundle"]
    assert bundle["include_raw_values"] is False, "honest false flag preserved"
    assert bundle["message"] == description
    assert bundle["recent_client_error_ids"] == [error_id]
    with _engine(tmp_path).connect() as cx:
        stored = cx.execute(
            sa.select(client_errors.c.message).where(
                client_errors.c.id == payload["report_id"]
            )
        ).scalar_one()
    assert stored == description

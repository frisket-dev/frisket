from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


def _project(client: TestClient, name: str = "Source health") -> tuple[str, Project]:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    return pid, client.app.state.workspace.get(pid)


def _insert_receipt(project: Project, receipt_id: str, body: dict[str, Any]) -> None:
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, action_id, "
        "idempotency_key, params_hash, status, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt_id,
            "source.poll",
            f"action_{receipt_id}",
            f"source_poll@sha256:{receipt_id}",
            f"params_{receipt_id}",
            str(body.get("status") or "completed"),
            json.dumps(body, sort_keys=True),
        ),
    )
    project.db.commit()


def test_source_health_route_summarizes_runs_jobs_costs_and_redacts(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, project = _project(client)
    source_id = SourceStore(project).add_source(
        "Sensitive feed",
        kind="rss",
        url="https://feeds.example/policy.xml?token=SECRET_URL_TOKEN",
        config={
            "api_key": "SECRET_CONFIG_KEY",
            "headers": {"authorization": "Bearer SECRET_CONFIG_BEARER"},
        },
        schedule="@hourly",
    )
    project.db.execute(
        "UPDATE sources SET cursor=? WHERE id=?",
        (json.dumps({"cursor": "SECRET_SOURCE_CURSOR"}), source_id),
    )
    project.db.commit()

    _insert_receipt(
        project,
        "receipt_ok",
        {
            "status": "completed",
            "provider_body": "SECRET_PROVIDER_RESPONSE",
            "cursor": "SECRET_RECEIPT_CURSOR",
        },
    )
    success_run = SourceStore(project).start_source_run(
        source_id,
        cursor_before=json.dumps({"cursor": "SECRET_RUN_CURSOR_BEFORE"}),
    )
    SourceStore(project).finish_source_run(
        success_run,
        status="ok",
        receipt_id="receipt_ok",
        new_rows=4,
        skipped_rows=3,
        changed_rows=2,
        revisions=1,
        cursor_after=json.dumps({"cursor": "SECRET_RUN_CURSOR_AFTER"}),
        duration_ms=345,
        warning_count=1,
        cost_micro=2300,
        summary={"provider_body": "SECRET_SUMMARY_BODY"},
    )
    SourceStore(project).record_source_run(
        source_id,
        status="error",
        error="provider failed with Authorization: Bearer SECRET_ERROR_BEARER",
    )
    latest_failure_id = SourceStore(project).record_source_run(
        source_id,
        status="error",
        error="token=SECRET_ERROR_TOKEN api_key=SECRET_ERROR_KEY",
    )

    ws = client.app.state.workspace
    ws.queue.enqueue(
        "source.poll",
        {
            "project_id": pid,
            "source_id": source_id,
            "token": "SECRET_JOB_PAYLOAD_TOKEN",
            "raw_provider_body": "SECRET_JOB_PROVIDER_BODY",
        },
        max_attempts=2,
    )

    response = client.get(f"/api/projects/{pid}/sources/{source_id}/health")
    assert response.status_code == 200, response.text
    body = response.json()
    serialized = json.dumps(body, sort_keys=True)

    assert body["schema_version"] == "frisket.source_health.v1"
    assert body["source"] == {
        "id": source_id,
        "name": "Sensitive feed",
        "kind": "rss",
        "url": "https://feeds.example/policy.xml",
        "enabled": True,
        "schedule": "@hourly",
        "sheet_id": None,
        "created_at": body["source"]["created_at"],
        "redactions": ["config", "cursor", "url_query"],
    }
    assert body["summary"]["status"] == "failing"
    assert body["summary"]["last_success_at"] is not None
    assert body["summary"]["last_failure_at"] is not None
    assert body["summary"]["consecutive_failures"] == 2
    assert body["summary"]["new_rows_total"] == 5
    assert body["summary"]["new_rows_recent"] == 4
    assert body["summary"]["changed_rows_recent"] == 2
    assert body["summary"]["skipped_rows_recent"] == 3
    assert body["summary"]["revisions_recent"] == 1
    assert body["summary"]["last_cursor_summary"] == "stored"

    assert body["runs_page"]["schema_version"] == "frisket.source_runs_page.v1"
    assert body["runs_page"]["limit"] == 20
    assert body["runs_page"]["total"] == 3
    assert [run["id"] for run in body["runs"]][0] == latest_failure_id
    assert body["runs"][0]["error_summary"] == ("token=[redacted] api_key=[redacted]")
    assert body["runs"][0]["cursor_before_present"] is False
    assert body["runs"][0]["cursor_after_present"] is False
    assert body["runs"][2]["cursor_before_present"] is True
    assert body["runs"][2]["cursor_after_present"] is True

    assert body["costs"] == {
        "recent_actual_micro": 2300,
        "recent_estimated_micro": 0,
        "basis": "source_runs.cost_micro for loaded runs; downstream costs unknown-safe",
    }
    assert body["downstream_jobs"][0]["kind"] == "source.poll"
    assert body["downstream_jobs"][0]["status"] == "queued"
    assert body["downstream_jobs"][0]["refs"] == {"source_id": source_id}
    assert "payload" not in body["downstream_jobs"][0]
    assert "result" not in body["downstream_jobs"][0]
    assert any(w["code"] == "sparse_source_run_metadata" for w in body["warnings"])

    assert "config" not in body["source"]
    assert "SECRET_" not in serialized
    assert "provider_body" not in serialized


def test_source_health_paginates_recent_runs_defaulting_to_twenty(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, project = _project(client, "Source health paging")
    source_id = SourceStore(project).add_source(
        "Long feed",
        kind="rss",
        url="https://feeds.example/long.xml",
        schedule="@hourly",
    )
    run_ids: list[int] = []
    for index in range(23):
        run_ids.append(
            SourceStore(project).record_source_run(
                source_id,
                status="ok",
                new_rows=index % 5,
            )
        )

    first = client.get(f"/api/projects/{pid}/sources/{source_id}/health")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["runs_page"] == {
        "schema_version": "frisket.source_runs_page.v1",
        "order": "desc",
        "offset": 0,
        "limit": 20,
        "total": 23,
        "has_more": True,
        "next_offset": 20,
    }
    assert [run["id"] for run in body["runs"]] == list(reversed(run_ids[-20:]))

    tail = client.get(
        f"/api/projects/{pid}/sources/{source_id}/health",
        params={"runs_offset": 20, "runs_limit": 20},
    )
    assert tail.status_code == 200, tail.text
    tail_body = tail.json()
    assert tail_body["runs_page"] == {
        "schema_version": "frisket.source_runs_page.v1",
        "order": "desc",
        "offset": 20,
        "limit": 20,
        "total": 23,
        "has_more": False,
        "next_offset": None,
    }
    assert [run["id"] for run in tail_body["runs"]] == list(reversed(run_ids[:3]))


def test_source_health_statuses_cover_disabled_never_run_stale_and_missing(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, project = _project(client, "Source health statuses")
    disabled = SourceStore(project).add_source(
        "Paused feed",
        kind="rss",
        url="https://feeds.example/paused.xml",
        schedule="@hourly",
        enabled=False,
    )
    never_run = SourceStore(project).add_source(
        "New feed",
        kind="rss",
        url="https://feeds.example/new.xml",
        schedule="@hourly",
    )
    stale = SourceStore(project).add_source(
        "Old feed",
        kind="rss",
        url="https://feeds.example/old.xml",
        schedule="@hourly",
    )
    SourceStore(project).record_source_run(stale, status="ok", new_rows=1)
    old = "2000-01-01T00:00:00+00:00"
    project.db.execute(
        "UPDATE source_runs SET started_at=?, finished_at=? WHERE source_id=?",
        (old, old, stale),
    )
    project.db.execute(
        "UPDATE sources SET last_checked_at=?, last_status='ok' WHERE id=?",
        (old, stale),
    )
    project.db.commit()

    for source_id, expected in (
        (disabled, "disabled"),
        (never_run, "never_run"),
        (stale, "stale"),
    ):
        response = client.get(f"/api/projects/{pid}/sources/{source_id}/health")
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["status"] == expected

    missing = client.get(f"/api/projects/{pid}/sources/9999/health")
    assert missing.status_code == 404

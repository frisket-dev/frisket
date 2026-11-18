"""Run outcomes reach the notification feed and run-filtered routes once."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import frisket.notifications.run_outcomes as run_outcomes
import frisket.sdk.ops.transcribe_engines as transcribe_engines
from frisket.contracts.action import ActionResult
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.receipts import ReceiptStore
from frisket.notifications.run_outcomes import emit_run_outcome_notification
from frisket.ops.base import RecipeInvocationHalt
from frisket.search_index import drain_index
from frisket.server.app import create_app
from http_test_helpers import (
    drain_queue,
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)
from tests.alert_helpers import post_alert, seed_empty_alert_baseline
from tests.deterministic_time import controlled_time

CSV = (
    "title,body\n"
    "Budget hearing,The council discussed budget oversight.\n"
    "Bridge repair,The bridge repair vote was tabled.\n"
)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws"))


def _project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Run notices"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("notes.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    drain_queue(client, worker_id="run-notices-setup")
    return pid, int(imported.json()["sheet_id"])


def _queue_run(client: TestClient, pid: str, sheet_id: int) -> dict[str, Any]:
    spec = queued_python_run_spec(sheet_id, "title", "copied")
    response = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] is not None
    return body


def _run_notifications(client: TestClient, pid: str) -> list[dict[str, Any]]:
    page = client.get(
        f"/api/projects/{pid}/notifications", params={"state": "all", "limit": 100}
    )
    assert page.status_code == 200, page.text
    return [
        item for item in page.json()["notifications"] if item["source_kind"] == "run"
    ]


def _route(
    client: TestClient,
    pid: str,
    *,
    severity_min: str,
    source_kind: str | None = "run",
) -> int:
    channel = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "slack",
            "name": f"{source_kind or 'all'} {severity_min}",
            "webhook_secret_ref": "env:FRISKET_TEST_UNSET_SLACK_WEBHOOK",
            "channel_label": "#runs",
            "enabled": True,
        },
    )
    assert channel.status_code == 200, channel.text
    route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": f"{source_kind or 'all'} {severity_min}",
            "channel_id": channel.json()["id"],
            "source_kind": source_kind,
            "severity_min": severity_min,
        },
    )
    assert route.status_code == 200, route.text
    return int(route.json()["id"])


def _routed_notification_ids(client: TestClient, pid: str, route_id: int) -> set[int]:
    page = client.get(
        f"/api/projects/{pid}/notification-delivery-requests", params={"limit": 100}
    )
    assert page.status_code == 200, page.text
    return {
        int(request["notification_id"])
        for request in page.json()["delivery_requests"]
        if request["route_id"] == route_id and request["notification_id"] is not None
    }


def _redeliver_project_run(client: TestClient, job_id: int) -> dict[str, Any]:
    ws = client.app.state.workspace
    job = ws.queue.get(job_id)
    assert job is not None
    return ws.registry.get("project.run")(
        {**deepcopy(job.payload), "job_id": job.id},
        JobHandlerContext.from_claimed_job(trusted_organization_id=None),
    )


def test_completed_queued_run_notifies_once_and_reaches_an_info_run_route(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    info_route = _route(client, pid, severity_min="info")
    warning_route = _route(client, pid, severity_min="warning")
    queued = _queue_run(client, pid, sheet_id)
    run_id = int(queued["run_id"])
    assert _run_notifications(client, pid) == []

    drain_queue(client)

    [item] = _run_notifications(client, pid)
    assert item["title"] == f"Run completed #{run_id}"
    assert item["severity"] == "info"
    assert item["state"] == "unseen"
    assert item["source_ref"] == {
        "project_id": pid,
        "run_id": run_id,
        "status": "completed",
    }
    assert item["deep_link"] == {"kind": "run", "project_id": pid, "run_id": run_id}
    assert _routed_notification_ids(client, pid, info_route) == {item["id"]}
    # Completions stay out of a route that asked only for problems.
    assert _routed_notification_ids(client, pid, warning_route) == set()

    # A redelivered job for the terminal run, a status poll, and a repeated
    # projection all observe the same committed outcome.
    assert _redeliver_project_run(client, int(queued["job_id"]))["skipped"] is True
    status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert status.status_code == 200, status.text
    emit_run_outcome_notification(client.app.state.workspace.get(pid), run_id)
    assert [notice["id"] for notice in _run_notifications(client, pid)] == [item["id"]]
    assert _routed_notification_ids(client, pid, info_route) == {item["id"]}


def test_redelivered_job_notifies_a_terminal_run_whose_emit_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    _route(client, pid, severity_min="info")
    queued = _queue_run(client, pid, sheet_id)

    def lost(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(run_outcomes, "emit_notification_candidate", lost)
    drain_queue(client)

    project = client.app.state.workspace.get(pid)
    run = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (queued["run_id"],)
    ).fetchone()
    assert run["status"] == "completed"
    assert ReceiptStore(project).status_by_id(queued["receipt_id"]) == "completed"
    assert client.app.state.workspace.queue.get(queued["job_id"]).status == "done"
    assert _run_notifications(client, pid) == []

    monkeypatch.undo()
    _redeliver_project_run(client, int(queued["job_id"]))
    [item] = _run_notifications(client, pid)
    assert item["source_ref"]["status"] == "completed"


def test_failed_queue_job_notifies_a_warning_once(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    warning_route = _route(client, pid, severity_min="warning")
    all_route = _route(client, pid, severity_min="info", source_kind=None)
    queued = _queue_run(client, pid, sheet_id)
    run_id = int(queued["run_id"])
    queue = client.app.state.workspace.queue
    job = queue.claim("failing-worker", lease_seconds=600)
    assert job is not None and job.id == queued["job_id"]
    assert queue.fail(job.id, "failing-worker", "worker crashed", retry=False)

    for _ in range(2):
        status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
        assert status.status_code == 200, status.text
        assert status.json()["run"]["status"] == "failed"

    [item] = _run_notifications(client, pid)
    assert item["title"] == f"Run failed #{run_id}"
    assert item["severity"] == "warning"
    assert item["source_ref"]["status"] == "failed"
    assert "worker crashed" in item["summary"]
    assert _routed_notification_ids(client, pid, warning_route) == {item["id"]}
    # Problems reach routes for every source kind as well.
    assert _routed_notification_ids(client, pid, all_route) == {item["id"]}


def test_cancelled_run_notifies_a_run_route_once(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    route_id = _route(client, pid, severity_min="info")
    queued = _queue_run(client, pid, sheet_id)
    run_id = int(queued["run_id"])

    for _ in range(2):
        cancelled = client.post(f"/api/projects/{pid}/actions/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"

    [item] = _run_notifications(client, pid)
    assert item["title"] == f"Run cancelled #{run_id}"
    assert item["severity"] == "info"
    assert item["source_ref"]["status"] == "cancelled"
    assert _routed_notification_ids(client, pid, route_id) == {item["id"]}


def test_emit_failure_leaves_the_cancelled_run_and_receipt_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    queued = _queue_run(client, pid, sheet_id)

    def broken(target: Any, _candidate: Any, **_kwargs: Any) -> None:
        # A write that fails after opening its transaction.
        target.db.execute("BEGIN IMMEDIATE")
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(run_outcomes, "emit_notification_candidate", broken)
    cancelled = client.post(
        f"/api/projects/{pid}/actions/runs/{queued['run_id']}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    project = client.app.state.workspace.get(pid)
    run = project.db.execute(
        "SELECT status, finished_at FROM runs WHERE id=?", (queued["run_id"],)
    ).fetchone()
    assert run["status"] == "cancelled" and run["finished_at"] is not None
    assert ReceiptStore(project).status_by_id(queued["receipt_id"]) == "cancelled"
    assert not project.db.in_transaction
    assert _run_notifications(client, pid) == []


def test_open_transaction_defers_the_run_notification(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    queued = _queue_run(client, pid, sheet_id)
    run_id = int(queued["run_id"])
    project = client.app.state.workspace.get(pid)

    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute(
        "UPDATE runs SET status='failed', finished_at=datetime('now') WHERE id=?",
        (run_id,),
    )
    emit_run_outcome_notification(project, run_id)
    # The caller's transaction is still open and still its own to roll back.
    assert project.db.in_transaction
    project.db.rollback()

    assert (
        project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[
            "status"
        ]
        == "running"
    )
    assert _run_notifications(client, pid) == []


def test_preview_records_no_run_notification(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    started = client.post(
        f"/api/projects/{pid}/actions/v1/preview",
        json={
            "action_id": "map.python",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "output_names": {"n": "title_length"},
            "params": {
                "input_columns": ["title"],
                "code": "result = {'n': len(row['title'])}",
                "return_schema": {
                    "type": "object",
                    "required": ["n"],
                    "properties": {"n": {"type": "integer"}},
                },
                "output_routes": [
                    {
                        "name": "n",
                        "path": "$.n",
                        "target": {"kind": "column", "type": "integer"},
                    }
                ],
            },
            "idempotency_key": "run-notices@preview",
        },
    )
    assert started.status_code == 202, started.text
    preview_id = started.json()["preview_id"]
    body: dict[str, Any] = {}

    def finished() -> bool:
        nonlocal body
        polled = client.get(f"/api/projects/{pid}/actions/v1/preview/{preview_id}")
        assert polled.status_code == 200, polled.text
        body = polled.json()
        return body["status"] in {"done", "error", "cancelled"}

    with controlled_time(timeout=8) as clock:
        clock.wait_until(finished, message="preview did not finish")
    assert body["status"] == "done", body
    drain_queue(client)

    assert _run_notifications(client, pid) == []


def test_alert_check_notifies_as_an_alert_not_a_run(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _project(client)
    drain_index(client.app.state.workspace.get(pid))
    alert = post_alert(
        client,
        f"/api/projects/{pid}/watches",
        json={
            "name": "Budget alert",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget", "limit": 10},
        },
    )
    assert alert.status_code == 200, alert.text
    alert_id = alert.json()["id"]
    seed_empty_alert_baseline(client.app.state.workspace.get(pid), alert_id)
    evaluated = client.post(f"/api/projects/{pid}/watches/{alert_id}/run")
    assert evaluated.status_code == 200, evaluated.text
    drain_queue(client)

    page = client.get(f"/api/projects/{pid}/notifications", params={"state": "all"})
    assert page.status_code == 200, page.text
    kinds = [item["source_kind"] for item in page.json()["notifications"]]
    assert kinds == ["watch"]


def _media_project(client: TestClient) -> tuple[str, int, list[int]]:
    pid = client.post("/api/projects", json={"name": "Halted run"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("Episodes")
    columns = {"media": project.add_column(sheet_id, "media", type="audio")}
    rows = []
    for index in range(2):
        blob = project.add_blob(
            f"RIFF0000WAVEfmt halted-{index}".encode(),
            filename=f"halted-{index}.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        rows.append(
            {"media": media_cell(blob, mime="audio/wav", filename=f"{index}.wav")}
        )
    return pid, sheet_id, project.add_rows(sheet_id, rows, columns)


def test_halted_run_needs_user_action_and_its_backfill_notifies_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    pid, sheet_id, row_ids = _media_project(client)
    _route(client, pid, severity_min="info")
    calls = 0
    halt = True

    async def halt_second_row(
        self: Any, path: str, spec: dict[str, Any], *, should_cancel: Any = None
    ) -> dict[str, Any]:
        nonlocal calls
        del self, path, spec, should_cancel
        calls += 1
        if halt and calls == 2:
            raise RecipeInvocationHalt(
                "local_session_failed", "session exited during queued work"
            )
        return {
            "text": f"transcript {calls}",
            "segments": [{"start": 0.0, "end": 0.5, "text": "transcript"}],
            "language": "en",
            "duration": 0.5,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", halt_second_row
    )
    monkeypatch.setattr(
        transcribe_engines, "transcription_max_row_concurrency", lambda spec: 1
    )
    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "media.transcribe",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
            "params": {"source": "media", "engine": "faster_whisper"},
            "output_names": {
                "text": "transcript",
                "segments": "transcript_segments",
                "detected_language": "transcript_language",
            },
            "idempotency_key": "media-transcribe@sha256:run-notice-halt",
        },
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued" and queued.run_id is not None
    drain_queue(client)

    [halted] = _run_notifications(client, pid)
    assert halted["title"] == f"Run needs user action #{queued.run_id}"
    assert halted["severity"] == "warning"
    assert halted["source_ref"] == {
        "project_id": pid,
        "run_id": queued.run_id,
        "status": "needs_user_action",
        "reason": "local_session_failed",
    }

    halt = False
    backfill = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "transcript"},
            "idempotency_key": "run-backfill@sha256:run-notice-recover",
        },
    )
    assert backfill.status_code == 200, backfill.text
    recovered = ActionResult.model_validate(backfill.json())
    assert recovered.status == "completed"
    assert recovered.run_id not in {None, queued.run_id}

    notices = {
        item["source_ref"]["run_id"]: item for item in _run_notifications(client, pid)
    }
    assert set(notices) == {queued.run_id, recovered.run_id}
    assert notices[queued.run_id]["id"] == halted["id"]
    assert notices[recovered.run_id]["title"] == f"Run completed #{recovered.run_id}"


def test_action_job_that_opens_its_run_in_the_worker_notifies_that_run(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Formula notices"}).json()["id"]
    _route(client, pid, severity_min="info")
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("Numbers")
    amount = project.add_column(sheet, "amount", type="number")
    project.add_rows(sheet, [{"amount": 3}], {"amount": amount})
    launched = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "map.formula",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"formula": {"text": "={{amount}} * 2"}},
            "output_names": {"result": "doubled"},
            "idempotency_key": "formula-run-notice",
        },
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["run_id"] is None
    drain_queue(client)

    receipt = ReceiptStore(project).parsed_by_id(launched.json()["receipt_id"])
    assert receipt is not None and receipt.run_id is not None
    [item] = _run_notifications(client, pid)
    assert item["source_ref"] == {
        "project_id": pid,
        "run_id": receipt.run_id,
        "status": "completed",
    }


def test_clean_completion_stores_nothing_without_a_route_for_runs(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    every_source = _route(client, pid, severity_min="info", source_kind=None)
    problems_only = _route(client, pid, severity_min="warning")
    queued = _queue_run(client, pid, sheet_id)

    drain_queue(client)

    project = client.app.state.workspace.get(pid)
    run = project.db.execute(
        "SELECT status, failed_rows FROM runs WHERE id=?", (queued["run_id"],)
    ).fetchone()
    assert (run["status"], run["failed_rows"]) == ("completed", 0)
    assert _run_notifications(client, pid) == []
    assert _routed_notification_ids(client, pid, every_source) == set()
    assert _routed_notification_ids(client, pid, problems_only) == set()


def test_a_route_for_runs_added_later_does_not_backfill_a_completion(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    queued = _queue_run(client, pid, sheet_id)
    drain_queue(client)
    assert _run_notifications(client, pid) == []

    project = client.app.state.workspace.get(pid)
    # The route is created strictly after the run finished.
    project.db.execute(
        "UPDATE runs SET finished_at=datetime('now', '-1 minute') WHERE id=?",
        (queued["run_id"],),
    )
    project.db.commit()
    route_id = _route(client, pid, severity_min="info")
    _redeliver_project_run(client, int(queued["job_id"]))
    emit_run_outcome_notification(project, int(queued["run_id"]))

    assert _run_notifications(client, pid) == []
    assert _routed_notification_ids(client, pid, route_id) == set()


def test_completion_with_failed_rows_is_a_warning_for_every_route(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _project(client)
    every_source = _route(client, pid, severity_min="warning", source_kind=None)
    spec = queued_python_run_spec(sheet_id, "title", "checked")
    spec["code"] = (
        "if row['title'] == 'Bridge repair':\n"
        "    raise ValueError('unreadable title')\n"
        "result = row['title']"
    )
    response = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    assert response.status_code == 200, response.text
    run_id = int(response.json()["run_id"])

    drain_queue(client)

    project = client.app.state.workspace.get(pid)
    run = project.db.execute(
        "SELECT status, failed_rows FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert (run["status"], run["failed_rows"]) == ("completed", 1)
    [item] = _run_notifications(client, pid)
    assert item["title"] == f"Run completed with 1 failed row #{run_id}"
    assert item["summary"] == f"Run {run_id} completed with 1 failed row."
    assert item["severity"] == "warning"
    assert _routed_notification_ids(client, pid, every_source) == {item["id"]}

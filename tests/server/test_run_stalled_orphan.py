from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from frisket.engine.jobs.queue import jobs_table
from frisket.server.app import create_app
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)

CSV = "first,last\nAda,Lovelace\nGrace,Hopper\n"


def _client(tmp_path, *, grace_seconds: float = 3600.0) -> TestClient:
    return TestClient(
        create_app(tmp_path / "ws", run_status_grace_seconds=grace_seconds)
    )


def _seed_queued_action_run(client: TestClient) -> tuple[str, int, int, int]:
    pid = client.post("/api/projects", json={"name": "Queue Status"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    spec = queued_python_run_spec(sheet_id, "first", "display")
    run = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=True)
    assert run.status_code == 200, run.text
    body = run.json()
    run_id = body["run_id"]
    job_id = body["job_id"]
    assert job_id is not None
    return pid, sheet_id, run_id, job_id


def _age_run(client: TestClient, pid: str, run_id: int) -> None:
    project = client.app.state.workspace.get(pid)
    project.db.execute(
        "UPDATE runs SET started_at=datetime('now', '-10 minutes') WHERE id=?",
        (run_id,),
    )
    project.db.commit()


def _delete_queue_job(client: TestClient, job_id: int) -> None:
    queue = client.app.state.workspace.queue
    with queue.engine.begin() as cx:
        cx.execute(jobs_table.delete().where(jobs_table.c.id == job_id))


def _expire_queue_job(client: TestClient, job_id: int) -> None:
    queue = client.app.state.workspace.queue
    past = datetime.now(UTC) - timedelta(seconds=5)
    with queue.engine.begin() as cx:
        cx.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(lease_expires_at=past)
        )


def _action_run_public_status(client: TestClient, pid: str, run_id: int) -> dict:
    response = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()["run"]["public_status"]


def test_run_status_distinguishes_queued_and_running_queue_jobs(tmp_path):
    client = _client(tmp_path)
    pid, _, run_id, job_id = _seed_queued_action_run(client)

    queued = _action_run_public_status(client, pid, run_id)
    assert queued["status"] == "queued"
    assert queued["queue"]["job_id"] == job_id
    assert queued["queue"]["status"] == "queued"

    job = client.app.state.workspace.queue.claim("status-worker", lease_seconds=600)
    assert job is not None and job.id == job_id

    running = _action_run_public_status(client, pid, run_id)
    assert running["status"] == "running"
    assert running["queue"]["status"] == "running"
    assert running["queue"]["locked_by"] == "status-worker"
    assert running["queue"]["lease_expired"] is False


def test_expired_worker_lease_surfaces_stalled_run_status(tmp_path):
    client = _client(tmp_path)
    pid, _, run_id, job_id = _seed_queued_action_run(client)

    job = client.app.state.workspace.queue.claim("dead-worker", lease_seconds=600)
    assert job is not None and job.id == job_id
    _expire_queue_job(client, job_id)

    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "stalled"
    assert status["stalled_reason"] == "lease_expired"
    assert status["queue"]["status"] == "running"
    assert status["queue"]["lease_expired"] is True


def test_queued_job_without_worker_after_grace_surfaces_stalled(tmp_path):
    client = _client(tmp_path, grace_seconds=1)
    pid, _, run_id, job_id = _seed_queued_action_run(client)
    _age_run(client, pid, run_id)

    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "stalled"
    assert status["stalled_reason"] == "queued_after_grace"
    assert status["queue"]["job_id"] == job_id
    assert status["queue"]["status"] == "queued"


def test_running_job_with_live_lease_stays_running_without_row_progress(tmp_path):
    client = _client(tmp_path, grace_seconds=1)
    pid, _, run_id, job_id = _seed_queued_action_run(client)
    _age_run(client, pid, run_id)
    job = client.app.state.workspace.queue.claim("slow-worker", lease_seconds=600)
    assert job is not None and job.id == job_id

    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "running"
    assert "stalled_reason" not in status
    assert status["queue"]["status"] == "running"
    assert status["queue"]["lease_expired"] is False


def test_failed_job_with_live_writer_surfaces_reconciliation_stall(tmp_path):
    client = _client(tmp_path)
    pid, _, run_id, job_id = _seed_queued_action_run(client)
    queue = client.app.state.workspace.queue
    job = queue.claim("failed-worker", lease_seconds=600)
    assert job is not None and job.id == job_id

    project = client.app.state.workspace.get(pid)
    attempt_id = "attempt_failed_worker"
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 0, 'dispatching', 'failed-worker-fixture', '[]', "
        "datetime('now'))",
        (attempt_id, run_id),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run_id),
    )
    project.db.commit()
    assert queue.fail(
        job_id,
        "failed-worker",
        "worker_exception: operation failed",
        retry=False,
    )

    def durable_state():
        return (
            tuple(
                project.db.execute(
                    "SELECT status, current_attempt_id, finished_at "
                    "FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT * FROM execution_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, body FROM receipts WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            ),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT * FROM output_column_claims WHERE run_id=? ORDER BY id",
                    (run_id,),
                ).fetchall()
            ),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT * FROM run_output_generations "
                    "WHERE run_id=? ORDER BY column_id",
                    (run_id,),
                ).fetchall()
            ),
        )

    before = durable_state()

    status = _action_run_public_status(client, pid, run_id)

    assert status["status"] == "stalled"
    assert status["stalled_reason"] == "effect_reconciliation_required"
    assert status["queue"]["status"] == "failed"
    assert status["queue"]["error"] == "worker_exception: operation failed"
    assert durable_state() == before

    repeated = _action_run_public_status(client, pid, run_id)
    assert repeated["status"] == "stalled"
    assert repeated["stalled_reason"] == "effect_reconciliation_required"
    assert repeated["queue"] == status["queue"]
    assert durable_state() == before


def test_typed_halt_reason_surfaces_on_a_resumable_cancelled_run(tmp_path):
    # A recipe/session halt (e.g. Parakeet's local_artifact_unavailable)
    # finalizes as a resumable 'cancelled'; the code+reason are stored in run
    # params. The status payload must surface them so the run reads as
    # "cancelled BECAUSE the model was unavailable" rather than a bare cancel.
    import json

    client = _client(tmp_path)
    pid, _, run_id, _job_id = _seed_queued_action_run(client)
    project = client.app.state.workspace.get(pid)
    row = project.db.execute("SELECT params FROM runs WHERE id=?", (run_id,)).fetchone()
    params = json.loads(row["params"] or "{}")
    params["halted_code"] = "local_artifact_unavailable"
    params["halted_reason"] = "the pinned local Parakeet artifacts are unavailable"
    project.db.execute(
        "UPDATE runs SET status='cancelled', params=? WHERE id=?",
        (json.dumps(params), run_id),
    )
    project.db.commit()

    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "cancelled"
    assert status["halted_code"] == "local_artifact_unavailable"
    assert "Parakeet artifacts are unavailable" in status["halted_reason"]


def test_missing_queue_job_after_grace_surfaces_orphaned(tmp_path):
    client = _client(tmp_path, grace_seconds=1)
    pid, _, run_id, job_id = _seed_queued_action_run(client)
    _age_run(client, pid, run_id)
    _delete_queue_job(client, job_id)

    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "orphaned"
    assert status["stalled_reason"] == "queue_job_missing"
    assert status["queue"] == {"job_id": job_id, "status": "missing"}


def test_done_queue_job_with_still_running_run_after_grace_is_orphaned(tmp_path):
    client = _client(tmp_path, grace_seconds=1)
    pid, _, run_id, job_id = _seed_queued_action_run(client)
    _age_run(client, pid, run_id)
    job = client.app.state.workspace.queue.claim("done-worker", lease_seconds=600)
    assert job is not None and job.id == job_id
    assert client.app.state.workspace.queue.complete(job_id, "done-worker")
    project = client.app.state.workspace.get(pid)
    project.db.execute(
        "UPDATE runs SET completed_rows=1 WHERE id=? AND status='running'",
        (run_id,),
    )
    project.db.commit()

    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "orphaned"
    assert status["stalled_reason"] == "job_done_but_run_still_running"
    assert status["queue"]["status"] == "done"


def test_cancel_cancels_queued_job_as_well_as_run_row(tmp_path):
    client = _client(tmp_path)
    pid, _, run_id, job_id = _seed_queued_action_run(client)

    cancelled = client.post(f"/api/projects/{pid}/actions/runs/{run_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["queue_job_id"] == job_id
    assert cancelled.json()["queue_cancelled"] is True

    job = client.app.state.workspace.queue.get(job_id)
    assert job is not None and job.status == "cancelled"
    status = _action_run_public_status(client, pid, run_id)
    assert status["status"] == "cancelled"

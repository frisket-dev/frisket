from fastapi.testclient import TestClient

from frisket.engine.store.sources import SourceStore
from frisket.server.app import create_app


def test_manual_feed_http_returns_destination_and_visible_cancellable_job(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Feed"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sid = SourceStore(project).add_source(
        "My feed", kind="rss", url="https://example.test/feed"
    )
    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            "action_id": "source.poll",
            "scope": {"kind": "project"},
            "params": {"source": sid},
            "idempotency_key": "import-feed",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    jid = body["job_id"]
    assert body["outputs"][0]["sheet_id"] is not None
    jobs = client.get(f"/api/projects/{pid}/actions/jobs").json()["jobs"]
    assert any(job["job_id"] == jid for job in jobs)
    queue = client.app.state.workspace.queue
    claimed = queue.claim("feed-worker")
    assert claimed.id == jid
    queue.report_progress(
        jid, claimed.attempts, {"completed_items": 8, "total_items": 20}
    )
    detail = client.get(f"/api/projects/{pid}/actions/jobs/{jid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["result_summary"]["completed_items"] == 8
    cancelled = client.post(f"/api/projects/{pid}/actions/jobs/{jid}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert queue.get(jid).status == "cancelled"
    assert not queue.report_progress(jid, claimed.attempts, {"completed_items": 9})

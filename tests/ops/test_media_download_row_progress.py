"""Intermediate row progress for queued YouTube downloads.

An 11-row queued ``media.ytdlp_download`` run must not report ``0/11`` and then
jump straight
to `11/11` with nothing in between.

Root cause (map_runner.py's `flush()`, shared by every reserved-MapRunner
queued kind — classify, transcribe, ocr, youtube_download, fetch_url, ...):
per-row results only got batched into the DB (`runs.completed_rows`) once
`BATCH_SIZE` (25) rows had queued up, with a single unconditional flush at
the very end of the run. Any queued run with fewer than 25 rows therefore
had exactly one DB write, at the end — the run drawer/jobs panel poll
`runs.completed_rows` via GET .../actions/runs/{id}/status, which is the
ONLY progress signal queued (worker-process) runs expose (no in-memory
`on_progress` observer crosses the process boundary — see
server/run_status.py's active_runs comment). The fix makes `flush()` persist
after every completed row; `RunResultStore.write_results`' completed_rows
delta is computed by diffing the `results` table itself, so more frequent
flushing is idempotent and cannot double-count or corrupt the run's
terminal receipt/finalize transaction (a separate BEGIN IMMEDIATE write in
action_reservations._finalize_reserved_action_receipt).

This is a GENRE fix at the shared MapRunner layer, not youtube-specific:
media.ytdlp_download is exercised end-to-end here (matching the bug
report), through the exact production queued path (HTTP action launch ->
job queue -> Worker) also proven by test_media_acquisition_queued_placement.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deterministic_time import controlled_time
from fastapi.testclient import TestClient

from frisket.ops import ytdlp as youtube_ops
from frisket.server.app import create_app
from frisket.ops.ytdlp import DownloadedMedia
from http_test_helpers import post_v1_action_with_exact_confirmation

YOUTUBE_WATCH = "https://www.youtube.com/watch?v=fixture-row-progress"
ROW_COUNT = 11


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))


def _seed_youtube_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "Row progress youtube"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Videos")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "url": project.add_column(sheet_id, "url", type="link"),
    }
    project.add_rows(
        sheet_id,
        [
            {"title": f"Clip {i}", "url": f"{YOUTUBE_WATCH}-{i}"}
            for i in range(ROW_COUNT)
        ],
        columns,
    )
    return project_id, sheet_id


def _youtube_action(*, sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "media.ytdlp_download",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"audio": "media"},
        "params": {
            "source": "url",
            "media_type": "audio",
        },
        "idempotency_key": "row-progress-youtube@sha256:first",
    }


def test_queued_youtube_download_run_reports_intermediate_row_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An 11-row queued download's ``completed_rows`` count,
    read via the same HTTP status endpoint the run drawer/jobs panel use,
    must visit a value strictly between 0 and 11 before landing on 11 — not
    jump 0 -> 11 in one hop.

    Deterministic via the front door's per-item barrier: every downloaded
    row parks at the barrier, the test releases exactly ONE row and awaits
    its flush through the status endpoint. With ten rows still parked, the
    observed value can only be 1 — strictly between 0 and 11 by
    construction (happens-before), not by out-polling a per-row sleep."""

    calls: list[str] = []

    with controlled_time() as t:
        barrier = t.item_barrier()

        def gated_fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
            calls.append(url)
            n = len(calls)
            barrier.arrive()  # parks this row until the test releases it
            return DownloadedMedia(
                data=f"fake media bytes {n}".encode("utf-8"),
                mime="audio/wav",
                filename=f"clip-{n}.wav",
                duration_seconds=1.25,
                metadata={"title": f"Clip {n}", "extractor": "Youtube"},
            )

        monkeypatch.setattr(youtube_ops, "download_media", gated_fake_download)

        client = _client(tmp_path)
        project_id, sheet_id = _seed_youtube_project(client)

        response = post_v1_action_with_exact_confirmation(
            client,
            project_id,
            _youtube_action(sheet_id=sheet_id),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "queued"
        run_id = result["run_id"]
        job_id = result["job_id"]
        assert run_id is not None
        assert job_id is not None
        # Launch returns before yt-dlp runs (queued, not inline): no worker
        # exists yet, so no download can have happened — a happens-before
        # fact, not a race.
        assert calls == []

        ws = client.app.state.workspace
        worker = t.worker(ws.queue, ws.registry, poll_interval=0.005)
        t.background(worker.run_once)

        status_url = f"/api/projects/{project_id}/actions/runs/{run_id}/status"

        # Release exactly one row; every other row stays parked, so
        # completed_rows cannot pass 1 — and the released row's per-row
        # flush must make it visible via the status endpoint.
        barrier.wait_arrived(1)
        barrier.release(1)
        observed = t.wait_until(
            lambda: client.get(status_url).json()["run"]["completed_rows"],
            message="first completed row never flushed to the status endpoint",
        )
        # HARD CONSTRAINT (the bug): an intermediate value is visible while
        # the run is provably still mid-flight (ten rows parked).
        assert 0 < observed < ROW_COUNT, (
            f"completed_rows={observed} while only one of {ROW_COUNT} rows "
            "was released — the per-row flush regressed to batching"
        )

        barrier.release_all()
        t.wait_finalized(job_id)

        final_payload = client.get(status_url).json()
        assert final_payload["run"]["status"] == "completed"
        assert final_payload["run"]["completed_rows"] == ROW_COUNT
        assert len(calls) == ROW_COUNT

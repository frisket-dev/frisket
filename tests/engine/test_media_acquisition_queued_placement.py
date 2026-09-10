"""Typed media acquisition uses observable, cancellable queued project runs.

Exercise request/worker separation for network acquisition, output prechecks,
worker-version stamping, and nonduplicated job projection. The remaining media
readers share this same typed queue dispatcher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_dispatch import placement_for_kind
from frisket.engine.executor.action_specs import (
    PlacementPolicy,
    execution_spec_for,
    queued_project_run_kinds,
)
from frisket.engine.executor.queue_policy import INTENTIONALLY_DIRECT_V1_ACTIONS
from frisket.engine.jobs import RUN_PROJECT_KIND
from frisket.engine.jobs.projection import run_inline_job_id
from frisket.engine.executor import file_fetch as fetch_url_recipe
from frisket.actions.registry import ACTION_REGISTRY
from frisket.ops import ytdlp as youtube_ops
from frisket.server.app import create_app
from frisket.engine.worker_version import code_version
from frisket.ops.ytdlp import DownloadedMedia
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation


ACQUISITION_QUEUED_KINDS = (
    "media.ytdlp_download",
    "media.fetch_url",
    "media.video_frames",
    "media.extract_faces",
)
YOUTUBE_WATCH = "https://www.youtube.com/watch?v=fixture123"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))


# --- placement is declared for the recipe-backed acquisition kinds ----------


def test_four_acquisition_kinds_are_declared_queued_project_run() -> None:
    declared = queued_project_run_kinds()
    for kind in ACQUISITION_QUEUED_KINDS:
        assert kind in declared, kind
        spec = execution_spec_for(kind)
        assert spec is not None, kind
        assert spec.lifecycle.placement is PlacementPolicy.QUEUED_PROJECT_RUN, kind
        assert placement_for_kind(kind) is PlacementPolicy.QUEUED_PROJECT_RUN, kind
        assert ACTION_REGISTRY.get(kind) is not None
        assert kind not in INTENTIONALLY_DIRECT_V1_ACTIONS, kind


# --- media.ytdlp_download: full request/worker split, the driving example -


def _youtube_action(
    *,
    sheet_id: int,
    output_name: str = "media",
    idempotency_key: str = "acq-queued-youtube@sha256:first",
) -> dict[str, Any]:
    return {
        "action_id": "media.ytdlp_download",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "url",
            "media_type": "audio",
        },
        "output_names": {"audio": output_name},
        "idempotency_key": idempotency_key,
    }


def _seed_youtube_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "Acquisition queued youtube"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Videos")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "url": project.add_column(sheet_id, "url", type="link"),
    }
    project.add_rows(sheet_id, [{"title": "Clip", "url": YOUTUBE_WATCH}], columns)
    return project_id, sheet_id


def test_media_download_queued_launch_returns_before_yt_dlp_runs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[str] = []

    def fake_download(url: str, **kwargs: Any) -> DownloadedMedia:
        calls.append(url)
        return DownloadedMedia(
            data=b"RIFFfakewave",
            mime="audio/wav",
            filename="clip.wav",
            duration_seconds=1.25,
            metadata={"title": "Fixture", "extractor": "Youtube"},
        )

    monkeypatch.setattr(youtube_ops, "download_media", fake_download)
    client = _client(tmp_path)
    project_id, sheet_id = _seed_youtube_project(client)

    response = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        _youtube_action(sheet_id=sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())

    # HARD CONSTRAINT: launch returns the run handle immediately; yt-dlp has
    # not run yet.
    assert result.status == "queued"
    assert result.action.kind == "media.ytdlp_download"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert calls == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["action_kind"] == "media.ytdlp_download"
    assert job.payload["spec"]["action_kind"] == "media.ytdlp_download"
    assert "recipe" not in job.payload["spec"]
    project = client.app.state.workspace.get(project_id)
    url_column = next(
        column for column in project.columns(sheet_id) if column["name"] == "url"
    )
    assert job.payload["v1_input_column_ids"] == {"url": url_column["id"]}
    assert job.payload["v1_input_column_types"] == {"url": "link"}
    assert job.payload["v1_output_names"] == {"audio": "media"}

    # A real (positive) queue job id -- not the run.inline negative sentinel
    # the parity projection would otherwise synthesize.
    assert result.job_id > 0
    assert result.job_id != run_inline_job_id(result.run_id)

    status_before = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_before["status"] == "queued"

    # Execution happens on the worker, not the request.
    drain_queue(client)
    assert calls == [YOUTUBE_WATCH]

    status_after = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_after["status"] == "completed"

    project = client.app.state.workspace.get(project_id)
    run_row = project.db.execute(
        "SELECT worker_version FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run_row is not None
    # HARD CONSTRAINT: the worker stamps worker_version for this kind now
    # (jobs/runs.py's generic RUN_PROJECT_KIND handler -- proven generic, not
    # assumed).
    assert run_row["worker_version"] == code_version()

    receipt_row = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"
    assert (
        project.db.execute(
            "SELECT source_url FROM blobs WHERE filename='clip.wav'"
        ).fetchone()[0]
        == YOUTUBE_WATCH
    )


def test_media_download_queued_overwrite_precheck_fires_before_worker(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        youtube_ops,
        "download_media",
        lambda url, **kwargs: (
            calls.append(url)
            or DownloadedMedia(  # noqa: ARG005
                data=b"unused",
                mime="audio/wav",
                filename="x.wav",
                duration_seconds=1.0,
                metadata={},
            )
        ),
    )
    client = _client(tmp_path)
    project_id, sheet_id = _seed_youtube_project(client)

    # Target the pre-existing SOURCE "url" column -- always a collision,
    # regardless of overwrite intent.
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_youtube_action(sheet_id=sheet_id, output_name="url"),
    )
    # A failed action result is a 409 (Conflict) HTTP response, not a 200.
    assert response.status_code == 409, response.text
    result = ActionResult.model_validate(response.json())

    # HARD CONSTRAINT: the overwrite precheck fires at REQUEST time -- no run,
    # no job, no yt-dlp call.
    assert result.status == "failed"
    assert result.errors[0].code == "output_column_exists"
    assert result.run_id is None
    assert result.job_id is None
    assert calls == []


# --- media.fetch_url: full request/worker split, a different payload shape -


def _fetch_url_action(
    *, sheet_id: int, idempotency_key: str = "acq-queued-fetch@sha256:first"
) -> dict[str, Any]:
    return {
        "action_id": "media.fetch_url",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "url"},
        "output_names": {"media": "media"},
        "idempotency_key": idempotency_key,
    }


def test_media_fetch_url_queued_launch_returns_before_download_and_worker_finalizes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[str] = []

    def fake_download_url(url: str, **kwargs: Any) -> tuple[bytes, str, str, None]:
        calls.append(url)
        return b"ID3fake", "audio/mpeg", "episode.mp3", None

    monkeypatch.setattr(fetch_url_recipe.enclosures, "download_url", fake_download_url)
    client = _client(tmp_path)
    project_id = client.post(
        "/api/projects", json={"name": "Acquisition queued fetch"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Feed")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "url": project.add_column(sheet_id, "url", type="link"),
    }
    project.add_rows(
        sheet_id,
        [{"title": "Audio", "url": "https://cdn.example/audio/episode.mp3"}],
        columns,
    )

    response = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        _fetch_url_action(sheet_id=sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert calls == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["action_kind"] == "media.fetch_url"
    assert job.payload["v1_input_column_ids"] == {"url": columns["url"]}
    assert job.payload["v1_output_names"] == {"media": "media"}

    drain_queue(client)
    assert calls == ["https://cdn.example/audio/episode.mp3"]

    status_after = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_after["status"] == "completed"


# --- dedupe: the parity union's negative-id projection never double-counts --


def test_queued_acquisition_run_appears_once_as_a_real_job_not_double_projected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        youtube_ops,
        "download_media",
        lambda url, **kwargs: DownloadedMedia(  # noqa: ARG005
            data=b"RIFFfakewave",
            mime="audio/wav",
            filename="clip.wav",
            duration_seconds=1.25,
            metadata={},
        ),
    )
    client = _client(tmp_path)
    project_id, sheet_id = _seed_youtube_project(client)

    response = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        _youtube_action(sheet_id=sheet_id),
    )
    result = ActionResult.model_validate(response.json())
    drain_queue(client)

    listed = client.get(f"/api/projects/{project_id}/actions/jobs")
    assert listed.status_code == 200, listed.text
    jobs = listed.json()["jobs"]
    matching = [j for j in jobs if j["run_id"] == result.run_id]
    # Exactly one entry -- the real queue job -- not also a synthesized
    # run.inline negative-id entry (_unqueued_run_rows's exclude_run_ids scan
    # in server/services/action_runs.py covers it because a real Job row with
    # this run_id now exists).
    assert len(matching) == 1, matching
    assert matching[0]["job_id"] == result.job_id
    assert matching[0]["job_id"] > 0
    assert matching[0]["kind"] == RUN_PROJECT_KIND
    assert matching[0]["status"] == "done"


# --- media.video_frames / media.extract_faces: declared + registry-wired ---
# (same blob_refs payload shape as the already-queued, already-HTTP-tested
# media.transcribe/media.ocr -- test_http_action_run_queue_boundary.py
# exercises that exact generic path end to end already; this asserts THESE
# kinds ride it too.)


def test_media_video_frames_and_extract_faces_are_wired_into_the_queue_registry() -> (
    None
):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import typed_queued_map_spec
    from frisket.engine.executor.queued_actions import _queued_v1_action_entry
    from frisket.engine.executor.action_inventory import _QueuedActionInventoryEntry

    for kind in ("media.video_frames", "media.extract_faces"):
        bound = typed_action_for_request(
            {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"source": "media"},
                "idempotency_key": kind + ":placement",
            }
        )
        _, spec, program = typed_queued_map_spec(bound)
        entry = _queued_v1_action_entry(_QueuedActionInventoryEntry(spec=spec))
        assert entry.kind == kind
        assert entry.payload_keys == (
            "input_column_ids",
            "input_column_types",
            "output_names",
            "output_target_preconditions",
        )
        assert program.name == kind
        assert entry.reserve_action is not None
        assert entry.cleanup_reservation is not None
        assert entry.mark_enqueued is not None
        assert entry.finalize_action is not None

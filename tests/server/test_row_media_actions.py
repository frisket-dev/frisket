"""Queued media result recovery retains named lists without rerunning workers."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.store.receipts import ReceiptStore
from frisket.server.app import create_app
from http_test_helpers import drain_queue
from tests.engine.test_media_extract_faces_executor import (
    _extract_faces_action,
    _patch_face_sandbox,
    _seed as seed_faces,
)
from tests.ops.test_media_video_frames_executor import (
    _patch_ffmpeg_sandbox,
    _seed as seed_frames,
    _video_frames_action,
)


@pytest.mark.parametrize("kind", ["frames", "faces"])
@pytest.mark.parametrize("crash", [False, True])
def test_queued_row_media_recovers_durable_occurrences_without_extraction(
    tmp_path, monkeypatch, kind, crash
):
    from frisket.engine.jobs import runs

    patch, seed, make = (
        (_patch_ffmpeg_sandbox, seed_frames, _video_frames_action)
        if kind == "frames"
        else (_patch_face_sandbox, seed_faces, _extract_faces_action)
    )
    patch(monkeypatch)
    with TestClient(
        create_app(tmp_path / "workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post("/api/projects", json={"name": "Media"}).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        seeded = seed(project, tmp_path)
        request = make(
            sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"], output_name="Images"
        )
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=request
        )
        assert response.status_code == 200, response.text
        queued = response.json()
        job = workspace.queue.get(queued["job_id"])
        payload = {**deepcopy(job.payload), "job_id": job.id}
        handler = workspace.registry.get("project.run")
        context = JobHandlerContext.from_claimed_job(trusted_org_id=None)
        original = runs.queued_v1_finalize_action_result
        if crash:

            class ProcessDeath(BaseException):
                pass

            def die(*args, **kwargs):
                raise ProcessDeath()

            monkeypatch.setattr(runs, "queued_v1_finalize_action_result", die)
            with pytest.raises(ProcessDeath):
                handler(deepcopy(payload), context)
            monkeypatch.setattr(runs, "queued_v1_finalize_action_result", original)
        else:
            drain_queue(client)
        before = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        observations = [
            item.ref
            for item in before.evidence
            if item.ref.get("kind") == "row_file_output"
        ]
        assert len(observations) == (4 if kind == "frames" else 2)

        async def forbidden(*args, **kwargs):
            pytest.fail("Redelivery must not rerun ffmpeg or OpenCV")

        monkeypatch.setattr(
            "frisket.engine.executor.row_media_read.run_sandboxed", forbidden
        )
        assert handler(deepcopy(payload), context)["skipped"] is True
        after = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        assert after.status == "completed", after.model_dump_json()
        assert [
            item.ref
            for item in after.evidence
            if item.ref.get("kind") == "row_file_output"
        ] == observations
        named = next(
            item.ref for item in after.outputs if item.ref.get("kind") == "named_result"
        )
        assert named["schema"] == kind
        assert named["route"] == "Images"
        assert named["row_ids"] == seeded["row_ids"]
        replay = client.post(f"/api/projects/{project_id}/actions/v1/run", json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_id"] == queued["receipt_id"]

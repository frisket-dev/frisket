from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.authoring.action_metadata import action_metadata_for_action_kind
from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from http_test_helpers import (
    drain_queue,
    post_canonical_run_spec_as_v1_action,
    queued_python_run_spec,
)


CSV = "note\nCall 212-555-0123\nNo phone\n"


def test_action_metadata_refuses_retired_implementation_aliases() -> None:
    unknown = {
        "action_kind": "unknown",
        "action_name": "Unknown action",
    }
    for retired in (
        "regex_extract",
        "video_frames",
        "extract_faces",
        "ner",
        "download_media",
        "unexpected_legacy_kind",
    ):
        assert action_metadata_for_action_kind(retired) == unknown
    assert action_metadata_for_action_kind(None) == {
        "action_kind": "unknown",
        "action_name": "Unknown action",
    }


def test_public_run_surfaces_expose_only_canonical_action_identity(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id = client.post(
        "/api/projects", json={"name": "Action identity v1"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    queued_run = post_canonical_run_spec_as_v1_action(
        client,
        project_id,
        queued_python_run_spec(sheet_id, "note", "queued_phone"),
        confirmed=True,
    )
    assert queued_run.status_code == 200, queued_run.text
    queued_run_id = queued_run.json()["run_id"]

    queued_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{queued_run_id}/status"
    )
    assert queued_status.status_code == 200, queued_status.text
    queued_body = queued_status.json()["run"]["public_status"]
    assert "run_kind" not in queued_body
    assert "run_kind" not in queued_body["queue"]
    assert queued_body["action_kind"] == "map.python"
    assert queued_body["queue"]["action_kind"] == "map.python"

    timing = client.get(f"/api/projects/{project_id}/timing")
    assert timing.status_code == 200, timing.text
    timing_body = timing.json()
    assert all("run_kind" not in run for run in timing_body["active_runs"])
    assert timing_body["active_runs"] == [
        {
            "run_id": queued_run_id,
            "action_kind": "map.python",
            "action_name": "Run trusted local Python",
            "status": "queued",
            "timing": timing_body["active_runs"][0]["timing"],
        }
    ]

    action_run = post_canonical_run_spec_as_v1_action(
        client,
        project_id,
        queued_python_run_spec(sheet_id, "note", "action_phone"),
        confirmed=True,
    )
    assert action_run.status_code == 200, action_run.text
    result = ActionResult.model_validate(action_run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    action_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert action_status.status_code == 200, action_status.text
    action_body = action_status.json()
    assert "run_kind" not in action_body["run"]
    assert "run_kind" not in action_body["run"]["public_status"]
    assert action_body["run"]["action_kind"] == "map.python"
    assert action_body["run"]["public_status"]["action_kind"] == "map.python"

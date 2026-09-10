import json

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from helpers import replace_test_source_cell
from http_test_helpers import drain_queue
from tests.engine.test_http_action_run_queue_boundary import (
    _media_ocr_action,
    _media_transcribe_action,
    _seed_media_ocr_project,
    _seed_media_transcribe_project,
)


@pytest.mark.parametrize("kind", ["ocr", "transcribe"])
@pytest.mark.parametrize("change", ["cell", "blob_metadata"])
def test_queued_media_source_changes_refuse_before_read(
    tmp_path, monkeypatch, kind, change
):
    from frisket.engine.executor.ocr_read import AdmittedOcrReader
    from frisket.engine.executor.transcription_read import AdmittedTranscriber

    calls = []

    async def unexpected_read(*args, **kwargs):
        calls.append(True)
        raise AssertionError("queued stale source reached a model reader")

    monkeypatch.setattr(AdmittedOcrReader, "start", unexpected_read)
    monkeypatch.setattr(AdmittedTranscriber, "start", unexpected_read)
    with TestClient(
        create_app(tmp_path / "workspace", run_status_grace_seconds=3600.0)
    ) as client:
        seed = (
            _seed_media_ocr_project if kind == "ocr" else _seed_media_transcribe_project
        )
        action = _media_ocr_action if kind == "ocr" else _media_transcribe_action
        project_id, sheet_id, row_ids, blobs = seed(client)
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=action(sheet_id, row_ids)
        )
        assert response.status_code == 200, response.text
        queued = ActionResult.model_validate(response.json())
        assert queued.status == "queued"
        project = client.app.state.workspace.get(project_id)
        if change == "cell":
            column = next(
                column
                for column in project.columns(sheet_id)
                if column["name"] == "media"
            )
            replace_test_source_cell(
                project,
                row_id=row_ids[0],
                column_id=column["id"],
                value=None,
            )
        else:
            project.db.execute(
                "UPDATE blobs SET filename=? WHERE hash=?",
                ("changed-source-name", blobs[0]),
            )
        project.db.commit()
        drain_queue(client)
        assert calls == []
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (queued.receipt_id,)
            ).fetchone()[0]
        )
        assert receipt["status"] == "failed"
        assert receipt["errors"][0]["code"] == "stale_input"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (queued.run_id,)
            ).fetchone()[0]
            == 0
        )

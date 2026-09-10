import json

import pytest
from fastapi.testclient import TestClient

from frisket.engine.executor.queued_actions import queued_v1_payload_envelope
from frisket.server.app import create_app
from http_test_helpers import drain_queue


@pytest.mark.parametrize("mutation", [None, "unhide", "type", "created_ids", "marker"])
def test_semantic_join_real_http_queue_to_worker(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *args, **kwargs: (
            lambda texts: [[1.0, 0.0] for _ in texts],
            "fastembed/test",
        ),
    )
    with TestClient(
        create_app(tmp_path / "workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Semantic queue"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        left = project.add_sheet("Donors")
        donor = project.add_column(left, "donor")
        project.add_rows(left, [{"donor": "ACME"}], {"donor": donor})
        right = project.add_sheet("Companies")
        company = project.add_column(right, "company")
        project.add_rows(right, [{"company": "Acme"}], {"company": company})
        body = {
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": left},
            "params": {
                "source": "donor",
                "target": {"sheet_id": right, "column": "company"},
            },
            "sheet_name": "Matches",
            "idempotency_key": "queued-semantic",
        }
        response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "queued", result
        job = client.app.state.workspace.queue.get(result["job_id"])
        assert queued_v1_payload_envelope(job.payload) is not None, job.payload
        assert {column["name"] for column in project.columns(left)} == {"donor"}
        if mutation in {"unhide", "type"}:
            field, value = ("hidden", 0) if mutation == "unhide" else ("type", "json")
            project.db.execute(
                f"UPDATE columns SET {field}=? WHERE sheet_id=? AND name='match_value'",
                (value, left),
            )
        elif mutation == "created_ids":
            op = project.db.execute(
                "SELECT ops.id,ops.undo_info FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                (result["run_id"],),
            ).fetchone()
            undo_info = json.loads(op["undo_info"])
            undo_info["created_columns"] = []
            project.db.execute(
                "UPDATE ops SET undo_info=? WHERE id=?",
                (json.dumps(undo_info), op["id"]),
            )
        elif mutation == "marker":
            op = project.db.execute(
                "SELECT ops.id,ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                (result["run_id"],),
            ).fetchone()
            spec = json.loads(op["spec"])
            spec.pop("deferred_publication")
            project.db.execute(
                "UPDATE ops SET spec=? WHERE id=?", (json.dumps(spec), op["id"])
            )
        project.db.commit()
        drain_queue(client)
        project = client.app.state.workspace.get(project_id)
        stored = project.db.execute(
            "SELECT status,body FROM receipts WHERE id=?", (result["receipt_id"],)
        ).fetchone()
        if mutation is not None:
            assert stored["status"] == "failed", stored["body"]
            assert not any(sheet["name"] == "Matches" for sheet in project.sheets())
            assert not project.db.execute(
                "SELECT 1 FROM cell_result_heads WHERE run_id=?", (result["run_id"],)
            ).fetchone()
            return
        assert stored["status"] == "completed", json.dumps(
            json.loads(stored["body"])["errors"]
        )
        assert any(sheet["name"] == "Matches" for sheet in project.sheets())
        assert {column["name"] for column in project.columns(left)} == {
            "donor",
            "match_value",
            "match_score",
            "matched_row_id",
        }

import json

import pytest
from fastapi.testclient import TestClient

from frisket.engine.executor.queued_actions import queued_v1_payload_envelope
from frisket.server.app import create_app
from http_test_helpers import drain_queue


@pytest.mark.parametrize("empty", [False, True])
def test_cluster_http_queue_publishes_reviewed_values_and_replays(tmp_path, empty):
    with TestClient(
        create_app(tmp_path / "workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Cluster queue"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet = project.add_sheet("Names")
        source = project.add_column(sheet, "name")
        project.add_rows(
            sheet,
            [] if empty else [{"name": "Jane Doe"}, {"name": "Doe Jane"}],
            {"name": source},
        )
        project.db.commit()
        body = {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "name"},
            "output_names": {"canonical": "Reviewed"},
            "idempotency_key": "queued-cluster",
        }
        response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "queued", result
        job = client.app.state.workspace.queue.get(result["job_id"])
        assert queued_v1_payload_envelope(job.payload) is not None, job.payload
        assert {column["name"] for column in project.columns(sheet)} == {"name"}, {
            "columns": [
                dict(column) for column in project.columns(sheet, include_hidden=True)
            ],
            "run": dict(
                project.db.execute(
                    "SELECT * FROM runs WHERE id=?", (result["run_id"],)
                ).fetchone()
            ),
            "op": dict(
                project.db.execute(
                    "SELECT ops.* FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
                    (result["run_id"],),
                ).fetchone()
            ),
        }
        drain_queue(client)
        project = client.app.state.workspace.get(project_id)
        stored = project.db.execute(
            "SELECT status,body FROM receipts WHERE id=?", (result["receipt_id"],)
        ).fetchone()
        assert stored["status"] == "completed", stored["body"]
        receipt = json.loads(stored["body"])
        [fact] = [
            item["ref"]
            for item in receipt["evidence"]
            if item["ref"].get("kind") == "value_clusters"
        ]
        assert len(fact["canonical_values"]) == (0 if empty else 2)
        output = next(
            column for column in project.columns(sheet) if column["name"] == "Reviewed"
        )
        assert output["id"] == fact["output"]["column_id"]
        replay = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_id"] == result["receipt_id"]
        assert replay.json()["status"] == "completed"

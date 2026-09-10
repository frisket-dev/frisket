import pytest
from fastapi.testclient import TestClient

from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.executor.cluster_receipt_read import AdmittedClusterReceiptReader
from frisket.actions.entity_types import ClusterReceiptSource
from frisket.server.app import create_app
from http_test_helpers import drain_queue


@pytest.mark.parametrize("values", [[], ["Jon Smith", "Smith Jon"]])
def test_review_cluster_empty_and_nonempty_replay(tmp_path, values):
    project = Project.create(tmp_path / "review.frisket")
    try:
        sheet = project.add_sheet("Names")
        column = project.add_column(sheet, "name")
        rows = project.add_rows(
            sheet, [{"name": value} for value in values], {"name": column}
        )
        request = {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "name"},
            "idempotency_key": "review-cluster",
        }
        result = run_action_spec(project, request, project_id="review")
        assert result.status == "completed", result.model_dump()
        replay = run_action_spec(project, request, project_id="review")
        assert replay.status == "completed", replay.model_dump()
        assert replay.receipt_id == result.receipt_id
        groups = AdmittedClusterReceiptReader(project).read(
            ClusterReceiptSource(kind="cluster_values", receipt_id=result.receipt_id)
        )
        assert len(groups) == bool(values)
        if rows:
            project.apply_edits(
                [{"row_id": rows[0], "column_id": column, "value": "Changed"}]
            )
        else:
            project.add_rows(sheet, [{"name": "New"}], {"name": column})
        stale = run_action_spec(project, request, project_id="review")
        assert stale.status == "failed", stale.model_dump()
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


@pytest.mark.parametrize("edit_before_drain", [True, False])
def test_review_cluster_http_source_fence(tmp_path, edit_before_drain):
    with TestClient(
        create_app(tmp_path / "workspace", run_status_grace_seconds=3600)
    ) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Cluster review"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet = project.add_sheet("Names")
        source = project.add_column(sheet, "name")
        rows = project.add_rows(
            sheet, [{"name": "Jon Smith"}, {"name": "Smith Jon"}], {"name": source}
        )
        request = {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "name"},
            "idempotency_key": "review-queued",
        }
        route = f"/api/projects/{project_id}/actions/v1/run"
        response = client.post(route, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"
        if not edit_before_drain:
            drain_queue(client)
        project = client.app.state.workspace.get(project_id)
        project.apply_edits(
            [{"row_id": rows[0], "column_id": source, "value": "Changed"}]
        )
        if edit_before_drain:
            drain_queue(client)
        replay = client.post(route, json=request)
        assert replay.json()["status"] == "failed", replay.text
        if not edit_before_drain:
            assert replay.json()["errors"][0]["code"] == "stale_replay"
        else:
            project = client.app.state.workspace.get(project_id)
            assert [column["name"] for column in project.columns(sheet)] == ["name"]

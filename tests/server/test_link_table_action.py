from __future__ import annotations

from tests.engine.test_entities import _client
from tests.engine.test_derive_link_table_executor import _derive_link_action, _seed


def test_public_link_table_exact_scope_receipt_replay_and_current_project(
    tmp_path, monkeypatch
):
    with _client(tmp_path) as client:
        pid = client.post("/api/projects", json={"name": "Link workflow"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        seed = _seed(project, tmp_path)

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "completed match materialization has no provider calls"
            )

        monkeypatch.setattr("frisket.semantic.resolve_embedder", forbidden)
        request = _derive_link_action(
            seed["join_receipt_id"],
            source_sheet_id=seed["source_sheet_id"],
            row_ids=seed["source_row_ids"][2:],
        )
        request["output_names"] = {"target_value": "Reviewed company"}
        url = f"/api/projects/{pid}/actions/v1/run"
        response = client.post(url, json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed", result
        assert result["run_id"] is None
        assert (
            len(next(o["row_ids"] for o in result["outputs"] if o["kind"] == "rows"))
            == 1
        )
        assert any(o["name"] == "Reviewed company" for o in result["outputs"])
        assert (
            client.post(url, json=request).json()["receipt_id"] == result["receipt_id"]
        )
        receipt = client.get(
            f"/api/projects/{pid}/actions/v1/receipts/{result['receipt_id']}"
        )
        assert receipt.status_code == 200
        source = next(
            i["ref"]
            for i in receipt.json()["inputs"]
            if i["ref"]["kind"] == "semantic_join_link_source"
        )
        assert source["requested_row_ids"] == seed["source_row_ids"][2:]
        assert source["source_receipt_id"] == seed["join_receipt_id"]
        other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
        foreign = client.post(f"/api/projects/{other}/actions/v1/run", json=request)
        assert foreign.status_code == 400, foreign.text
        assert foreign.json()["errors"][0]["code"] == "invalid_input_ref"

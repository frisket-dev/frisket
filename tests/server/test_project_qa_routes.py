from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_shared_history_revision_and_project_boundary(tmp_path):
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        response = client.post(path, json={"scope": {"kind": "project"}})
        assert response.status_code == 200, response.text
        thread = response.json()
        detail = f"{path}/{thread['id']}"
        assert client.get(detail).json()["thread"] == thread
        assert (
            client.get(f"/api/projects/{other}/qa/threads/{thread['id']}").status_code
            == 404
        )
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "title": "New title"}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "title": "Stale"}
            ).status_code
            == 409
        )
        assert (
            client.get(detail + "/events", params={"after": 1, "before": 2}).status_code
            == 422
        )
        assert (
            client.get(detail + "/report").json()["markdown"].startswith("# New title")
        )
        assert client.delete(detail).status_code == 204
        assert client.get(path).json() == []


def test_missing_scope_is_rejected_before_admission(tmp_path):
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        missing = {"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": 999}]}
        assert client.post(path, json={"scope": missing}).status_code == 422
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        detail = f"{path}/{thread['id']}"
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "scope": missing}
            ).status_code
            == 422
        )
        assert (
            client.post(
                detail + "/turns",
                json={"request_id": "bad", "question": "Read", "scope": missing},
            ).status_code
            == 422
        )
        result = client.get(detail).json()
        assert result["active_turn"] is None
        assert result["history"]["events"] == []


def test_citation_resolves_saved_value_and_refuses_another_thread(tmp_path):
    from frisket.engine.store.project_qa import ProjectQAStore
    from frisket.server.services.project_qa_tools import ProjectQATools

    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Evidence")
        column = project.add_column(sheet, "Name")
        [row] = project.add_rows(sheet, [{"Name": "Ada"}], {"Name": column})
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Question")
        other = store.create_thread(title="Other")
        turn = store.submit_turn(thread["id"], request_id="once", question="Who?")
        read = ProjectQATools(project, turn, store).read_rows(sheet)
        citation_id = read["rows"][0]["cells"][0]["citation_id"]
        store.finish_turn(turn["id"], status="completed")
        prefix = f"/api/projects/{pid}/qa/threads"
        path = f"{prefix}/{thread['id']}/citations/{citation_id}"
        result = client.get(path)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "current"
        assert result.json()["target"] == {
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
        }
        assert (
            client.get(f"{prefix}/{other['id']}/citations/{citation_id}").status_code
            == 404
        )
        project.apply_edits([{"row_id": row, "column_id": column, "value": "Grace"}])
        changed = client.get(path).json()
        assert changed["status"] == "changed"
        assert "Ada" in changed["excerpt"]
        project.delete_sheet(sheet)
        assert client.get(path).json()["status"] == "unavailable"

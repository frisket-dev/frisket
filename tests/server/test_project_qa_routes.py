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

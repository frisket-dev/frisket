"""``columns.semantic_type`` on the /data payload (frisket.contracts.http.
models.SheetDataColumn). The marker is set only through the store (no v1
action exposes it -- it is written exclusively by recipe execution, e.g.
map.ner), so this poke sets it directly on the live Project the app already
holds (``app.state.workspace``) and asserts the HTTP payload reflects it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    router = ModelRouter(cache=None, cache_mode="off")
    return TestClient(create_app(tmp_path / "ws", router=router))


def _import(client: TestClient, pid: str, csv: str) -> int:
    r = client.post(
        f"/api/projects/{pid}/import/csv", files={"file": ("t.csv", csv, "text/csv")}
    )
    assert r.status_code == 200, r.text
    return r.json()["sheet_id"]


def _cols(client: TestClient, pid: str, sheet_id: int) -> dict[str, dict]:
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    return {c["name"]: c for c in data["columns"]}


def test_data_endpoint_carries_semantic_type_for_a_marked_column(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
    sheet_id = _import(client, pid, "name,mentions\nAda,x\nGrace,y\n")

    cols_before = _cols(client, pid, sheet_id)
    assert cols_before["mentions"]["semantic_type"] is None
    assert cols_before["name"]["semantic_type"] is None

    project = client.app.state.workspace.get(pid)
    project.set_column_semantic_type(cols_before["mentions"]["id"], "entity_mentions")

    cols_after = _cols(client, pid, sheet_id)
    assert cols_after["mentions"]["semantic_type"] == "entity_mentions"
    # The unmarked column stays null -- never inferred, never contagious.
    assert cols_after["name"]["semantic_type"] is None

"""Lens-view sheet-data fetch via ``GET /sheets/{id}/data?row_ids=``.

A saved lens resolves to an ordered, ranked row-id set; the grid must then page
EXACTLY those rows in that RANKED order. The ?row_ids= param scopes the data route
to the named ids, preserves their given order (NOT the natural sheet order), drops
unknown/foreign ids, and reports the scoped total — the real contract the web grid
relies on for the lens grid view.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))


def _seed(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(
        sheet,
        [{"headline": t} for t in ("cat", "kitten", "airplane", "puppy")],
        {"headline": col},
    )
    return pid, project, sheet, col


def _all_ids(client, pid, sheet):
    data = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data?offset=0&limit=50"
    ).json()
    return [r["id"] for r in data["rows"]]


def test_row_ids_scopes_grid_to_exactly_those_rows_in_given_order(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    ids = _all_ids(client, pid, sheet)
    assert len(ids) == 4
    # a ranked subset in NON-natural order (reverse of the last two rows)
    ranked = [ids[3], ids[1]]
    resp = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data",
        params={"row_ids": ",".join(str(i) for i in ranked), "offset": 0, "limit": 50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # exactly the lens rows, in the RANKED order (not the full sheet, not sorted)
    assert [r["id"] for r in body["rows"]] == ranked
    assert body["total"] == 2
    # cells still come through (a real grid render, not an id-only stub)
    headline_col = next(c for c in body["columns"] if c["name"] == "headline")
    cid = str(headline_col["id"])
    assert body["rows"][0]["cells"][cid] == "puppy"
    assert body["rows"][1]["cells"][cid] == "kitten"


def test_row_ids_drops_unknown_and_foreign_ids(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    ids = _all_ids(client, pid, sheet)
    # a never-existed id (99999) and a duplicate are dropped; order preserved
    resp = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data",
        params={"row_ids": f"{ids[2]},99999,{ids[0]},{ids[2]}"},
    )
    body = resp.json()
    assert [r["id"] for r in body["rows"]] == [ids[2], ids[0]]
    assert body["total"] == 2


def test_row_ids_paginates_within_the_scoped_set(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    ids = _all_ids(client, pid, sheet)
    ranked = [ids[3], ids[2], ids[1], ids[0]]
    page = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data",
        params={"row_ids": ",".join(str(i) for i in ranked), "offset": 1, "limit": 2},
    ).json()
    # total is the FULL scoped set; the window respects offset/limit + order
    assert page["total"] == 4
    assert [r["id"] for r in page["rows"]] == [ids[2], ids[1]]


def test_empty_row_ids_yields_no_rows(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    body = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data", params={"row_ids": ""}
    ).json()
    assert body["rows"] == []
    assert body["total"] == 0


def test_row_ids_too_many_returns_typed_400(tmp_path):
    # An unbounded ``row_ids`` list must be rejected with a clear 400,
    # not parsed into an arbitrarily large set. The cap is MAX_EXPLICIT_ROW_IDS.
    from frisket.server.services.sheet_grid import MAX_EXPLICIT_ROW_IDS

    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    too_many = ",".join(str(i) for i in range(1, MAX_EXPLICIT_ROW_IDS + 2))
    resp = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data", params={"row_ids": too_many}
    )
    assert resp.status_code == 400
    assert "too many row_ids" in resp.text
    # exactly at the cap is still accepted (boundary)
    at_cap = ",".join(str(i) for i in range(1, MAX_EXPLICIT_ROW_IDS + 1))
    ok = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data", params={"row_ids": at_cap}
    )
    assert ok.status_code == 200


def test_row_ids_invalid_token_returns_typed_400(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    resp = client.get(
        f"/api/projects/{pid}/sheets/{sheet}/data", params={"row_ids": "1,notanint,3"}
    )
    assert resp.status_code == 400
    assert "invalid row_ids value" in resp.text

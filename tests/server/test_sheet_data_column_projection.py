from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import make_client


def _import(client: TestClient, project_id: str, name: str, csv: str) -> int:
    response = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": (name, csv, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return int(response.json()["sheet_id"])


def test_sheet_data_column_projection_returns_only_requested_cells(
    tmp_path, monkeypatch
) -> None:
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Projection"}).json()["id"]
    sheet_id = _import(
        client, project_id, "docs.csv", "title,body,other\nOne,Body,Nope\n"
    )
    full = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data").json()
    by_name = {column["name"]: column for column in full["columns"]}
    monkeypatch.setattr(
        "frisket.server.services.sheet_grid.compute_transcript_statuses",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("projected cell hydration scanned transcript metadata")
        ),
    )

    projected = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={
            "row_ids": str(full["rows"][0]["id"]),
            "column_ids": f"{by_name['body']['id']},{by_name['title']['id']}",
        },
    )
    assert projected.status_code == 200, projected.text
    payload = projected.json()
    assert [column["name"] for column in payload["columns"]] == ["body", "title"]
    assert payload["rows"][0]["cells"] == {
        str(by_name["body"]["id"]): "Body",
        str(by_name["title"]["id"]): "One",
    }
    assert set(payload["rows"][0]["meta"]) == {
        str(by_name["body"]["id"]),
        str(by_name["title"]["id"]),
    }


def test_sheet_data_column_projection_rejects_cross_sheet_and_bad_lists(
    tmp_path,
) -> None:
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Projection"}).json()["id"]
    sheet_id = _import(client, project_id, "one.csv", "title,body\nOne,Body\n")
    other_sheet_id = _import(client, project_id, "two.csv", "foreign\nValue\n")
    other = client.get(
        f"/api/projects/{project_id}/sheets/{other_sheet_id}/data"
    ).json()["columns"][0]["id"]

    for column_ids in (str(other),):
        response = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/data",
            params={"column_ids": column_ids},
        )
        assert response.status_code == 400, response.text

    empty = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"column_ids": ""},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["columns"] == []
    assert empty.json()["rows"][0]["cells"] == {}

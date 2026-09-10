from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


def _seed_project(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Independent watches"}).json()[
        "id"
    ]
    response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return pid, int(response.json()["sheet_id"])


def _create_filter_watch(
    client: TestClient, pid: str, sheet_id: int
) -> dict[str, object]:
    response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Open rows watch",
            "scope": {"kind": "sheet", "sheet_id": sheet_id},
            "query": {
                "kind": "filter",
                "sheet_id": sheet_id,
                "filter": {"status": {"eq": "open"}},
                # Presentation order is deliberately not Watch authority.
                "sort": [{"column": "title", "dir": "desc"}],
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_watch_owns_captured_filters_and_ignores_saved_view_changes(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    view = client.post(
        f"/api/projects/{pid}/views",
        json={
            "name": "Open rows",
            "sheet_id": sheet_id,
            "filter": {"status": {"eq": "open"}},
            "sort": [{"column": "title", "dir": "asc"}],
        },
    ).json()

    watch = _create_filter_watch(client, pid, sheet_id)
    assert watch["query"] == {
        "kind": "filter",
        "sheet_id": sheet_id,
        "filter": {"status": {"eq": "open"}},
    }

    changed = client.put(
        f"/api/projects/{pid}/views/{view['id']}/definition",
        json={
            "filter": {"status": {"eq": "closed"}},
            "sort": [],
            "columns": None,
            "column_groups": None,
        },
    )
    assert changed.status_code == 200, changed.text
    deleted = client.delete(f"/api/projects/{pid}/views/{view['id']}")
    assert deleted.status_code == 200, deleted.text

    run = client.post(f"/api/projects/{pid}/watches/{watch['id']}/run")
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["run"]["status"] == "ok"
    assert payload["run"]["matched_rows"] == 2
    assert payload["run"]["resolved_query"]["filter"] == {"status": {"eq": "open"}}


def test_watch_creation_does_not_create_a_saved_view(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    project = client.app.state.workspace.get(pid)

    before = project.db.execute("SELECT count(*) FROM views").fetchone()[0]
    watch = _create_filter_watch(client, pid, sheet_id)

    assert watch["id"] > 0
    assert project.db.execute("SELECT count(*) FROM views").fetchone()[0] == before


@pytest.mark.parametrize(
    "retired_key", ["binding_mode", "source_view_id", "source_view"]
)
def test_watch_create_rejects_retired_saved_view_authority(
    tmp_path: Path, retired_key: str
) -> None:
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    payload: dict[str, object] = {
        "name": "Closed request",
        "query": {"kind": "filter", "sheet_id": sheet_id, "filter": {}},
    }
    payload[retired_key] = (
        {"name": "Source", "sheet_id": sheet_id, "filter": {}}
        if retired_key == "source_view"
        else (1 if retired_key == "source_view_id" else "snapshot")
    )

    response = client.post(f"/api/projects/{pid}/watches", json=payload)

    assert response.status_code == 422, response.text
    assert any(item["loc"][-1] == retired_key for item in response.json()["detail"])


def test_watch_storage_requires_owned_query(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, _sheet_id = _seed_project(client)
    project = client.app.state.workspace.get(pid)

    with pytest.raises(sqlite3.IntegrityError):
        project.db.execute(
            "INSERT INTO watches (name, query) VALUES (?, NULL)", ("bad",)
        )

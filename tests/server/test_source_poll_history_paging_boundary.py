from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store.sources import SourceStore


# mock.ts (?mock=1 adapter) retired in onboard-sample-project-v1


def _seed_source_runs(tmp_path: Path) -> tuple[TestClient, str, int, list[int]]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Source poll history paging"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    source_id = SourceStore(project).add_source(
        "Long-lived feed",
        kind="rss",
        url="https://example.test/feed.xml",
        schedule="@hourly",
    )
    run_ids: list[int] = []
    for index in range(125):
        run_ids.append(
            SourceStore(project).record_source_run(
                source_id,
                status="error" if index % 10 == 0 else "ok",
                new_rows=index % 7,
                error=f"poll {index + 1} failed" if index % 10 == 0 else None,
            )
        )
    return client, project_id, source_id, run_ids


def test_source_detail_returns_bounded_poll_history_page(tmp_path: Path) -> None:
    client, project_id, source_id, run_ids = _seed_source_runs(tmp_path)

    first_page = client.get(
        f"/api/projects/{project_id}/sources/{source_id}",
        params={"runs_offset": 0, "runs_limit": 20},
    )
    assert first_page.status_code == 200, first_page.text
    body = first_page.json()
    assert body["id"] == source_id
    assert body["runs_page"] == {
        "schema_version": "frisket.source_runs_page.v1",
        "order": "desc",
        "offset": 0,
        "limit": 20,
        "total": len(run_ids),
        "has_more": True,
        "next_offset": 20,
        "latest_run": body["runs"][0],
        "latest_run_loaded": True,
    }
    assert body["runs_page"]["latest_run"]["id"] == run_ids[-1]
    assert len(body["runs"]) == 20
    assert [run["id"] for run in body["runs"]] == list(reversed(run_ids[-20:]))

    tail_page = client.get(
        f"/api/projects/{project_id}/sources/{source_id}",
        params={"runs_offset": 120, "runs_limit": 20},
    )
    assert tail_page.status_code == 200, tail_page.text
    tail_body = tail_page.json()
    assert tail_body["runs_page"] == {
        "schema_version": "frisket.source_runs_page.v1",
        "order": "desc",
        "offset": 120,
        "limit": 20,
        "total": len(run_ids),
        "has_more": False,
        "next_offset": None,
        "latest_run": first_page.json()["runs"][0],
        "latest_run_loaded": False,
    }
    assert tail_body["runs_page"]["latest_run"]["id"] == run_ids[-1]
    assert [run["id"] for run in tail_body["runs"]] == list(reversed(run_ids[:5]))

    for params in (
        {"runs_offset": -1, "runs_limit": 20},
        {"runs_offset": 0, "runs_limit": 0},
        {"runs_offset": 0, "runs_limit": 101},
    ):
        invalid = client.get(
            f"/api/projects/{project_id}/sources/{source_id}",
            params=params,
        )
        assert invalid.status_code == 422

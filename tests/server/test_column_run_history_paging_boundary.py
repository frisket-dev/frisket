from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.run_payloads import column_run_provenance_payload
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


def _seed_column_run_history(
    tmp_path: Path,
) -> tuple[TestClient, str, int, int, list[int]]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Column run paging"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    source_col = project.add_column(sheet_id, "story")
    topic_col = project.add_column(sheet_id, "topic", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [{"story": "alpha"}, {"story": "beta"}],
        {"story": source_col},
    )
    run_ids: list[int] = []
    for index in range(5):
        op_id = project.append_op(
            "map", {"action_kind": "map.classify"}, label=f"run {index}"
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            model=f"anthropic/test-{index}",
            params={
                "action_kind": "map.classify",
                "sheet_id": sheet_id,
                "input_columns": ["story"],
                "context": f"prompt {index}",
                "fields": [
                    {
                        "name": "topic",
                        "type": "category",
                        "labels": ["a", "b"],
                    }
                ],
            },
            total_rows=len(row_ids),
            row_ids=row_ids,
        )
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": topic_col,
                    "value": f"topic-{index}-{row_position}",
                }
                for row_position, row_id in enumerate(row_ids)
            ],
        )
        RunResultStore(project).finish_run(run_id)
        RunResultStore(project).point_column_at_run(op_id, topic_col, run_id)
        run_ids.append(run_id)
    RunResultStore(project).point_column_at_run(op_id, topic_col, run_ids[1])
    return client, project_id, topic_col, sheet_id, run_ids


def test_column_run_history_route_returns_bounded_pages(tmp_path: Path) -> None:
    client, project_id, topic_col, _sheet_id, run_ids = _seed_column_run_history(
        tmp_path
    )
    newest = list(reversed(run_ids))

    first_page = client.get(
        f"/api/projects/{project_id}/columns/{topic_col}/runs",
        params={"offset": 0, "limit": 2},
    )
    second_page = client.get(
        f"/api/projects/{project_id}/columns/{topic_col}/runs",
        params={"offset": 2, "limit": 2},
    )
    final_page = client.get(
        f"/api/projects/{project_id}/columns/{topic_col}/runs",
        params={"offset": 4, "limit": 2},
    )

    assert first_page.status_code == 200, first_page.text
    body = first_page.json()
    _assert_no_recipe_keys(body)
    assert body["offset"] == 0
    assert body["limit"] == 2
    assert body["total"] == 5
    assert body["has_more"] is True
    assert body["next_offset"] == 2
    assert body["column"]["current_run_id"] is None
    assert body["column"]["latest_run_id"] == run_ids[-1]
    assert body["column"]["mixed_origins"] is False
    assert body["current_run"] is None
    assert body["current_run_loaded"] is False
    assert body["latest_run"]["run_id"] == run_ids[-1]
    assert body["latest_run_loaded"] is True
    assert [run["run_id"] for run in body["runs"]] == newest[:2]
    assert body["runs"][0]["action_kind"] == "map.classify"
    assert body["runs"][0]["action_name"] == "Classify rows"

    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert second_body["offset"] == 2
    assert second_body["has_more"] is True
    assert second_body["next_offset"] == 4
    assert second_body["current_run_loaded"] is False
    assert second_body["latest_run"]["run_id"] == run_ids[-1]
    assert second_body["latest_run_loaded"] is False
    assert [run["run_id"] for run in second_body["runs"]] == newest[2:4]
    assert [run["current"] for run in second_body["runs"]].count(True) == 0

    assert final_page.status_code == 200, final_page.text
    assert final_page.json()["offset"] == 4
    assert final_page.json()["has_more"] is False
    assert final_page.json()["next_offset"] is None
    assert [run["run_id"] for run in final_page.json()["runs"]] == newest[4:]

    for params in (
        {"offset": -1, "limit": 2},
        {"offset": 0, "limit": 0},
        {"offset": 0, "limit": 101},
    ):
        invalid = client.get(
            f"/api/projects/{project_id}/columns/{topic_col}/runs",
            params=params,
        )
        assert invalid.status_code == 422


def test_column_run_history_helper_is_paged_and_route_delegates(
    tmp_path: Path,
) -> None:
    client, project_id, topic_col, _sheet_id, run_ids = _seed_column_run_history(
        tmp_path
    )
    project = client.app.state.workspace.get(project_id)
    direct = column_run_provenance_payload(project, topic_col, offset=1, limit=3)
    response = client.get(
        f"/api/projects/{project_id}/columns/{topic_col}/runs",
        params={"offset": 1, "limit": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json() == direct
    assert direct["offset"] == 1
    assert direct["limit"] == 3
    assert direct["total"] == len(run_ids)
    assert direct["has_more"] is True
    assert direct["next_offset"] == 4
    assert direct["column"]["current_run_id"] is None
    assert direct["column"]["latest_run_id"] == run_ids[-1]
    assert direct["current_run"] is None
    assert direct["current_run_loaded"] is False
    assert direct["latest_run"]["run_id"] == run_ids[-1]
    assert direct["latest_run_loaded"] is False
    assert [run["run_id"] for run in direct["runs"]] == list(reversed(run_ids))[1:4]

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


ROOT = Path(__file__).resolve().parents[2]


def _assert_no_recipe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in {"recipe", "recipe_version"}
            _assert_no_recipe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_recipe_keys(child)


def _seed_review_history(tmp_path: Path) -> tuple[TestClient, str, list[int]]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Review bundle paging"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("stories")
    story_col = project.add_column(sheet_id, "story")
    risk_col = project.add_column(sheet_id, "risk", ai_generated=True)
    tone_col = project.add_column(sheet_id, "tone", ai_generated=True)
    justification_col = project.add_column(
        sheet_id, "risk_justification", ai_generated=True
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"story": f"Story {index:02d}"} for index in range(64)],
        {"story": story_col},
    )
    op_id = project.append_op(
        "map",
        {"action_kind": "map.classify", "purpose": "review paging"},
        label="Classify rows for review paging",
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        model="anthropic/claude-haiku-4-5",
        params={"fields": [{"name": "risk"}, {"name": "tone"}]},
        total_rows=len(row_ids),
        row_ids=row_ids,
    )
    batch: list[dict[str, Any]] = []
    for index, row_id in enumerate(row_ids):
        confidence = round(0.01 + index / 1000, 4)
        batch.extend(
            [
                {
                    "row_id": row_id,
                    "column_id": risk_col,
                    "value": "high" if index % 2 else "low",
                    "confidence": confidence,
                    "justification": f"risk justification {index}",
                },
                {
                    "row_id": row_id,
                    "column_id": tone_col,
                    "value": "urgent" if index % 3 else "routine",
                    "confidence": round(confidence + 0.001, 4),
                    "justification": f"tone justification {index}",
                },
                {
                    "row_id": row_id,
                    "column_id": justification_col,
                    "value": f"evidence {index}",
                    "confidence": None,
                    "justification": None,
                },
            ]
        )
    write_claimed_test_results(project, run_id, batch)
    RunResultStore(project).point_column_at_run(op_id, risk_col, run_id)
    RunResultStore(project).point_column_at_run(op_id, tone_col, run_id)
    RunResultStore(project).point_column_at_run(op_id, justification_col, run_id)
    return client, project_id, row_ids


def test_review_bundles_return_bounded_pages_with_action_metadata(
    tmp_path: Path,
) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)

    count_response = client.get(f"/api/projects/{project_id}/review/count")
    assert count_response.status_code == 200, count_response.text
    assert count_response.json() == {"count": len(row_ids) * 2}

    response = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={"offset": 0, "limit": 25},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_no_recipe_keys(body)
    assert body["schema_version"] == "frisket.review_bundles_page.v1"
    assert body["offset"] == 0
    assert body["limit"] == 25
    assert body["total"] == len(row_ids)
    assert body["has_more"] is True
    assert body["next_offset"] == 25
    assert len(body["bundles"]) == 25

    first = body["bundles"][0]
    assert first["row_id"] == row_ids[0]
    assert first["sheet_name"] == "stories"
    assert first["action_kind"] == "map.classify"
    assert first["action_name"] == "Classify rows"
    assert first["model"] == "anthropic/claude-haiku-4-5"
    assert first["source"] == {"story": "Story 00"}
    assert {field["column_name"] for field in first["fields"]} == {"risk", "tone"}
    assert {field["column_type"] for field in first["fields"]} == {"text"}
    assert {item["column_name"] for item in first["evidence"]} == {"risk_justification"}
    assert {item["column_type"] for item in first["evidence"]} == {"text"}
    assert all(field["chore"] is True for field in first["fields"])
    assert all(item["chore"] is False for item in first["evidence"])

    tail = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={"offset": 60, "limit": 25},
    )
    assert tail.status_code == 200, tail.text
    tail_body = tail.json()
    assert tail_body["offset"] == 60
    assert tail_body["limit"] == 25
    assert tail_body["total"] == len(row_ids)
    assert tail_body["has_more"] is False
    assert tail_body["next_offset"] is None
    assert [bundle["row_id"] for bundle in tail_body["bundles"]] == row_ids[60:]


def test_review_bundle_page_params_are_validated(tmp_path: Path) -> None:
    client, project_id, _row_ids = _seed_review_history(tmp_path)
    for params in (
        {"offset": -1, "limit": 25},
        {"offset": 0, "limit": 0},
        {"offset": 0, "limit": 101},
    ):
        response = client.get(
            f"/api/projects/{project_id}/review/bundles",
            params=params,
        )
        assert response.status_code == 422, response.text

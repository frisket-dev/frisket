from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.runner.review import queue_count, review_bundle_page
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.value_codec import PreparedContentRef
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
        params={
            "input_columns": ["story"],
            "fields": [{"name": "risk"}, {"name": "tone"}],
        },
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
    RunResultStore(project).finish_run(run_id, "completed")
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


def test_review_bundle_hydrates_prepared_result_to_ordinary_text(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "prepared-review-ws"))
    project_id = client.post("/api/projects", json={"name": "Prepared review"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("documents")
    source_col = project.add_column(sheet_id, "document")
    output_col = project.add_column(sheet_id, "prepared", ai_generated=True)
    [row_id] = project.add_rows(
        sheet_id, [{"document": "source.pdf"}], {"document": source_col}
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        page_count=1,
    )
    op_id = project.append_op(
        "map", {"action_kind": "media.ocr"}, label="Prepare document"
    )
    project.db.execute("BEGIN IMMEDIATE")
    ref = PreparedContentStore(project).stage_reference(
        source_artifact_id=artifact["id"],
        producing_op_id=op_id,
        pages=[PreparedPageDraft(page_number=1, text="ordinary review text")],
    )
    project.db.commit()
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "media.ocr",
        params={"input_columns": ["document"], "fields": [{"name": "prepared"}]},
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_col,
                "value": PreparedContentRef(ref.ref_id),
                "confidence": 0.9,
            }
        ],
    )
    RunResultStore(project).point_column_at_run(op_id, output_col, run_id)
    RunResultStore(project).finish_run(run_id, "completed")

    stored = project.db.execute(
        "SELECT value_kind,value FROM results WHERE run_id=?", (run_id,)
    ).fetchone()
    assert (stored["value_kind"], stored["value"]) == (
        "prepared_content_ref",
        ref.ref_id,
    )
    response = client.get(
        f"/api/projects/{project_id}/review/bundles", params={"run_id": run_id}
    )

    assert response.status_code == 200, response.text
    [bundle] = response.json()["bundles"]
    [field] = bundle["fields"]
    assert field["value"] == "ordinary review text"
    assert isinstance(field["value"], str)


def test_review_bundle_page_batches_source_reads_for_all_rows(
    tmp_path: Path, monkeypatch
) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)
    project = client.app.state.workspace.get(project_id)
    run_id = int(project.db.execute("SELECT id FROM runs").fetchone()[0])
    story_id = int(
        project.db.execute("SELECT id FROM columns WHERE name='story'").fetchone()[0]
    )
    original_columns = project.columns
    original_get_values = project.get_values
    column_calls: list[int] = []
    value_calls: list[tuple[int, int, tuple[int, ...]]] = []

    def columns(sheet_id: int, include_hidden: bool = False):
        column_calls.append(sheet_id)
        return original_columns(sheet_id, include_hidden)

    def get_values(
        sheet_id: int,
        column_id: int,
        row_ids: list[int] | None = None,
        **kwargs: Any,
    ):
        value_calls.append((sheet_id, column_id, tuple(row_ids or ())))
        return original_get_values(sheet_id, column_id, row_ids, **kwargs)

    monkeypatch.setattr(project, "columns", columns)
    monkeypatch.setattr(project, "get_values", get_values)

    page = review_bundle_page(project, run_id=run_id, limit=25)

    sheet_id = int(page["bundles"][0]["sheet_id"])
    assert column_calls == [sheet_id]
    assert value_calls == [(sheet_id, story_id, tuple(row_ids[:25]))]
    assert [bundle["source"] for bundle in page["bundles"]] == [
        {"story": f"Story {index:02d}"} for index in range(25)
    ]


def test_selected_run_keeps_frozen_hidden_and_renamed_outputs(tmp_path: Path) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)
    project = client.app.state.workspace.get(project_id)
    run_id = int(project.db.execute("SELECT id FROM runs").fetchone()[0])
    initial = review_bundle_page(project, run_id=run_id, limit=100)
    assert initial["total"] == len(row_ids)
    risk_id = next(
        int(row["id"])
        for row in project.db.execute("SELECT id,name FROM columns")
        if row["name"] == "risk"
    )
    support_id = next(
        int(row["id"])
        for row in project.db.execute("SELECT id,name FROM columns")
        if row["name"] == "risk_justification"
    )

    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[0],))
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (risk_id,))
    project.db.execute(
        "UPDATE columns SET name='renamed_primary' WHERE id=?", (support_id,)
    )
    project.db.commit()

    stable = review_bundle_page(project, run_id=run_id, limit=100)
    assert stable["total"] == len(row_ids)
    first = next(item for item in stable["bundles"] if item["row_id"] == row_ids[0])
    assert {item["column_name"] for item in first["fields"]} == {"risk", "tone"}
    assert {item["column_name"] for item in first["evidence"]} == {"risk_justification"}


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


def test_review_runs_report_decisions_and_persist_workflow_status(
    tmp_path: Path,
) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)
    project = client.app.state.workspace.get(project_id)
    run_id = int(project.db.execute("SELECT id FROM runs").fetchone()[0])
    columns = {
        row["name"]: int(row["id"])
        for row in project.db.execute("SELECT id, name FROM columns").fetchall()
    }
    store = RunResultStore(project)
    store.set_result_review_state(run_id, row_ids[0], columns["risk"], "verified")
    store.set_result_review_metadata(
        run_id, row_ids[0], columns["risk"], "accept", None
    )
    store.set_result_review_state(run_id, row_ids[0], columns["tone"], "verified")
    store.set_result_review_metadata(run_id, row_ids[0], columns["tone"], "edit", None)
    # Old reviewed rows have no durable accept-vs-edit fact. They stay in the
    # eligible total, but not in known decisions or pending work.
    store.set_result_review_state(run_id, row_ids[1], columns["risk"], "verified")

    response = client.get(
        f"/api/projects/{project_id}/review/runs",
        params={"run_id": run_id, "limit": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == "frisket.review_runs_page.v1"
    assert body["total"] == 1
    [run] = body["runs"]
    assert run["review_status"] == "open"
    assert run["total"] == {
        "eligible_count": 128,
        "reviewed_count": 2,
        "accepted_count": 1,
        "incorrect_count": 1,
        "unreviewed_count": 125,
        "confidence_count": 128,
    }
    assert {field["column_name"] for field in run["fields"]} == {"risk", "tone"}

    reviewed_page = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={
            "run_id": run_id,
            "field_id": columns["risk"],
            "include_reviewed": True,
        },
    ).json()
    assert any(bundle["row_id"] == row_ids[1] for bundle in reviewed_page["bundles"])

    completed = client.post(
        f"/api/projects/{project_id}/review/runs/{run_id}/status",
        json={"status": "complete"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"
    assert completed.json()["review_completed_at"] is not None
    reopened = client.post(
        f"/api/projects/{project_id}/review/runs/{run_id}/status",
        json={"status": "open"},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["review_completed_at"] is None


def test_review_run_picker_excludes_current_running_runs(tmp_path: Path) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)
    project = client.app.state.workspace.get(project_id)
    sheet_id = int(project.db.execute("SELECT id FROM sheets").fetchone()[0])
    risk_id = int(
        project.db.execute("SELECT id FROM columns WHERE name='risk'").fetchone()[0]
    )
    op_id = project.append_op("map", {"action_kind": "map.classify"})
    running_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        params={"input_columns": ["story"]},
        total_rows=1,
        row_ids=[row_ids[0]],
    )
    write_claimed_test_results(
        project,
        running_id,
        [{"row_id": row_ids[0], "column_id": risk_id, "value": "new"}],
    )
    RunResultStore(project).point_column_at_run(op_id, risk_id, running_id)

    while_running = client.get(f"/api/projects/{project_id}/review/runs")
    assert while_running.status_code == 200, while_running.text
    assert running_id not in {
        int(item["run_id"]) for item in while_running.json()["runs"]
    }
    assert queue_count(project) == len(row_ids) * 2

    RunResultStore(project).finish_run(running_id, "completed")
    finished = client.get(f"/api/projects/{project_id}/review/runs")
    assert finished.status_code == 200, finished.text
    assert running_id in {int(item["run_id"]) for item in finished.json()["runs"]}
    assert queue_count(project) == len(row_ids) * 2 + 1


def test_review_bundle_row_cursor_pages_without_offset_rescans(tmp_path: Path) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)
    project = client.app.state.workspace.get(project_id)
    run_id = int(project.db.execute("SELECT id FROM runs").fetchone()[0])

    first = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={"run_id": run_id, "order": "row", "limit": 25},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert [item["row_id"] for item in first_body["bundles"]] == row_ids[:25]
    assert first_body["has_more"] is True
    assert first_body["next_offset"] is None
    assert first_body["next_cursor"] == row_ids[24]

    second = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={
            "run_id": run_id,
            "order": "row",
            "cursor": first_body["next_cursor"],
            "limit": 25,
        },
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [item["row_id"] for item in second_body["bundles"]] == row_ids[25:50]
    assert second_body["next_cursor"] == row_ids[49]

    tail = client.get(
        f"/api/projects/{project_id}/review/bundles",
        params={
            "run_id": run_id,
            "order": "row",
            "cursor": second_body["next_cursor"],
            "limit": 25,
        },
    )
    assert tail.status_code == 200, tail.text
    tail_body = tail.json()
    assert [item["row_id"] for item in tail_body["bundles"]] == row_ids[50:]
    assert tail_body["has_more"] is False
    assert tail_body["next_cursor"] is None


def test_random_review_batches_and_exact_history(tmp_path: Path) -> None:
    client, project_id, row_ids = _seed_review_history(tmp_path)
    project = client.app.state.workspace.get(project_id)
    run_id = int(project.db.execute("SELECT id FROM runs").fetchone()[0])
    url = f"/api/projects/{project_id}/review/bundles"
    request = {
        "kind": "sample",
        "run_id": run_id,
        "include_reviewed": True,
        "limit": 25,
    }
    first = client.post(url, json=request)
    assert first.status_code == 200, first.text
    body = first.json()
    shown = [item["row_id"] for item in body["bundles"]]
    assert len(shown) == len(set(shown)) == 25
    assert body["total"] == len(row_ids)
    assert body["has_more"] is True
    second = client.post(url, json={**request, "exclude_row_ids": shown})
    assert second.status_code == 200, second.text
    next_ids = [item["row_id"] for item in second.json()["bundles"]]
    assert len(next_ids) == 25
    assert not set(shown) & set(next_ids)
    tail = client.post(url, json={**request, "exclude_row_ids": shown + next_ids})
    assert tail.status_code == 200, tail.text
    assert len(tail.json()["bundles"]) == 14
    assert tail.json()["has_more"] is False
    risk = int(
        project.db.execute("SELECT id FROM columns WHERE name='risk'").fetchone()[0]
    )
    store = RunResultStore(project)
    store.set_result_review_state(run_id, shown[0], risk, "verified")
    store.set_result_review_metadata(run_id, shown[0], risk, "accept", "Checked")
    exact = client.post(
        url,
        json={
            "kind": "rows",
            "run_id": run_id,
            "include_reviewed": True,
            "row_ids": shown[::-1],
        },
    )
    assert exact.status_code == 200, exact.text
    assert [item["row_id"] for item in exact.json()["bundles"]] == shown[::-1]
    field = next(
        item
        for item in exact.json()["bundles"][-1]["fields"]
        if item["column_id"] == risk
    )
    assert field["review_decision"] == "accept"
    _assert_no_recipe_keys(exact.json())


def test_review_batch_request_validation(tmp_path: Path) -> None:
    client, project_id, _ = _seed_review_history(tmp_path)
    url = f"/api/projects/{project_id}/review/bundles"
    for body in (
        {"kind": "sample", "run_id": 0},
        {"kind": "sample", "run_id": 1, "limit": 101},
        {"kind": "sample", "run_id": 1, "exclude_row_ids": [-1]},
        {"kind": "rows", "run_id": 1, "row_ids": []},
        {"kind": "rows", "run_id": 1, "row_ids": list(range(1, 102))},
        {"kind": "rows", "run_id": 1, "row_ids": [1], "exclude_row_ids": []},
    ):
        response = client.post(url, json=body)
        assert response.status_code == 422, response.text

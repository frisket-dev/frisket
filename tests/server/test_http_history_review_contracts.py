"""Behavioral HTTP contracts for the lean history/review read surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.route_errors import RouteError, register_route_error_handler
from frisket.server.routes.sheet_features import register_column_run_history_routes
from frisket.server.routes.project_research import (
    register_project_entity_review_routes,
)
from frisket.server.routes.project_inspection import register_project_history_routes
from frisket.server.app import create_app


class _HistoryService:
    def __init__(self) -> None:
        self.call: tuple[str, int | None, int] | None = None

    def history(
        self, project_id: str, *, offset: int | None, limit: int
    ) -> dict[str, Any]:
        self.call = (project_id, offset, limit)
        return {
            "schema_version": "frisket.history_page.v1",
            "order": "asc",
            "offset": 0,
            "limit": limit,
            "total": 0,
            "has_more_before": False,
            "has_more_after": False,
            "prev_offset": None,
            "next_offset": None,
            "cursor_index": -1,
            "cursor_op": None,
            "cursor_op_loaded": False,
            "undo_target": None,
            "redo_target": None,
            "revision": {"total": 0, "max_op_id": 0, "op_cursor": 0},
            "ops": [],
        }


class _ColumnRunsService:
    def column_runs(
        self, project_id: str, column_id: int, *, offset: int, limit: int
    ) -> dict[str, Any]:
        if column_id == 404:
            raise RouteError(404, "no such column")
        run = {
            "run_id": 9,
            "action_kind": "map.classify",
            "action_name": "Classify rows",
            "model": None,
            "status": "completed",
            "spec": {"producer_extension": {"preserved": [1, True]}},
            "total_rows": 2,
            "completed_rows": 2,
            "failed_rows": 0,
            "cost_actual": 0.0,
            "started_at": "2026-08-11T00:00:00Z",
            "finished_at": "2026-08-11T00:00:01Z",
            "duration_ms": 1000,
            "tokens_in": None,
            "tokens_out": None,
            "current": True,
            "human_score": {"passed": 1, "graded": 2},
            "judge_scores": [],
        }
        older = {**run, "run_id": 7, "current": False}
        return {
            "column": {
                "id": column_id,
                "name": "risk",
                "type": "text",
                "ai_generated": True,
                "current_run_id": 9,
                "latest_run_id": 9,
                "mixed_origins": False,
            },
            "offset": offset,
            "limit": limit,
            "total": 2,
            "has_more": False,
            "next_offset": None,
            "current_run": run,
            "current_run_loaded": True,
            "latest_run": run,
            "latest_run_loaded": True,
            "runs": [run, older],
        }

    def reviewed_run_revision(
        self, project_id: str, column_id: int, run_id: int
    ) -> dict[str, Any]:
        return {
            "schema_version": "frisket.reviewed_run_revision.v1",
            "source_run_id": run_id,
            "reviewed_rows": 2,
            "draft": {
                "action_id": "map.classify",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": 8,
                    "row_ids": [3, 5],
                },
                "params": {"labels": ["yes", "no"]},
                "output_names": {"label": "risk"},
            },
        }


class _ReviewService:
    def review_bundles(
        self,
        project_id: str,
        *,
        sheet_id: int | None,
        offset: int,
        limit: int,
        run_id: int | None,
        include_reviewed: bool,
    ) -> dict[str, Any]:
        item = {
            "run_id": 9,
            "row_id": 3,
            "column_id": 4,
            "column_name": "risk",
            "column_type": "text",
            "sheet_id": sheet_id or 1,
            "value": "high",
            "confidence": 0.9,
            "justification": "observed",
            "error": None,
            "review_state": "unreviewed",
            "review_decision": None,
            "review_note": None,
            "role": "field",
            "chore": True,
        }
        evidence = {
            **item,
            "column_id": 5,
            "column_name": "risk_evidence",
            "error": "citation unavailable",
            "role": "evidence",
            "chore": False,
        }
        return {
            "schema_version": "frisket.review_bundles_page.v1",
            "offset": offset,
            "limit": limit,
            "total": 1,
            "has_more": False,
            "next_offset": None,
            "bundles": [
                {
                    "id": "9:3",
                    "run_id": 9,
                    "row_id": 3,
                    "sheet_id": sheet_id or 1,
                    "sheet_name": "stories",
                    "action_kind": "map.classify",
                    "action_name": "Classify rows",
                    "model": None,
                    "confidence": 0.9,
                    "source": {"story": "alpha"},
                    "fields": [item],
                    "evidence": [evidence],
                    "items": [item, evidence],
                }
            ],
        }

    def review_count(
        self, project_id: str, *, run_id: int | None = None
    ) -> dict[str, int]:
        if project_id == "missing":
            raise RouteError(404, "no such project")
        return {"count": 2}

    def entities(self, project_id: str) -> list[dict[str, Any]]:
        return []

    def review_queue(
        self, project_id: str, *, sheet_id: int | None, run_id: int | None = None
    ) -> list[dict[str, Any]]:
        return []


def _client() -> tuple[TestClient, _HistoryService]:
    app = FastAPI()
    register_route_error_handler(app)
    history = _HistoryService()
    register_project_history_routes(app, service=history)  # type: ignore[arg-type]
    register_column_run_history_routes(app, service=_ColumnRunsService())  # type: ignore[arg-type]
    register_project_entity_review_routes(app, service=_ReviewService())  # type: ignore[arg-type]
    return TestClient(app), history


def test_history_review_contracts_preserve_bytes_omissions_and_open_json() -> None:
    client, history_service = _client()

    history = client.get("/api/projects/p/history")
    assert history.status_code == 200, history.text
    assert history_service.call == ("p", None, 50)
    assert history.json() == {
        "schema_version": "frisket.history_page.v1",
        "order": "asc",
        "offset": 0,
        "limit": 50,
        "total": 0,
        "has_more_before": False,
        "has_more_after": False,
        "prev_offset": None,
        "next_offset": None,
        "cursor_index": -1,
        "cursor_op": None,
        "cursor_op_loaded": False,
        "undo_target": None,
        "redo_target": None,
        "revision": {"total": 0, "max_op_id": 0, "op_cursor": 0},
        "ops": [],
    }

    runs = client.get("/api/projects/p/columns/12/runs", params={"limit": 2})
    assert runs.status_code == 200, runs.text
    run_body = runs.json()
    assert [run["run_id"] for run in run_body["runs"]] == [9, 7]
    assert run_body["column"]["latest_run_id"] == 9
    assert run_body["column"]["mixed_origins"] is False
    assert run_body["latest_run"]["run_id"] == 9
    assert run_body["runs"][0]["spec"] == {
        "producer_extension": {"preserved": [1, True]}
    }
    assert run_body["runs"][0]["human_score"] == {"passed": 1, "graded": 2}

    revision = client.get("/api/projects/p/columns/12/runs/9/revision")
    assert revision.status_code == 200, revision.text
    assert revision.json()["draft"]["scope"]["row_ids"] == [3, 5]
    assert "idempotency_key" not in revision.json()["draft"]

    bundles = client.get(
        "/api/projects/p/review/bundles", params={"sheet_id": 8, "limit": 1}
    )
    assert bundles.status_code == 200, bundles.text
    bundle = bundles.json()["bundles"][0]
    assert [item["role"] for item in bundle["fields"]] == ["field"]
    assert bundle["evidence"][0]["error"] == "citation unavailable"
    assert [item["column_id"] for item in bundle["items"]] == [4, 5]
    assert bundle["fields"][0]["review_note"] is None
    assert client.get("/api/projects/p/review/count").json() == {"count": 2}


def test_history_review_contracts_retain_query_bounds_error_envelopes_and_openapi() -> (
    None
):
    client, _history_service = _client()

    assert client.get("/api/projects/p/history", params={"limit": 0}).status_code == 422
    assert (
        client.get("/api/projects/p/columns/12/runs", params={"offset": -1}).status_code
        == 422
    )
    assert (
        client.get("/api/projects/p/review/bundles", params={"limit": 101}).status_code
        == 422
    )
    missing_column = client.get("/api/projects/p/columns/404/runs")
    assert missing_column.status_code == 404
    assert missing_column.json() == {"detail": "no such column"}
    missing_project = client.get("/api/projects/missing/review/count")
    assert missing_project.status_code == 404
    assert missing_project.json() == {"detail": "no such project"}

    document = client.app.openapi()
    for path, operation in (
        ("/api/projects/{pid}/history", "get"),
        ("/api/projects/{pid}/columns/{column_id}/runs", "get"),
        ("/api/projects/{pid}/columns/{column_id}/runs/{run_id}/revision", "get"),
        ("/api/projects/{pid}/review/bundles", "get"),
        ("/api/projects/{pid}/review/count", "get"),
    ):
        responses = document["paths"][path][operation]["responses"]
        assert set(responses) >= {"200", "401", "403", "404", "422", "500"}


def test_history_contract_preserves_an_ordinary_unlabeled_persisted_operation(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Unlabeled history operation"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    op_id = project.append_op("edit", {"cell": "unchanged"})

    response = client.get(f"/api/projects/{project_id}/history")

    assert response.status_code == 200, response.text
    assert response.json()["ops"] == [
        {
            "id": op_id,
            "index": 0,
            "kind": "edit",
            "label": None,
            "status": "applied",
            "barrier": False,
            "at_cursor": True,
            "created_at": response.json()["ops"][0]["created_at"],
            "run": None,
        }
    ]

from __future__ import annotations

import gzip
import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store.runs import RunResultStore
from frisket.operability.trace import trace_path
from helpers import write_claimed_test_results


ROOT = Path(__file__).resolve().parents[2]
CSV = 'snippet\n"alpha"\n"beta"\n'


def _write_trace(path: Path, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(text)


def _seed_completed_run_with_failed_row(client: TestClient) -> tuple[str, int, int]:
    project_id = client.post("/api/projects", json={"name": "Action rows"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("rows.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    project = client.app.state.workspace.get(project_id)
    rows = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
    ).fetchall()
    output_col = project.add_column(sheet_id, "topic", ai_generated=True)
    row_ids = [int(row["id"]) for row in rows]
    op_id = project.append_op("map", {"action_kind": "map.classify"}, "seed rows")
    run_store = RunResultStore(project)
    run_id = run_store.start_run(
        op_id,
        sheet_id,
        "map.classify",
        model="anthropic/test",
        params={"row_ids": row_ids},
        total_rows=len(row_ids),
        row_ids=row_ids,
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_col,
                "value": "ok",
                "tokens_in": 3,
                "tokens_out": 1,
            },
            {
                "row_id": row_ids[1],
                "column_id": output_col,
                "error": "provider 429: retry budget exhausted",
                "tokens_in": 4,
                "tokens_out": 0,
            },
        ],
    )
    run_store.finish_run(run_id)
    sidecar = trace_path(project.path, run_id)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(sidecar, "wt", encoding="utf-8") as stream:
        stream.write(
            "\n".join(
                [
                    json.dumps(
                        {
                            "kind": "meta",
                            "run_id": run_id,
                            "trace_id": "trace-rows",
                            "action_kind": "map.classify",
                            "model": "anthropic/test",
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": row_ids[1],
                            "trace_id": "trace-rows",
                            "error": "provider 429",
                            "retries": [
                                {
                                    "event": "attempt",
                                    "status": 429,
                                    "retryable": True,
                                    "error": "rate limited",
                                }
                            ],
                        }
                    ),
                ]
            )
            + "\n"
        )
    project.db.commit()
    return project_id, run_id, row_ids[1]


def test_action_run_rows_route_exposes_error_rows_with_action_metadata(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id, run_id, failed_row_id = _seed_completed_run_with_failed_row(client)

    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/rows?status=error"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert "run_id" not in body and "status" not in body
    assert "recipe" not in json.dumps(body).lower()
    assert body["run"] == {
        "id": run_id,
        "action_kind": "map.classify",
        "action_name": "Classify rows",
        "status": "completed",
        "total_rows": 2,
        "completed_rows": 2,
        "failed_rows": 1,
    }
    assert body["total"] == 1
    assert body["rows"][0]["row_id"] == failed_row_id
    assert body["rows"][0]["status"] == "error"
    assert body["rows"][0]["error"] == "provider 429: retry budget exhausted"
    assert body["rows"][0]["retry_count"] == 1
    assert body["rows"][0]["retries"][0]["status"] == 429

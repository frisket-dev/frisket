from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import frisket.server.run_payloads as run_payloads
from frisket.server.app import create_app
from frisket.engine.store.runs import RunResultStore
from frisket.operability.trace import trace_path


CSV = 'snippet\n"alpha"\n"beta"\n"gamma"\n'


def _write_trace(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _seed_run_with_trace_only_error(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": "Trace page"}).json()["id"]
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
    row_ids = [int(row["id"]) for row in rows]
    output_col = project.add_column(sheet_id, "topic", ai_generated=True)
    project.db.execute("INSERT INTO ops (kind, label) VALUES ('run', 'seed rows')")
    op_id = project.db.execute("SELECT MAX(id) AS m FROM ops").fetchone()["m"]
    cursor = project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind, model, params, status, "
        "total_rows, completed_rows, failed_rows, cost_actual) "
        "VALUES (?, ?, 'map.classify', 'anthropic/test', ?, 'completed', 3, 2, 1, 0.01)",
        (op_id, sheet_id, json.dumps({"row_ids": row_ids})),
    )
    run_id = int(cursor.lastrowid)
    project.db.executemany(
        "INSERT INTO results (run_id, row_id, column_id, value, tokens_in, "
        "tokens_out, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, row_ids[0], output_col, json.dumps("ok"), 3, 1, None),
            (run_id, row_ids[2], output_col, json.dumps("ok"), 5, 1, None),
        ],
    )
    sidecar = trace_path(project.path, run_id)
    _write_trace(
        sidecar,
        [
            {
                "kind": "meta",
                "run_id": run_id,
                "trace_id": "trace-page",
                "action_kind": "map.classify",
                "model": "anthropic/test",
            },
            {
                "kind": "row",
                "row_id": row_ids[1],
                "trace_id": "trace-page",
                "error": "provider 429",
                "retries": [
                    {
                        "event": "attempt",
                        "status": 429,
                        "retryable": True,
                        "error": "rate limited",
                    }
                ],
            },
        ],
    )
    project.db.commit()
    return project_id, run_id, row_ids


def _seed_run_with_trace_edge_cases(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": "Trace edge"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("rows.csv", 'snippet\n"a"\n"b"\n"c"\n"d"\n', "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    project = client.app.state.workspace.get(project_id)
    rows = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
    ).fetchall()
    row_ids = [int(row["id"]) for row in rows]
    output_col = project.add_column(sheet_id, "topic", ai_generated=True)
    project.db.execute("INSERT INTO ops (kind, label) VALUES ('run', 'edge rows')")
    op_id = project.db.execute("SELECT MAX(id) AS m FROM ops").fetchone()["m"]
    cursor = project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind, model, params, status, "
        "total_rows, completed_rows, failed_rows, cost_actual) "
        "VALUES (?, ?, 'map.classify', 'anthropic/test', ?, 'completed', 3, 2, 1, 0.01)",
        (op_id, sheet_id, json.dumps({"row_ids": row_ids[:3]})),
    )
    run_id = int(cursor.lastrowid)
    # The run's row scope (row_ids[3] deliberately excluded, below) is
    # recorded through the current run_rows/run_scopes mechanism, the same
    # way a real partial run does it — not through the params blob.
    RunResultStore(project).record_run_row_scope(run_id, row_ids[:3])
    project.db.commit()
    # `outcome` (not `error`) is the single source of truth for cell failure
    # classification. This fixture writes results directly via SQL rather than through
    # RunResultStore.write_results, so it must set outcome itself the same
    # way that producer derives it (error -> model_error, value -> ok).
    project.db.executemany(
        "INSERT INTO results (run_id, row_id, column_id, value, tokens_in, "
        "tokens_out, error, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, row_ids[0], output_col, json.dumps("ok"), 3, 1, None, "ok"),
            (
                run_id,
                row_ids[2],
                output_col,
                None,
                4,
                0,
                "db provider error",
                "model_error",
            ),
        ],
    )
    sidecar = trace_path(project.path, run_id)
    _write_trace(
        sidecar,
        [
            {
                "kind": "meta",
                "run_id": run_id,
                "trace_id": "trace-edge",
                "action_kind": "map.classify",
                "model": "anthropic/test",
            },
            {
                "kind": "row",
                "row_id": row_ids[1],
                "trace_id": "trace-edge",
                "error": "transient trace error",
            },
            {
                "kind": "row",
                "row_id": row_ids[1],
                "trace_id": "trace-edge",
                "retries": [{"status": 200, "retryable": False}],
            },
            {
                "kind": "row",
                "row_id": row_ids[2],
                "trace_id": "trace-edge",
                "prompt": "large prompt that run rows must not retain",
                "raw_response": "large response that run rows must not retain",
                "retries": [
                    {
                        "event": "attempt",
                        "status": 429,
                        "retryable": True,
                        "error": "rate limited",
                    }
                ],
            },
            {
                "kind": "row",
                "row_id": row_ids[3],
                "trace_id": "trace-edge",
                "error": "out of run scope",
            },
        ],
    )
    project.db.commit()
    return project_id, run_id, row_ids


def test_action_run_rows_ignores_trace_only_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id, run_id, row_ids = _seed_run_with_trace_only_error(client)
    project = client.app.state.workspace.get(project_id)

    def fail_full_trace_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("action_run_rows_payload must not call read_trace()")

    monkeypatch.setattr(run_payloads, "read_trace", fail_full_trace_load, raising=False)

    ordinary = run_payloads.action_run_rows_payload(
        project,
        run_id,
        offset=1,
        limit=1,
    )
    assert ordinary["total"] == 3
    assert ordinary["rows"][0]["row_id"] == row_ids[1]
    assert ordinary["rows"][0]["status"] == "not_run"
    assert ordinary["rows"][0]["error"] is None
    assert ordinary["rows"][0]["retry_count"] == 1
    assert ordinary["rows"][0]["retries"][0]["status"] == 429

    direct = run_payloads.action_run_rows_payload(project, run_id, status="error")
    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/rows?status=error"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body == direct
    assert body["total"] == 0
    assert body["rows"] == []


def test_action_run_rows_uses_durable_results_not_trace_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id, run_id, row_ids = _seed_run_with_trace_edge_cases(client)
    project = client.app.state.workspace.get(project_id)

    def fail_full_trace_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("action_run_rows_payload must not call read_trace()")

    monkeypatch.setattr(run_payloads, "read_trace", fail_full_trace_load, raising=False)

    body = run_payloads.action_run_rows_payload(project, run_id, status="error")
    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/rows?status=error"
    )
    assert response.status_code == 200, response.text
    assert response.json() == body

    assert body["total"] == 1
    assert [row["row_id"] for row in body["rows"]] == [row_ids[2]]
    row = body["rows"][0]
    assert row["status"] == "error"
    assert row["error"] == "db provider error"
    assert row["retry_count"] == 1
    assert row["retries"][0]["status"] == 429
    assert "large prompt" not in json.dumps(row)
    assert "large response" not in json.dumps(row)

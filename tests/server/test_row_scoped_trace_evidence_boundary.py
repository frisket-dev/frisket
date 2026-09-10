from __future__ import annotations

import json
import gzip
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.operability.trace import trace_path
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _seed_project(tmp_path: Path) -> tuple[TestClient, str, int, list[int], int, int]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Trace row"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("events")
    story_col = project.add_column(sheet_id, "story")
    topic_col = project.add_column(sheet_id, "topic", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [{"story": "alpha"}, {"story": "beta"}, {"story": "gamma"}],
        {"story": story_col},
    )
    op_id = project.append_op("map", {"action_kind": "map.classify"}, label="classify")
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        model="anthropic/test",
        params={"action_kind": "map.classify", "sheet_id": sheet_id},
        total_rows=2,
        row_ids=row_ids[:2],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {"row_id": row_ids[0], "column_id": topic_col, "value": "a"},
            {"row_id": row_ids[1], "column_id": topic_col, "value": "b"},
        ],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, topic_col, run_id)

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
                            "trace_id": "trace-row-scope",
                            "action_kind": "map.classify",
                            "model": "anthropic/test",
                            "created_at": "2026-06-19T00:00:00Z",
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": row_ids[0],
                            "trace_id": "trace-row-scope",
                            "prompt": [{"role": "user", "content": "alpha prompt"}],
                            "raw_response": '{"topic":"a"}',
                            "tokens_in": 7,
                            "tokens_out": 3,
                            "latency_ms": 12,
                            "retries": [],
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": row_ids[0],
                            "trace_id": "trace-row-scope",
                            "prompt": [
                                {"role": "user", "content": "alpha prompt resumed"}
                            ],
                            "raw_response": '{"topic":"a-latest"}',
                            "tokens_in": 8,
                            "tokens_out": 4,
                            "latency_ms": 9,
                            "retries": [],
                        }
                    ),
                    "",
                ]
            )
        )
    return client, project_id, run_id, row_ids, topic_col, sheet_id


def test_action_run_trace_row_endpoint_returns_typed_row_scoped_evidence(
    tmp_path: Path,
) -> None:
    client, project_id, run_id, row_ids, topic_col, _sheet_id = _seed_project(tmp_path)

    recorded = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/trace/rows/{row_ids[0]}",
        params={"column_id": topic_col},
    )
    missing = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/trace/rows/{row_ids[1]}",
        params={"column_id": topic_col},
    )
    outside_scope = client.get(
        f"/api/projects/{project_id}/actions/runs/{run_id}/trace/rows/{row_ids[2]}",
        params={"column_id": topic_col},
    )

    project = client.app.state.workspace.get(project_id)
    untraced_op = project.append_op(
        "map", {"action_kind": "map.classify"}, label="untraced"
    )
    untraced_run_id = RunResultStore(project).start_run(
        untraced_op,
        project.db.execute(
            "SELECT sheet_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()["sheet_id"],
        "map.classify",
        total_rows=1,
        row_ids=[row_ids[0]],
    )
    RunResultStore(project).finish_run(untraced_run_id)
    not_recorded = client.get(
        f"/api/projects/{project_id}/actions/runs/{untraced_run_id}/trace/rows/{row_ids[0]}",
        params={"column_id": topic_col},
    )
    assert recorded.status_code == 200, recorded.text
    body = recorded.json()
    assert body["action"] == "run_trace_row"
    assert body["recorded"] is True
    assert body["status"] == "recorded"
    assert body["row_id"] == row_ids[0]
    assert body["column_id"] == topic_col
    assert body["trace"]["row"]["row_id"] == row_ids[0]
    assert body["trace"]["row"]["raw_response"] == '{"topic":"a-latest"}'
    assert body["trace"]["record_count"] == 2
    assert "rows" not in body["trace"], (
        "row-scoped lookup must not serialize full trace"
    )
    assert body["trace"]["action_kind"] == "map.classify"
    assert body["trace"]["action_name"] == "Classify rows"

    assert missing.status_code == 200, missing.text
    assert missing.json()["recorded"] is False
    assert missing.json()["status"] == "missing"
    assert missing.json()["trace"] is None

    assert outside_scope.status_code == 200, outside_scope.text
    assert outside_scope.json()["recorded"] is False
    assert outside_scope.json()["status"] == "row_not_in_run"
    assert outside_scope.json()["trace"] is None

    assert not_recorded.status_code == 200, not_recorded.text
    assert not_recorded.json()["recorded"] is False
    assert not_recorded.json()["status"] == "not_recorded"
    assert not_recorded.json()["trace"] is None
    # Even without a trace, the run-level facts are populated so
    # "Explain this cell" can show non-LLM provenance (action/status/timing/
    # cost) instead of a dead-end message.
    nr_run = not_recorded.json()["run"]
    assert nr_run["action_name"]
    assert nr_run["status"]
    assert "started_at" in nr_run
    assert "finished_at" in nr_run
    assert "cost_actual" in nr_run

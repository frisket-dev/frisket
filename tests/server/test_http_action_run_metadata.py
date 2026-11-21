"""HTTP provenance, column-run history, and action-trace metadata payloads."""

from __future__ import annotations

import json
from datetime import datetime

from action_test_helpers import typed_map_request
from frisket.contracts.action import ActionResult
from helpers import make_client
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation
from model_doubles import canned_reply_router

CALLS_CSV = "note\nCall 212-555-0123\nNo phone\n"
STORIES_CSV = (
    "story\n"
    "Mayor Linda Reyes met lobbyist Tom Quayle before the vote.\n"
    "Transit riders protested a weekend bus cut.\n"
)


def _seed_regex_run(
    client, *, name: str, idempotency_key: str
) -> tuple[str, int, ActionResult]:
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CALLS_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    run = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=typed_map_request(
            "map.regex_extract",
            sheet_id,
            params={"input_columns": ["note"], "pattern": r"\d{3}-\d{3}-\d{4}"},
            output_names={"extracted": "phone"},
            idempotency_key=idempotency_key,
        ),
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)
    return project_id, sheet_id, result


def test_http_project_provenance_uses_run_kind_public_shape(client) -> None:
    project_id, sheet_id, result = _seed_regex_run(
        client,
        name="Provenance v1",
        idempotency_key="provenance-v1-regex@sha256:stable",
    )

    response = client.get(f"/api/projects/{project_id}/provenance")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["action_kinds"] == [
        {
            "action_kind": "map.regex_extract",
            "action_name": "Extract text with regex",
            "runs": 1,
            "rows": 2,
            "failed_rows": 0,
            "cost": 0.0,
        }
    ]
    assert body["runs"] == [
        {
            "run_id": result.run_id,
            "sheet_id": sheet_id,
            "action_kind": "map.regex_extract",
            "action_name": "Extract text with regex",
            "model": None,
            "provider": None,
            "status": "completed",
            "total_rows": 2,
            "completed_rows": 2,
            "failed_rows": 0,
            "cost": 0.0,
            "started_at": body["runs"][0]["started_at"],
            "finished_at": body["runs"][0]["finished_at"],
        }
    ]


def test_http_column_runs_use_action_metadata(client) -> None:
    project_id, sheet_id, result = _seed_regex_run(
        client,
        name="Column runs v1",
        idempotency_key="column-runs-v1-regex@sha256:stable",
    )

    data = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=0"
    )
    assert data.status_code == 200, data.text
    phone_column = next(c for c in data.json()["columns"] if c["name"] == "phone")

    response = client.get(
        f"/api/projects/{project_id}/columns/{phone_column['id']}/runs"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["column"] == {
        "id": phone_column["id"],
        "name": "phone",
        "type": "text",
        "ai_generated": True,
        # regex_extract is generation-managed: the wire deliberately reports
        # no scalar current run for managed columns — exact heads own value
        # authority, and latest_run_id/mixed_origins carry the chronology.
        "current_run_id": None,
        "latest_run_id": result.run_id,
        "mixed_origins": False,
    }
    assert len(body["runs"]) == 1
    run_body = body["runs"][0]
    assert set(run_body) == {
        "run_id",
        "action_kind",
        "action_name",
        "model",
        "status",
        "spec",
        "total_rows",
        "completed_rows",
        "failed_rows",
        "cost_actual",
        "started_at",
        "finished_at",
        "duration_ms",
        "tokens_in",
        "tokens_out",
        "human_score",
        "judge_scores",
        "current",
    }
    assert datetime.fromisoformat(run_body["started_at"]) <= datetime.fromisoformat(
        run_body["finished_at"]
    )
    assert run_body["duration_ms"] >= 0
    assert {
        key: value
        for key, value in run_body.items()
        if key not in {"spec", "started_at", "finished_at", "duration_ms"}
    } == {
        "run_id": result.run_id,
        "action_kind": "map.regex_extract",
        "action_name": "Extract text with regex",
        "model": None,
        "status": "completed",
        "total_rows": 2,
        "completed_rows": 2,
        "failed_rows": 0,
        "cost_actual": 0.0,
        "tokens_in": None,
        "tokens_out": None,
        "human_score": {"graded": 0, "passed": 0},
        "judge_scores": [],
        # Managed columns have no scalar "current" run — exact heads own
        # value authority — so the run-level flag stays False and the
        # column-level latest_run_id above carries the chronology.
        "current": False,
    }
    spec = run_body["spec"]
    assert {
        key: spec[key]
        for key in (
            "action_kind",
            "action_version",
            "sheet_id",
            "input_columns",
            "output_names",
            "params",
        )
    } == {
        "action_kind": "map.regex_extract",
        "action_version": "1",
        "sheet_id": sheet_id,
        "input_columns": ["note"],
        "output_names": {"extracted": "phone"},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
        },
    }


def test_action_run_trace_uses_action_metadata(tmp_path) -> None:
    client = make_client(tmp_path, router=canned_reply_router({"beat": "civic"})[0])
    project_id = client.post("/api/projects", json={"name": "Trace v1"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("stories.csv", STORIES_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    run = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        {
            "action_id": "map.classify",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["story"],
                "engine": "llm",
                "model": "anthropic/claude-haiku-4-5",
                "context": "Classify one-line local news items.",
                "fields": [
                    {
                        "name": "beat",
                        "type": "category",
                        "labels": ["civic", "transportation"],
                    }
                ],
            },
            "idempotency_key": "action-trace-v1-classify@sha256:stable",
        },
    )
    assert run.status_code == 200, run.text
    result = ActionResult.model_validate(run.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    drain_queue(client)

    response = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/trace"
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["schema_version"] == "frisket.actions.v1"
    assert body["action"] == "run_trace"
    assert body["project_id"] == project_id
    assert body["run_id"] == result.run_id
    assert body["recorded"] is True

    trace = body["trace"]
    assert trace["run_id"] == result.run_id
    assert trace["trace_id"]
    assert trace["action_kind"] == "map.classify"
    assert trace["action_name"] == "Classify rows"
    assert trace["model"] == "anthropic/claude-haiku-4-5"
    assert len(trace["rows"]) == 2
    assert {row["trace_id"] for row in trace["rows"]} == {trace["trace_id"]}
    assert any("Linda Reyes" in json.dumps(row["prompt"]) for row in trace["rows"])
    assert all(
        json.loads(row["raw_response"]) == {"beat": "civic"} for row in trace["rows"]
    )

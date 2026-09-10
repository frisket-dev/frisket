from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _seed_project(
    tmp_path: Path,
) -> tuple[TestClient, str, int, dict[str, int], list[int], int, int]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post("/api/projects", json={"name": "Cell provenance"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("events")
    source_col = project.add_column(sheet_id, "story")
    output_col = project.add_column(sheet_id, "topic", ai_generated=True)
    row_ids = project.add_rows(
        sheet_id,
        [
            {"story": "alpha"},
            {"story": "beta"},
            {"story": "gamma"},
            {"story": "delta"},
            {"story": "epsilon"},
        ],
        {"story": source_col},
    )
    op_id = project.append_op("map", {"action_kind": "map.classify"}, label="classify")
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        model="anthropic/test",
        params={
            "action_kind": "map.classify",
            "sheet_id": sheet_id,
            "fields": [{"name": "topic", "type": "text"}],
        },
        total_rows=4,
        row_ids=[row_ids[0], row_ids[1], row_ids[3], row_ids[4]],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": output_col,
                "value": "ai-alpha",
                "confidence": 0.91,
                "justification": "generated alpha",
            },
            {
                "row_id": row_ids[1],
                "column_id": output_col,
                "value": "ai-beta",
                "confidence": 0.73,
                "justification": "generated beta",
            },
            {
                "row_id": row_ids[3],
                "column_id": output_col,
                "value": None,
                "error": "model failed",
            },
            # Terminal failure (empty-output terminal-row contract): the
            # producer declares the outcome explicitly; /data must pass it
            # through so the UI can offer the deliberate "Retry anyway".
            {
                "row_id": row_ids[4],
                "column_id": output_col,
                "value": None,
                "error": "engine returned empty output",
                "outcome": "empty_output",
            },
        ],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, output_col, run_id)
    edit_op_id = project.apply_edits(
        [
            {
                "row_id": row_ids[0],
                "column_id": output_col,
                "value": "manual-alpha",
            }
        ]
    )
    project.db.commit()
    return (
        client,
        project_id,
        sheet_id,
        {"story": source_col, "topic": output_col},
        row_ids,
        run_id,
        edit_op_id,
    )


def test_sheet_data_exposes_typed_current_value_refs(tmp_path: Path) -> None:
    client, project_id, sheet_id, columns, row_ids, run_id, edit_op_id = _seed_project(
        tmp_path
    )

    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"offset": 0, "limit": 10},
    )
    negative_limit = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"offset": 0, "limit": -1},
    )
    negative_offset = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"offset": -1, "limit": 1},
    )

    assert response.status_code == 200, response.text
    assert negative_limit.status_code == 422, negative_limit.text
    assert negative_offset.status_code == 422, negative_offset.text
    rows = {row["id"]: row for row in response.json()["rows"]}
    story_col = str(columns["story"])
    topic_col = str(columns["topic"])

    manual_ref = rows[row_ids[0]]["meta"][topic_col]["current_value_ref"]
    assert rows[row_ids[0]]["cells"][topic_col] == "manual-alpha"
    assert manual_ref == {
        "kind": "manual_edit",
        "op_id": edit_op_id,
        "row_id": row_ids[0],
        "column_id": columns["topic"],
        "run_id": None,
    }

    generated_ref = rows[row_ids[1]]["meta"][topic_col]["current_value_ref"]
    generated_op_id = int(
        client.app.state.workspace.get(project_id)
        .db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,))
        .fetchone()["op_id"]
    )
    assert rows[row_ids[1]]["cells"][topic_col] == "ai-beta"
    assert generated_ref == {
        "kind": "run_result",
        "op_id": generated_op_id,
        "row_id": row_ids[1],
        "column_id": columns["topic"],
        "run_id": run_id,
    }
    assert rows[row_ids[1]]["meta"][topic_col]["confidence"] == 0.73
    assert rows[row_ids[1]]["meta"][topic_col]["justification"] == "generated beta"
    # Successful cells carry no outcome — it rides along only on failures.
    assert rows[row_ids[1]]["meta"][topic_col].get("outcome") is None

    missing_ref = rows[row_ids[2]]["meta"][topic_col]["current_value_ref"]
    assert rows[row_ids[2]]["cells"][topic_col] is None
    assert rows[row_ids[2]]["meta"][topic_col]["state"] == "incomplete"
    assert missing_ref == {
        "kind": "missing",
        "op_id": None,
        "row_id": row_ids[2],
        "column_id": columns["topic"],
        "run_id": None,
    }

    error_ref = rows[row_ids[3]]["meta"][topic_col]["current_value_ref"]
    assert rows[row_ids[3]]["cells"][topic_col] is None
    assert rows[row_ids[3]]["meta"][topic_col]["state"] == "error"
    assert rows[row_ids[3]]["meta"][topic_col]["error"] == "model failed"
    assert rows[row_ids[3]]["meta"][topic_col]["outcome"] == "model_error"
    assert error_ref == {
        "kind": "run_result",
        "op_id": generated_op_id,
        "row_id": row_ids[3],
        "column_id": columns["topic"],
        "run_id": run_id,
    }

    terminal_meta = rows[row_ids[4]]["meta"][topic_col]
    assert rows[row_ids[4]]["cells"][topic_col] is None
    assert terminal_meta["state"] == "error"
    assert terminal_meta["error"] == "engine returned empty output"
    assert terminal_meta["outcome"] == "empty_output"
    assert terminal_meta["current_value_ref"] == {
        "kind": "run_result",
        "op_id": generated_op_id,
        "row_id": row_ids[4],
        "column_id": columns["topic"],
        "run_id": run_id,
    }

    source_ref = rows[row_ids[0]]["meta"][story_col]["current_value_ref"]
    assert source_ref == {
        "kind": "source_cell",
        "op_id": None,
        "row_id": row_ids[0],
        "column_id": columns["story"],
        "run_id": None,
    }

    project = client.app.state.workspace.get(project_id)
    assert project.undo() == edit_op_id
    after_undo_response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"offset": 0, "limit": 1},
    )
    assert after_undo_response.status_code == 200, after_undo_response.text
    after_undo = after_undo_response.json()["rows"][0]
    assert after_undo["id"] == row_ids[0]
    assert after_undo["cells"][topic_col] == "ai-alpha"
    assert after_undo["meta"][topic_col]["current_value_ref"] == {
        "kind": "run_result",
        "op_id": generated_op_id,
        "row_id": row_ids[0],
        "column_id": columns["topic"],
        "run_id": run_id,
    }
    assert project.redo() == edit_op_id
    after_redo_response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data",
        params={"offset": 0, "limit": 1},
    )
    assert after_redo_response.status_code == 200, after_redo_response.text
    after_redo = after_redo_response.json()["rows"][0]
    assert after_redo["cells"][topic_col] == "manual-alpha"
    assert after_redo["meta"][topic_col]["current_value_ref"]["kind"] == "manual_edit"

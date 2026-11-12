"""Submitted settings survive refusals without becoming execution authority."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.server.services.action_runs import ActionRunService
from http_test_helpers import drain_queue


@pytest.fixture
def scene(tmp_path):
    client = TestClient(
        create_app(
            tmp_path / "ws", router=ModelRouter(keys={}, cache=None, cache_mode="off")
        )
    )
    workspace = client.app.state.workspace
    workspace.create("History", project_id="history")
    project = workspace.get("history")
    sheet = project.add_sheet("Notes")
    column = project.add_column(sheet, "note")
    rows = project.add_rows(sheet, [{"note": "Hello 123"}], {"note": column})
    yield client, project, sheet, rows
    project.close()


def request(sheet, rows):
    return {
        "action_id": "map.ask",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
        "params": {
            "source": ["note"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "What happened?\nKeep this instruction.",
        },
        "output_names": {"answer": "answer"},
        "idempotency_key": "history-request",
    }


def history(client, action="map.ask"):
    response = client.get(
        "/api/projects/history/actions/history", params={"action_id": action}
    )
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, item):
    response = client.get(f"/api/projects/history/actions/history/{item['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_missing_key_submission_recovers_settings_without_execution_authority(scene):
    client, project, sheet, rows = scene
    body = request(sheet, rows)
    body.update(
        confirmation="unused",
        replace_existing=True,
        expected_output_columns={"answer": None},
    )
    body["execution_context"] = {"api_key": "never-save-resolved-secret"}
    before = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
    response = client.post("/api/projects/history/actions/v1/run", json=body)
    assert response.json()["status"] == "failed", response.text
    assert response.json()["errors"][0]["code"] == "missing_provider_key"
    project.db.execute(
        "UPDATE action_submissions SET submitted_at='2026-11-12 12:34:56'"
    )
    project.db.commit()
    [item] = history(client)["items"]
    assert item["status"] == "not_started" and item["run_id"] is None
    assert item["submitted_at"] == "2026-11-12T12:34:56Z"
    restored = detail(client, item)
    assert restored["item"]["submitted_at"] == item["submitted_at"]
    saved = restored["draft"]
    assert saved["params"] == body["params"]
    assert saved["scope"] == body["scope"]
    assert saved["output_names"] == body["output_names"]
    assert set(saved) == {"action_id", "scope", "params", "output_names", "sheet_name"}
    raw = project.db.execute("SELECT draft_json FROM action_submissions").fetchone()[0]
    assert "never-save-resolved-secret" not in raw and "confirmation" not in raw
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == before
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    # A fresh HTTP client sees the same durable settings; no component-local draft is involved.
    assert history(TestClient(client.app)) == history(client)


@pytest.mark.parametrize("missing", ["empty", "row", "sheet", "hidden_sheet"])
def test_missing_scope_cannot_restore_as_all_rows(scene, missing):
    client, project, sheet, rows = scene
    body = request(sheet, [] if missing == "empty" else rows)
    client.post("/api/projects/history/actions/v1/run", json=body)
    if missing == "row":
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[0],))
    elif missing in {"sheet", "hidden_sheet"}:
        # Deleted and hidden scopes both fail against the original saved sheet id.
        if missing == "sheet":
            project.db.execute(
                "UPDATE action_submissions SET draft_json=json_set(draft_json,'$.scope.sheet_id',999),sheet_id=999"
            )
        else:
            project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet,))
    project.db.commit()
    saved = detail(client, history(client)["items"][0])
    assert saved["draft"] is None
    assert "no longer available" in saved["unavailable_reason"]


def regex_request(sheet, rows):
    return {
        **request(sheet, rows),
        "action_id": "map.regex_extract",
        "params": {"input_columns": ["note"], "pattern": r"\d+"},
        "output_names": {"extracted": "digits"},
    }


def test_admitted_failure_and_old_runs_share_one_reader(scene, monkeypatch):
    client, project, sheet, rows = scene
    body = regex_request(sheet, rows)
    response = client.post("/api/projects/history/actions/v1/run", json=body)
    assert response.json()["status"] == "queued", response.text
    run_id = response.json()["run_id"]
    # Some hosts attach the run to the receipt after submission admission.
    project.db.execute("UPDATE action_submissions SET run_id=NULL")
    project.db.commit()
    queued_history = history(client, body["action_id"])
    assert queued_history["total"] == 1
    assert queued_history["items"][0]["run_id"] == run_id
    assert queued_history["items"][0]["status"] == "queued"
    from frisket.actions.extract import regex

    def fail(*args, **kwargs):
        raise RuntimeError("injected extraction failure")

    monkeypatch.setattr(
        regex,
        "_compiled",
        lambda _: SimpleNamespace(groups=0, groupindex={}, search=fail),
    )
    drain_queue(client)
    page = history(client, body["action_id"])
    assert page["total"] == 1
    [item] = page["items"]
    assert item["run_id"] == run_id
    assert item["status"] == "failed"
    assert detail(client, item)["draft"]["params"] == body["params"]
    # A project from before submission history has only its existing durable run.
    project.db.execute("DELETE FROM action_submissions")
    project.db.commit()
    refused = request(sheet, rows)
    refused["idempotency_key"] = "unlinked-refusal"
    client.post("/api/projects/history/actions/v1/run", json=refused)
    [old] = history(client, body["action_id"])["items"]
    assert old["id"] == f"run:{run_id}"
    assert old["submitted_at"].endswith("Z")
    assert detail(client, old)["draft"]["params"]["pattern"] == r"\d+"


def test_internal_calls_and_import_payloads_do_not_duplicate_data(scene):
    client, project, sheet, rows = scene
    ActionRunService(client.app.state.workspace).run_action(
        "history", request(sheet, rows)
    )
    imported = client.post(
        "/api/projects/history/import/csv",
        files={"file": ("more.csv", "name\nAda\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    direct = client.post(
        "/api/projects/history/actions/v1/run",
        json={
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "params": {"rows": [{"large": "payload"}]},
            "idempotency_key": "import-only",
        },
    )
    assert direct.status_code < 500
    assert (
        project.db.execute("SELECT COUNT(*) FROM action_submissions").fetchone()[0] == 0
    )


def test_history_is_action_filtered_paginated_and_project_isolated(scene):
    client, project, sheet, rows = scene
    for number in range(3):
        body = request(sheet, rows)
        body["idempotency_key"] += str(number)
        client.post("/api/projects/history/actions/v1/run", json=body)
    page = client.get(
        "/api/projects/history/actions/history",
        params={"action_id": "map.ask", "offset": 1, "limit": 1},
    ).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert history(client, "map.extract")["items"] == []
    client.app.state.workspace.create("Other", project_id="other")
    assert (
        client.get(
            f"/api/projects/other/actions/history/{page['items'][0]['id']}"
        ).status_code
        == 404
    )

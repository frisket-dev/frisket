"""Semantic service paths retain the coupled source/child execution contract."""

import pytest
from fastapi.testclient import TestClient

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import _default_map_runner_factory, run_action_spec
from frisket.engine.executor.run_backfill_action import run_typed_backfill_action
from frisket.engine.executor.semantic_join_action import run_typed_semantic_join_action
from frisket.server.app import create_app
from frisket.server.services.action_runs import ActionRunService


@pytest.fixture
def semantic_workspace(tmp_path, monkeypatch):
    calls = []
    vectors = {"ACME": [1.0, 0.0], "Acme": [1.0, 0.0], "Globex": [0.0, 1.0]}

    def embed(texts):
        calls.append(list(texts))
        return [vectors[text] for text in texts]

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *args, **kwargs: (embed, "fastembed/test"),
    )
    router = ModelRouter(cache=None, cache_mode="off")
    with TestClient(create_app(tmp_path / "workspace", router=router)) as client:
        pid = client.post("/api/projects", json={"name": "Semantic services"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(pid)
        source = project.add_sheet("Donors")
        source_col = project.add_column(source, "donor")
        rows = project.add_rows(source, [{"donor": "ACME"}], {"donor": source_col})
        target = project.add_sheet("Registry")
        target_col = project.add_column(target, "company")
        project.add_rows(
            target,
            [{"company": "Acme"}, {"company": "Globex"}],
            {"company": target_col},
        )
        request = {
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": source},
            "params": {
                "source": "donor",
                "target": {"sheet_id": target, "column": "company"},
            },
            "output_names": {"match_value": "best"},
            "sheet_name": "Matches",
            "idempotency_key": "semantic-service",
        }
        yield client, pid, project, request, rows, calls


def _counts(project):
    return tuple(
        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "receipts", "ops", "sheets", "columns", "results")
    )


def _run(project, pid, request):
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request["action_id"]), ActionRequest.model_validate(request)
    )
    result = run_typed_semantic_join_action(
        project, pid, bound, None, _default_map_runner_factory
    )
    assert result.status == "completed", str(result.errors)
    return result


@pytest.mark.parametrize("existing_outputs", [False, True])
def test_served_semantic_estimate_uses_admitted_backend_without_writes_or_embedding(
    semantic_workspace, existing_outputs
):
    client, pid, project, request, _rows, calls = semantic_workspace
    if existing_outputs:
        _run(project, pid, request)
    before = _counts(project)
    calls.clear()
    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": request}
    )
    assert response.status_code == 200, response.text
    estimate = response.json()["estimate"]
    assert estimate["rows"] == 1
    assert estimate["cost"] == 0
    assert estimate["cost_source"] == "free_local"
    assert calls == []
    assert _counts(project) == before


def test_served_semantic_receipt_replay_checks_child_without_rerunning(
    semantic_workspace, monkeypatch
):
    _client, pid, project, request, _rows, calls = semantic_workspace
    original = _run(project, pid, request)
    before = _counts(project)
    calls.clear()
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *args, **kwargs: None
    )
    replay = ActionRunService._typed_receipt_replay_result(project, pid, request)
    assert replay.receipt_id == original.receipt_id
    assert replay.status == "completed"
    assert calls == []
    assert _counts(project) == before

    child = next(sheet for sheet in project.sheets() if sheet["name"] == "Matches")
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (child["id"],))
    project.db.commit()
    replay = ActionRunService._typed_receipt_replay_result(project, pid, request)
    assert replay.status == "failed"
    assert replay.errors[0].code == "stale_replay"
    assert calls == []


def test_semantic_backfill_updates_same_child_and_preserves_completed_siblings(
    semantic_workspace,
):
    client, pid, project, request, original_rows, calls = semantic_workspace
    _run(project, pid, request)
    source = request["scope"]["sheet_id"]
    columns = {column["name"]: column["id"] for column in project.columns(source)}
    child = next(sheet for sheet in project.sheets() if sheet["name"] == "Matches")
    [new_row] = project.add_rows(source, [{"donor": "Globex"}], columns)
    backfill_request = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": source},
        "params": {"column": "best"},
        "idempotency_key": "semantic-backfill",
    }
    estimate = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": backfill_request},
    )
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["estimate"]["rows"] == 1
    calls.clear()

    for retry in (False, True):
        if retry:
            # A successor receipt must itself authorize another coupled retry.
            backfill_request["scope"]["row_ids"] = [new_row]
            backfill_request["idempotency_key"] = "semantic-backfill-again"
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("run.backfill"),
            ActionRequest.model_validate(backfill_request),
        )
        result = run_typed_backfill_action(
            project, pid, bound, None, _default_map_runner_factory
        )
        assert result.status == "completed", str(result.errors)
        assert result.action.kind == "run.backfill"
        filled = next(
            output for output in result.outputs if output.kind == "run_backfill"
        )
        assert filled.ref["filled_row_ids"] == [new_row]
        assert project.get_values(source, columns["best"]) == {
            original_rows[0]: "Acme",
            new_row: "Globex",
        }
        children = [sheet for sheet in project.sheets() if sheet["name"] == "Matches"]
        assert [sheet["id"] for sheet in children] == [child["id"]]
        assert project.row_count(child["id"]) == 2
        child_columns = {
            column["name"]: column["id"] for column in project.columns(child["id"])
        }
        assert set(project.get_values(child["id"], child_columns["best"]).values()) == {
            "Acme",
            "Globex",
        }
        assert all("ACME" not in call for call in calls)
        before = _counts(project)
        calls.clear()
        replay = run_typed_backfill_action(
            project,
            pid,
            bound,
            None,
            lambda *_args: pytest.fail("backfill replay executed a runner"),
        )
        assert replay.receipt_id == result.receipt_id
        assert replay.status == "completed"
        assert calls == []
        assert _counts(project) == before
        linked = run_action_spec(
            project,
            {
                "action_id": "derive.link_table",
                "scope": {"kind": "project"},
                "params": {
                    "source": {"kind": "semantic_join", "receipt_id": result.receipt_id}
                },
                "sheet_name": f"Backfill links {retry}",
                "idempotency_key": f"backfill-links-{retry}",
            },
            project_id=pid,
        )
        assert linked.status == "completed", str(linked.errors)
        assert calls == []


def test_semantic_backfill_with_no_remaining_rows_keeps_child(semantic_workspace):
    _client, pid, project, request, _rows, calls = semantic_workspace
    _run(project, pid, request)
    child = next(sheet for sheet in project.sheets() if sheet["name"] == "Matches")
    original_children = project.visible_row_ids(child["id"])
    calls.clear()
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("run.backfill"),
        ActionRequest.model_validate(
            {
                "action_id": "run.backfill",
                "scope": request["scope"],
                "params": {"column": "best"},
                "idempotency_key": "semantic-empty-backfill",
            }
        ),
    )
    result = run_typed_backfill_action(
        project, pid, bound, None, _default_map_runner_factory
    )
    assert result.status == "completed", str(result.errors)
    filled = next(output for output in result.outputs if output.kind == "run_backfill")
    assert filled.ref["filled"] == 0
    assert project.visible_row_ids(child["id"]) == original_children
    assert calls == []


def test_semantic_backfill_refuses_missing_child_before_embedding(semantic_workspace):
    _client, pid, project, request, _rows, calls = semantic_workspace
    _run(project, pid, request)
    source = request["scope"]["sheet_id"]
    columns = {column["name"]: column["id"] for column in project.columns(source)}
    project.add_rows(source, [{"donor": "Globex"}], columns)
    child = next(sheet for sheet in project.sheets() if sheet["name"] == "Matches")
    project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (child["id"],))
    project.db.commit()
    calls.clear()
    before = _counts(project)
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("run.backfill"),
        ActionRequest.model_validate(
            {
                "action_id": "run.backfill",
                "scope": request["scope"],
                "params": {"column": "best"},
                "idempotency_key": "missing-child-backfill",
            }
        ),
    )
    result = run_typed_backfill_action(
        project, pid, bound, None, _default_map_runner_factory
    )
    assert result.status == "failed"
    assert "child" in result.errors[0].message
    assert calls == []
    assert _counts(project) == before


def test_ordinary_zero_row_backfill_still_closes_its_attempt(tmp_path):
    from http_test_helpers import (
        drain_queue,
        post_v1_action_with_exact_confirmation,
    )
    from tests.server.test_run_backfill_executor import (
        _backfill_action,
        _classify_spec,
        _client,
        _seed_project,
    )

    with _client(tmp_path) as client:
        pid, sheet = _seed_project(client)
        response = post_v1_action_with_exact_confirmation(
            client, pid, _classify_spec(sheet)
        )
        assert response.status_code == 200, response.text
        drain_queue(client)
        response = post_v1_action_with_exact_confirmation(
            client, pid, _backfill_action(sheet)
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "completed", result
        output = next(
            item for item in result["outputs"] if item["kind"] == "run_backfill"
        )
        assert output["ref"]["filled"] == 0
        project = client.app.state.workspace.get(pid)
        [attempt] = project.db.execute(
            "SELECT state FROM execution_attempts WHERE run_id=?", (result["run_id"],)
        ).fetchall()
        assert attempt["state"] == "effected"

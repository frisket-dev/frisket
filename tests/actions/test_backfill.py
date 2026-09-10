from __future__ import annotations

import json
from typing import Any

import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    PreparedBackfill,
    RunBackfiller,
)
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.run_backfill_action import (
    prepare_backfill_action,
    run_typed_backfill_action,
)
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation
from tests.server.test_run_backfill_executor import (
    _classify_spec,
    _client,
    _seed_project,
)


class CustomParams(ActionParams):
    selected: str


def custom(params: CustomParams, runs: RunBackfiller) -> PreparedBackfill:
    return runs.prepare(ColumnRef[Any](params.selected.removeprefix("chosen:")))


def _bound(sheet_id: int, handler=custom, *, key="custom-backfill"):
    definition = action(
        name="recover",
        title="Recover",
        description="Recover selected results",
        category=ActionCategory.CONVERT,
        run=handler,
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.recover")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.recover",
            scope={"kind": "sheet_rows", "sheet_id": sheet_id},
            params={"selected": "chosen:beat"},
            idempotency_key=key,
        ),
    )


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    # This fixture exercises the explicit consent round-trip. Keep the local
    # project from inheriting an operator's standing preapproval threshold.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = _client(tmp_path)
    pid, sheet_id = _seed_project(client)
    response = post_v1_action_with_exact_confirmation(
        client, pid, _classify_spec(sheet_id)
    )
    assert response.status_code == 200, response.text
    drain_queue(client)
    project = client.app.state.workspace.get(pid)
    column = next(col for col in project.columns(sheet_id) if col["name"] == "story")
    project.add_rows(sheet_id, [{"story": "late"}], {"story": column["id"]})
    yield client, project, pid, sheet_id
    client.close()


def _counts(project):
    return tuple(
        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "receipts", "ops", "rows")
    )


@pytest.mark.parametrize("custom_action", [False, True])
def test_catalog_scope_matches_backfill_binding(custom_action):
    registered = (
        _bound(1).action if custom_action else ACTION_REGISTRY.get("run.backfill")
    )
    entry = registered.catalog_entry()
    assert entry["execution_mode"] == "per_row"
    assert entry["async_mode"] == "sync"
    assert entry["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows", "exact_membership"],
    }


def test_actual_derived_column_prepares_without_writes_and_executes_with_host_facts(
    seeded,
):
    client, project, pid, sheet_id = seeded
    bound = _bound(sheet_id)
    before = _counts(project)
    plan = prepare_backfill_action(project, bound)
    assert plan.column == "beat"
    assert len(plan.row_ids) == 1
    assert _counts(project) == before
    result = run_typed_backfill_action(
        project,
        pid,
        bound,
        client.app.state.workspace.router_for(project),
        _default_map_runner_factory,
    )
    assert result.status == "needs_confirmation", result.errors
    bound = BoundTypedActionRequest.bind(
        bound.action,
        bound.request.model_copy(
            update={
                "confirmation": result.errors[0].details["promise_set_hash"],
            }
        ),
    )
    result = run_typed_backfill_action(
        project,
        pid,
        bound,
        client.app.state.workspace.router_for(project),
        _default_map_runner_factory,
    )
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["filled"] == 1
    assert result.outputs[0].ref["column_name"] == "beat"
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()[0]
    )
    fact = next(
        item["ref"]
        for item in receipt["evidence"]
        if item["ref"]["kind"] == "backfill_source_generation"
    )
    assert fact["column"] == "beat"
    assert fact["row_ids"] == list(plan.row_ids)
    after = _counts(project)
    replay = run_typed_backfill_action(
        project, pid, bound, None, lambda *_args: pytest.fail("replay ran")
    )
    assert replay.receipt_id == result.receipt_id
    assert _counts(project) == after


@pytest.mark.parametrize("mode", ["forged", "foreign", "wrong_type", "raises"])
def test_invalid_preparation_returns_refuse_before_any_effect(seeded, mode):
    _client_value, project, pid, sheet_id = seeded
    prior: list[PreparedBackfill] = []

    def capture(params: CustomParams, runs: RunBackfiller) -> PreparedBackfill:
        handle = runs.prepare(ColumnRef[Any](params.selected.removeprefix("chosen:")))
        prior.append(handle)
        return handle

    prepare_backfill_action(project, _bound(sheet_id, capture))

    def invalid(params: CustomParams, runs: RunBackfiller) -> PreparedBackfill:
        handle = runs.prepare(ColumnRef[Any](params.selected.removeprefix("chosen:")))
        if mode == "raises":
            raise ValueError("author failed after preparation")
        if mode == "foreign":
            return prior[0]
        if mode == "wrong_type":
            return {"selected_row_count": handle.selected_row_count}  # type: ignore[return-value]
        return PreparedBackfill(handle.selected_row_count)

    before = _counts(project)
    result = run_typed_backfill_action(
        project,
        pid,
        _bound(sheet_id, invalid),
        None,
        lambda *_args: pytest.fail("refusal ran"),
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_params"
    assert _counts(project) == before
    assert not project.db.in_transaction


@pytest.mark.parametrize("rows", [[], [True], [0], [1, 1]])
def test_backfill_scope_refuses_invalid_explicit_rows(rows):
    validation = validate_root_action(
        {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": rows},
            "params": {"column": "beat"},
            "idempotency_key": "invalid-scope",
        }
    )
    assert not validation.ok

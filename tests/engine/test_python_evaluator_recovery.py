"""Regression proofs preserved from independent Python migration review."""

from copy import deepcopy
from dataclasses import replace
import hashlib

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.geospatial_types import GeocodedAddress, Geocoder
from frisket.actions.python_types import PythonEvaluator, RoutedOutput
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, Row, RowResult
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from tests.engine.test_typed_python import derived
from tests.engine.test_typed_python import source as source
from tests.engine.test_typed_geocode import RenamedGeocodeParams
from tests.engine.test_typed_geocode import env as env

import pytest

from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.store.receipts import ReceiptStore
from http_test_helpers import drain_queue
from tests.engine.test_typed_project_run_queue import _client
from tests.engine.test_typed_python import request


@pytest.fixture(autouse=True)
def _always_challenge_metered_actions(monkeypatch):
    # The imported geocode fixture creates its Project before the test body;
    # establish the local consent posture before that fixture is evaluated.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")


def _queued(tmp_path, *, custom=False):
    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Python hostile"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("Inputs")
    column = project.add_column(sheet, "payload", type="json")
    project.add_rows(sheet, [{"payload": {"n": 1}}], {"payload": column})
    body = request(sheet)
    if custom:
        body["params"] = {
            "selected": "payload",
            "expression": "row['value']['n'] + 20",
            "route": {
                "name": "answer",
                "path": "$",
                "target": {"kind": "column", "type": "integer"},
            },
        }
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["status"] == "queued"
    ws = client.app.state.workspace
    job = ws.queue.get(queued["job_id"])
    return (
        client,
        project,
        queued,
        ws.registry.get("project.run"),
        {**deepcopy(job.payload), "job_id": job.id},
        "result = row['value']['n'] + 20" if custom else body["params"]["code"],
    )


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_worker_recovery_preserves_actual_code_evidence(
    tmp_path, monkeypatch, crash, custom
):
    from frisket.engine.jobs import runs
    from frisket.engine.executor import python_transform

    if custom:
        registered = ACTION_REGISTRY.get("map.python")
        replacement = RegisteredAction(
            "map.python",
            replace(
                registered.definition,
                run=map_rows(
                    derived,
                    dynamic_outputs=lambda params: {
                        params.route.name: RoutedOutput(
                            params.route, {"type": "integer"}
                        )
                    },
                ),
                _example_params=(),
            ),
        )
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {**ACTION_REGISTRY._actions, "map.python": replacement},
        )
    client, project, queued, handler, payload, actual_code = _queued(
        tmp_path, custom=custom
    )
    context = JobHandlerContext.from_claimed_job(trusted_org_id=None)
    original = runs.queued_v1_finalize_action_result
    if crash:

        class ProcessDeath(BaseException):
            pass

        def die(*args, **kwargs):
            raise ProcessDeath()

        monkeypatch.setattr(runs, "queued_v1_finalize_action_result", die)
        with pytest.raises(ProcessDeath):
            handler(deepcopy(payload), context)
        monkeypatch.setattr(runs, "queued_v1_finalize_action_result", original)
    else:
        drain_queue(client)
    before = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    expected_hash = "sha256:" + hashlib.sha256(actual_code.encode()).hexdigest()
    assert [
        item.ref["code_hash"]
        for item in before.evidence
        if item.ref.get("kind") == "map_python_code"
    ] == [expected_hash]

    def forbidden():
        raise AssertionError("Recovery cannot execute another Python guest")

    monkeypatch.setattr(python_transform, "resolve_executor", forbidden)
    rows_before = [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM results ORDER BY row_id, column_id"
        )
    ]
    assert handler(deepcopy(payload), context)["skipped"] is True
    after = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert after.status == "completed"
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT * FROM results ORDER BY row_id, column_id"
        )
    ] == rows_before
    assert [
        item.ref["code_hash"]
        for item in after.evidence
        if item.ref.get("kind") == "map_python_code"
    ] == [expected_hash]
    client.close()


def test_frozen_hidden_schema_rejects_scalar_before_feedable_receipt(source):
    project, sheet, _, _ = source
    definition = action(
        name="wrongshape",
        title="Wrong shape",
        description="Review",
        category=ActionCategory.CONVERT,
        run=map_rows(
            derived,
            dynamic_outputs=lambda params: {
                params.route.name: RoutedOutput(
                    params.route, {"type": "array", "items": {"type": "integer"}}
                )
            },
        ),
    )
    bound = BoundTypedActionRequest.bind(
        RegisteredAction("review.wrongshape", definition),
        ActionRequest(
            action_id="review.wrongshape",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            idempotency_key="wrongshape",
            params={
                "selected": "payload",
                "expression": "42",
                "route": {
                    "name": "items",
                    "path": "$",
                    "target": {
                        "kind": "named_result",
                        "schema": "items",
                        "may_feed": ["derive.table_from_list"],
                    },
                },
            },
        ),
    )
    result = run_typed_map_rows_action(
        project, "review", bound, None, _default_map_runner_factory
    )
    assert result.status == "failed", [
        (output.kind, output.ref) for output in result.outputs
    ]


async def mixed_lookup(
    params: RenamedGeocodeParams,
    row: Row,
    geocoder: Geocoder,
    evaluator: PythonEvaluator,
) -> RowResult[GeocodedAddress]:
    result = await geocoder.lookup(params.query.read(row))
    await evaluator.evaluate(code="result = 1", row={})
    return RowResult(output=result)


def test_python_capability_does_not_replace_other_capability_provider_facts(
    env, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-opencage-key")
    project, sheet, calls, run = env
    registered = ACTION_REGISTRY.get("enrich.geocode")
    replacement = RegisteredAction(
        "enrich.geocode",
        replace(registered.definition, run=map_rows(mixed_lookup), _example_params=()),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, "enrich.geocode": replacement},
    )
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"query": "address", "selected": "opencage"},
        "idempotency_key": "mixed-geocode-python",
    }
    quoted = run(body)
    assert quoted.status == "needs_confirmation", quoted.errors
    body["confirmation"] = quoted.errors[0].details["promise_set_hash"]
    completed = run(body)
    assert completed.status == "completed", completed.errors
    assert len(calls) == 1
    receipt = ReceiptStore(project).parsed_by_id(completed.receipt_id)
    assert any(
        item.get("provider") == "opencage" and item.get("external_api")
        for item in receipt.provider_use
    ), receipt.provider_use
    local = next(
        item
        for item in receipt.provider_use
        if item.get("service") == "frisket.sandbox"
    )
    assert local["cost_actual"] == 0.0 and local["model_call_count"] == 0

    address = next(
        column for column in project.columns(sheet) if column["name"] == "address"
    )
    project.add_rows(sheet, [{"address": "Cambridge"}], {"address": address["id"]})
    backfill_body = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"column": receipt.outputs[0].name},
        "idempotency_key": "mixed-geocode-python-backfill",
    }
    quoted = run(backfill_body)
    assert quoted.status == "needs_confirmation", quoted.errors
    backfill_body["confirmation"] = quoted.errors[0].details["promise_set_hash"]
    backfilled = run(backfill_body)
    assert backfilled.status == "completed", backfilled.errors
    assert len(calls) == 2
    successor = ReceiptStore(project).parsed_by_id(backfilled.receipt_id)
    assert successor.provider_use == receipt.provider_use
    code_evidence = [
        item.ref
        for item in successor.evidence
        if item.ref.get("kind") == "map_python_code"
    ]
    assert len(code_evidence) == 1
    assert code_evidence[0]["run_id"] == backfilled.run_id
    assert (
        code_evidence[0]["code_hash"]
        == "sha256:" + hashlib.sha256(b"result = 1").hexdigest()
    )

    from frisket.engine.executor import python_transform

    def forbidden():
        raise AssertionError("same-key backfill replay must not reconstruct evaluator")

    monkeypatch.setattr(python_transform, "resolve_executor", forbidden)
    replayed = run(backfill_body)
    assert replayed.status == "completed", replayed.errors
    assert replayed.receipt_id == backfilled.receipt_id
    assert len(calls) == 2
    assert ReceiptStore(project).parsed_by_id(replayed.receipt_id) == successor

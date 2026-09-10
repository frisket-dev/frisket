from __future__ import annotations

import copy
import hashlib

import pytest

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.python_types import (
    PythonEvaluator,
    PythonOutputRoute,
    RoutedOutput,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    DynamicOutput,
    Row,
    RowResult,
)
from frisket.engine.executor.actions import _default_map_runner_factory, run_action_spec
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from helpers import replace_test_source_cell


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "python.frisket")
    sheet = project.add_sheet("Inputs")
    col = project.add_column(sheet, "payload", type="json")
    secret = project.add_column(sheet, "unselected", type="text")
    rows = project.add_rows(
        sheet,
        [
            {"payload": {"n": 1}, "unselected": "private"},
            {"payload": {"n": 2}, "unselected": "private"},
        ],
        {"payload": col, "unselected": secret},
    )
    yield project, sheet, rows, col
    project.close()


def request(sheet, *, key="python", target="named_result"):
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "input_columns": ["payload"],
            "code": "assert set(row) == {'payload'}\nresult = [{'n': row['payload']['n']}]",
            "return_schema": {
                "type": "array",
                "items": {"type": "object", "properties": {"n": {"type": "integer"}}},
            },
            "output_routes": [
                {
                    "name": "items",
                    "path": "$",
                    "target": (
                        {
                            "kind": "named_result",
                            "schema": "items",
                            "may_feed": ["derive.table_from_list"],
                        }
                        if target == "named_result"
                        else {"kind": "receipt_evidence", "retention": "pinned"}
                    ),
                }
            ],
        },
        "idempotency_key": key,
    }


def test_internal_empty_backfill_scope_does_not_relax_public_requests(source):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
        build_typed_map_rows_plan,
    )

    project, sheet, _, _ = source
    body = request(sheet)
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("map.python"), ActionRequest.model_validate(body)
    )
    spec = build_typed_map_rows_plan(project, bound).spec_dict()
    spec["row_ids"] = []
    reconstructed = bound_typed_program_request_from_runner_spec(spec)
    assert reconstructed is not None
    assert reconstructed.request.scope.row_ids == ()
    body["scope"]["row_ids"] = []
    with pytest.raises(ValueError, match="row ids must not be empty"):
        ActionRequest.model_validate(body)


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_unsuccessful_replay_still_checks_published_hidden_outputs(source, status):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.executor.map_rows_action import _typed_replay_error

    project, sheet, _, _ = source
    body = request(sheet)
    result = run_action_spec(project, body, project_id="python")
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt is not None
    receipt = receipt.model_copy(update={"status": status})
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("map.python"), ActionRequest.model_validate(body)
    )
    check = _typed_replay_error(project, bound)
    assert check(receipt) is None
    hidden = next(
        col for col in project.columns(sheet, include_hidden=True) if col["hidden"]
    )
    project.db.execute("UPDATE columns SET name='changed' WHERE id=?", (hidden["id"],))
    project.db.commit()
    error = check(receipt)
    assert error is not None and error.code == "stale_replay"


@pytest.mark.parametrize("target", ["named_result", "receipt_evidence"])
def test_hidden_only_routes_replay_with_generation_and_value_checks(
    source, monkeypatch, target
):
    project, sheet, rows, _ = source
    body = request(sheet, target=target)
    result = run_action_spec(project, body, project_id="python")
    assert result.status == "completed", result.errors
    from frisket.engine.executor import python_transform

    def forbidden():
        raise AssertionError("replay must not construct an evaluator")

    monkeypatch.setattr(python_transform, "resolve_executor", forbidden)
    replay = run_action_spec(project, body, project_id="python")
    assert replay.status == "completed", replay.errors
    assert replay.receipt_id == result.receipt_id
    hidden = next(
        col for col in project.columns(sheet, include_hidden=True) if col["hidden"]
    )
    project.db.execute("UPDATE columns SET name='changed' WHERE id=?", (hidden["id"],))
    project.db.commit()
    stale = run_action_spec(project, body, project_id="python")
    assert stale.status == "failed" and stale.errors[0].code == "stale_replay"


class DerivedParams(ActionParams):
    selected: ColumnRef[dict[str, int]]
    expression: str
    route: PythonOutputRoute
    mutate_route: bool = False
    bad_output: bool = False


async def derived(
    params: DerivedParams, row: Row, evaluator: PythonEvaluator
) -> RowResult[DynamicOutput]:
    result = await evaluator.evaluate(
        code="result = " + params.expression, row={"value": params.selected.read(row)}
    )
    if params.mutate_route:
        params.route.path = "$.forged"
    return RowResult(
        output=DynamicOutput(
            {params.route.name: "bad" if params.bad_output else result}
        )
    )


def test_custom_evaluator_uses_actual_derived_args_and_frozen_routes(source):
    project, sheet, rows, _ = source
    definition = action(
        name="derived",
        title="Derived",
        description="Derive evaluator arguments.",
        category=ActionCategory.CONVERT,
        run=map_rows(
            derived,
            dynamic_outputs=lambda params: {
                params.route.name: RoutedOutput(params.route, {"type": "integer"})
            },
        ),
    )
    registered = RegisteredAction("example.derived", definition)
    bound = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="example.derived",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={
                "selected": "payload",
                "expression": "row['value']['n'] + 10",
                "mutate_route": True,
                "route": {
                    "name": "answer",
                    "path": "$",
                    "target": {"kind": "column", "type": "integer"},
                },
            },
            idempotency_key="derived",
        ),
    )
    result = run_typed_map_rows_action(
        project, "python", bound, None, _default_map_runner_factory
    )
    assert result.status == "completed", result.errors
    output = result.outputs[0]
    assert list(project.get_values(sheet, output.column_id).values()) == [11, 12]
    assert output.ref["path"] == "$"
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    hashes = [
        item.ref["code_hash"]
        for item in receipt.evidence
        if item.ref.get("kind") == "map_python_code"
    ]
    assert hashes == [
        "sha256:" + hashlib.sha256(b"result = row['value']['n'] + 10").hexdigest()
    ]


def test_host_enforces_routed_column_type_even_when_custom_handler_skips_it(source):
    project, sheet, _, _ = source
    definition = action(
        name="bad",
        title="Bad output",
        description="Exercise host output validation.",
        category=ActionCategory.CONVERT,
        run=map_rows(
            derived,
            dynamic_outputs=lambda params: {
                params.route.name: RoutedOutput(params.route, {"type": "integer"})
            },
        ),
    )
    bound = BoundTypedActionRequest.bind(
        RegisteredAction("example.bad", definition),
        ActionRequest(
            action_id="example.bad",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={
                "selected": "payload",
                "expression": "1",
                "bad_output": True,
                "route": {
                    "name": "answer",
                    "path": "$",
                    "target": {"kind": "column", "type": "integer"},
                },
            },
            idempotency_key="bad",
        ),
    )
    result = run_typed_map_rows_action(
        project, "python", bound, None, _default_map_runner_factory
    )
    assert result.status == "failed"
    errors = project.db.execute(
        "SELECT error_code FROM results WHERE run_id=?", (result.run_id,)
    ).fetchall()
    assert {row[0] for row in errors} == {"return_schema_mismatch"}


@pytest.mark.parametrize(
    "code,schema,expected",
    [
        ("result = None", {"type": "null"}, "completed"),
        ("x = 1", {"type": "null"}, "failed"),
        (
            "result = {'n': 'bad'}",
            {"type": "object", "properties": {"n": {"type": "integer"}}},
            "failed",
        ),
    ],
)
def test_raw_return_schema_and_explicit_null_stay_distinct(
    source, code, schema, expected
):
    project, sheet, _, _ = source
    body = request(sheet, target="receipt_evidence")
    body["params"].update(code=code, return_schema=schema)
    result = run_action_spec(project, body, project_id="python")
    assert result.status == expected, result.errors
    replay = run_action_spec(project, body, project_id="python")
    assert replay.status == expected, replay.errors
    assert replay.receipt_id == result.receipt_id


def test_repeated_backfill_keeps_rich_output_bindings(source):
    project, sheet, _, col = source
    body = request(sheet)
    body["params"]["output_routes"].extend(
        [
            {
                "name": "visible",
                "path": "$",
                "target": {"kind": "column", "type": "json"},
            },
            {
                "name": "debug",
                "path": "$",
                "target": {"kind": "receipt_evidence", "retention": "pinned"},
            },
        ]
    )
    first = run_action_spec(project, body, project_id="python")
    assert first.status == "completed", first.errors
    original = {
        item["name"]: item["id"] for item in project.columns(sheet, include_hidden=True)
    }
    added = project.add_rows(sheet, [{"payload": {"n": 3}}], {"payload": col})
    for number in (3, 4):
        backfill = run_action_spec(
            project,
            {
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet,
                    **({"row_ids": added} if number == 4 else {}),
                },
                "params": {"column": "visible"},
                "idempotency_key": f"backfill-{number}",
            },
            project_id="python",
        )
        assert backfill.status == "completed", backfill.errors
        assert {
            item["name"]: item["id"]
            for item in project.columns(sheet, include_hidden=True)
        } == original
        for name in ("visible", "__result_items", "__evidence_debug"):
            assert project.get_values(sheet, original[name], row_ids=added)[
                added[0]
            ] == [{"n": 3}]
        named = next(
            output for output in backfill.outputs if output.kind == "named_result"
        )
        assert named.ref["row_ids"] == added
        receipt = ReceiptStore(project).parsed_by_id(backfill.receipt_id)
        assert any(
            item.ref.get("kind") == "map_python_code" for item in receipt.evidence
        )
        assert any(
            item.ref.get("kind") == "map_python_receipt_evidence"
            and item.retention == "pinned"
            for item in receipt.evidence
        )
        derived_result = run_action_spec(
            project,
            {
                "action_id": "derive.table_from_list",
                "scope": {"kind": "project"},
                "sheet_name": f"Items {number}",
                "idempotency_key": f"derive-{number}",
                "params": {
                    "source": {
                        "kind": "named_result",
                        "sheet_id": sheet,
                        "column_id": original["__result_items"],
                        "run_id": backfill.run_id,
                        "route": "items",
                        "schema": "items",
                    },
                    "item_schema": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                    },
                    "columns": [{"name": "n", "path": "$.n", "type": "integer"}],
                },
            },
            project_id="python",
        )
        assert derived_result.status == "completed", derived_result.errors


def test_duplicate_hidden_names_and_hidden_rename_refuse_before_guest(source):
    project, sheet, _, _ = source
    body = request(sheet)
    body["output_names"] = {"items": "forged"}
    assert run_action_spec(project, body, project_id="python").status == "failed"
    body.pop("output_names")
    other = copy.deepcopy(body["params"]["output_routes"][0])
    body["params"]["output_routes"][0]["name"] = "a-b"
    other["name"] = "a_b"
    body["params"]["output_routes"].append(other)
    assert run_action_spec(project, body, project_id="python").status == "failed"
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_rich_routes_survive_canonical_queue_and_replay(tmp_path, monkeypatch):
    from tests.engine.test_typed_project_run_queue import _client
    from http_test_helpers import drain_queue

    client = _client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Python queue"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet = project.add_sheet("Inputs")
    col = project.add_column(sheet, "payload", type="json")
    rows = project.add_rows(sheet, [{"payload": {"n": 1}}], {"payload": col})
    body = request(sheet)
    body["params"]["output_routes"].extend(
        [
            {
                "name": "visible",
                "path": "$",
                "target": {"kind": "column", "type": "json"},
            },
            {
                "name": "debug",
                "path": "$",
                "target": {"kind": "receipt_evidence", "retention": "pinned"},
            },
        ]
    )
    path = f"/api/projects/{project_id}/actions/v1/run"
    first = client.post(path, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "queued"
    drain_queue(client)
    receipt = ReceiptStore(project).parsed_by_id(first.json()["receipt_id"])
    assert receipt.status == "completed"
    named = next(
        output for output in receipt.outputs if output.ref.get("kind") == "named_result"
    )
    assert named.ref["row_ids"] == rows
    assert any(item.ref.get("kind") == "map_python_code" for item in receipt.evidence)
    from frisket.engine.executor import python_transform

    def forbidden():
        raise AssertionError("replay must not construct an evaluator")

    monkeypatch.setattr(python_transform, "resolve_executor", forbidden)
    replay = client.post(path, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "completed"
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    client.close()


def test_partial_named_membership_and_failed_row_backfill(source):
    project, sheet, rows, col = source
    body = request(sheet)
    body["params"]["code"] = (
        "assert row['payload']['n'] != 1\nresult = [{'n': row['payload']['n']}]"
    )
    body["params"]["output_routes"].append(
        {"name": "visible", "path": "$", "target": {"kind": "column", "type": "json"}}
    )
    first = run_action_spec(project, body, project_id="python")
    assert first.status == "partial", first.errors
    named = next(item for item in first.outputs if item.kind == "named_result")
    assert named.ref["row_ids"] == [rows[1]]
    replace_test_source_cell(
        project,
        row_id=rows[0],
        column_id=col,
        value={"n": 3},
    )
    result = run_action_spec(
        project,
        {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
            "params": {"column": "visible"},
            "idempotency_key": "failed-recovery",
        },
        project_id="python",
    )
    assert result.status == "completed", result.errors
    recovered = next(item for item in result.outputs if item.kind == "named_result")
    assert recovered.ref["row_ids"] == [rows[0]]


@pytest.mark.parametrize("path,expected", [("$.a[0]", 9), ("$.values[-1].n", 2)])
def test_route_paths_keep_literal_keys_and_negative_array_indices(
    source, path, expected
):
    project, sheet, _, _ = source
    body = request(sheet)
    body["params"].update(
        code="result = {'a[0]': 9, 'a': [1], 'values': [{'n': 1}, {'n': 2}]}",
        return_schema={"type": "object"},
        output_routes=[
            {
                "name": "value",
                "path": path,
                "target": {"kind": "column", "type": "integer"},
            }
        ],
    )
    result = run_action_spec(project, body, project_id="python")
    assert result.status == "completed", result.errors
    assert set(project.get_values(sheet, result.outputs[0].column_id).values()) == {
        expected
    }

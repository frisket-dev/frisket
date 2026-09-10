"""Registered params models own derive/semantic/embedding structure."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from frisket.actions.system import (
    root_action_catalog,
    typed_action_for_request,
    validate_root_action,
)
from frisket.actions.types import ActionRequest
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project


MALFORMED_PARAMS = (("derive.link_table", {"source": []}),)
AFFECTED_KINDS = {kind for kind, _patch in MALFORMED_PARAMS}
VALID_ACTIONS = {
    entry.kind: entry.examples[0]
    for entry in root_action_catalog().actions
    if entry.kind in AFFECTED_KINDS | {"embedding.index_create"}
}


def _set_path(params: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = params
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


@pytest.mark.parametrize("kind", sorted(AFFECTED_KINDS))
def test_valid_params_keep_raw_action_and_hash(kind: str) -> None:
    raw = deepcopy(VALID_ACTIONS[kind])
    validation = validate_root_action(raw)

    assert validation.ok is True, validation.model_dump()
    assert validation.action is not None
    assert validation.action.params == raw["params"]
    assert typed_request_hash(typed_action_for_request(raw)) == typed_request_hash(
        typed_action_for_request(validation.action.model_dump(mode="json"))
    )


@pytest.mark.parametrize("kind,patch", MALFORMED_PARAMS)
def test_known_field_malformed_params_defer_safely_to_model(
    kind: str, patch: dict[str, Any]
) -> None:
    spec = deepcopy(VALID_ACTIONS[kind])
    spec["params"].update(patch)

    result = validate_root_action(spec)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_action_request"
    assert "source" in result.error.message


def _project_counts(project: Project) -> tuple[int, ...]:
    return tuple(
        project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("sheets", "columns", "rows", "ops", "receipts")
    )


def _list_table_request() -> dict[str, Any]:
    return {
        "action_id": "derive.table_from_list",
        "scope": {"kind": "project"},
        "sheet_name": "Items",
        "params": {
            "source": {
                "kind": "named_result",
                "sheet_id": 1,
                "column_id": 2,
                "run_id": 3,
                "route": "items",
                "schema": "items.v1",
            },
            "item_schema": {"type": "object"},
            "columns": [{"name": "name", "path": "$.name", "type": "text"}],
        },
        "idempotency_key": "list-table-shape",
    }


def test_typed_list_table_validation_preserves_request_and_hash():
    raw = _list_table_request()
    validation = validate_root_action(raw)

    assert validation.ok, validation.model_dump()
    assert validation.action == ActionRequest.model_validate(raw)
    assert validation.action.params == raw["params"]
    assert typed_request_hash(typed_action_for_request(raw)) == typed_request_hash(
        typed_action_for_request(validation.action.model_dump(mode="json"))
    )


@pytest.mark.parametrize(
    "path,value",
    [
        (("source",), []),
        (("source", "sheet_id"), "1"),
        (("source", "column_id"), "2"),
        (("source", "run_id"), "3"),
    ],
)
def test_typed_list_table_malformed_shapes_and_id_coercions_refuse_before_effects(
    tmp_path, path, value
):
    request = _list_table_request()
    _set_path(request["params"], path, value)
    validation = validate_root_action(request)
    assert not validation.ok
    assert validation.error.code == "invalid_action_request"
    assert "source" in validation.error.message

    project = Project.create(tmp_path / "list-table-shape.frisket")
    try:
        before = _project_counts(project)
        result = run_action_spec(project, request, project_id="shape-ownership")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert _project_counts(project) == before
    finally:
        project.close()


@pytest.mark.parametrize(
    "kind,patch",
    (
        ("derive.link_table", {"source": []}),
        ("embedding.index_create", {"source_columns": "body"}),
    ),
)
def test_malformed_params_fail_before_project_effects(
    tmp_path, kind: str, patch: dict[str, Any]
) -> None:
    project = Project.create(tmp_path / f"{kind}.frisket", name=kind)
    try:
        before = _project_counts(project)
        spec = deepcopy(VALID_ACTIONS[kind])
        spec["params"].update(patch)

        result = run_action_spec(project, spec, project_id="shape-ownership")

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert next(iter(patch)) in result.errors[0].message
        assert _project_counts(project) == before
    finally:
        project.close()


@pytest.mark.parametrize(
    "action_id, params",
    [
        ("embedding.index_project", {"index_id": "idx", "dimensions": 3}),
        ("embedding.index_cluster", {"index_id": "idx", "k": []}),
        ("embedding.index_cluster", {"index_id": "idx", "k": "2"}),
    ],
)
def test_typed_embedding_analysis_rejects_malformed_semantics(action_id, params):
    result = validate_root_action(
        {
            "action_id": action_id,
            "scope": {"kind": "project"},
            "sheet_name": "Analysis",
            "params": params,
            "idempotency_key": "bad",
        }
    )
    assert not result.ok
    assert result.error.code == "invalid_action_request"

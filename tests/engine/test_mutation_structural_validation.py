from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from typing import Any

import pytest
from pydantic import ValidationError

from frisket.actions.mutations import (
    CellEditParams,
    CellEditQueryParams,
    ColumnAddParams,
    ColumnPatchParams,
    ColumnSetTypeParams,
    RowAddParams,
    RowDeleteParams,
)
from frisket.actions.review_replay import (
    ReplayAcceptColumnParams,
    ReplayAcceptParams,
    ReplayDismissParams,
    ReviewDecisionParams,
)
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import EditedCells, QueryEditedCells, RetypedColumn
from frisket.engine.executor import actions as executor_actions
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project

_HASH = f"sha256:{'a' * 64}"

VALID_PARAMS: dict[str, dict[str, Any]] = {
    "review.decision": {
        "run_id": 1,
        "row_id": 1,
        "column_id": 2,
        "decision": "edit",
        "value": 0,
    },
    "replay.accept": {
        "sheet_id": 1,
        "row_id": 1,
        "column_id": 2,
        "run_id": 1,
        "generated_value_hash": _HASH,
    },
    "replay.accept_column": {"sheet_id": 1, "column_id": 2},
    "replay.dismiss": {
        "sheet_id": 1,
        "row_id": 1,
        "column_id": 2,
        "generated_value_hash": _HASH,
        "run_id": 1,
    },
}

_DELETE = object()
MALFORMED: tuple[tuple[str, str, tuple[str | int, ...], Any], ...] = (
    ("review-target", "review.decision", ("row_id",), 0),
    ("review-coerced-id", "review.decision", ("run_id",), True),
    ("review-decision", "review.decision", ("decision",), "maybe"),
    ("review-note", "review.decision", ("note",), 3),
    ("review-missing", "review.decision", ("value",), _DELETE),
    ("review-cross", "review.decision", ("decision",), "accept"),
    ("review-json", "review.decision", ("value",), object()),
    ("accept-target", "replay.accept", ("row_id",), 0),
    ("accept-coerced-id", "replay.accept", ("row_id",), "1"),
    ("accept-column", "replay.accept_column", ("column_id",), 0),
    (
        "accept-column-coerced-id",
        "replay.accept_column",
        ("column_id",),
        True,
    ),
    ("dismiss-hash", "replay.dismiss", ("generated_value_hash",), _DELETE),
    ("dismiss-run", "replay.dismiss", ("run_id",), 0),
    ("dismiss-coerced-run", "replay.dismiss", ("run_id",), "1"),
)


def _action(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": f"{kind}@test",
    }


def _malformed(kind: str, path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    params = deepcopy(VALID_PARAMS[kind])
    parent: Any = params
    for key in path[:-1]:
        parent = parent[key]
    if value is _DELETE:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    return params


def test_valid_mutations_preserve_raw_params_and_hashes() -> None:
    for kind, params in VALID_PARAMS.items():
        raw = _action(kind, params)
        first = typed_action_for_request(raw)
        second = typed_action_for_request(deepcopy(raw))

        assert first.params.model_dump(mode="json", exclude_unset=True) == params, kind
        assert typed_request_hash(first) == typed_request_hash(second), kind


def test_malformed_mutations_stop_before_executor_and_database(
    tmp_path, monkeypatch
) -> None:
    del monkeypatch

    project = Project.create(tmp_path / "malformed.frisket")
    tables = ("sheets", "columns", "rows", "runs", "results", "ops", "receipts")

    def counts() -> dict[str, int]:
        return {
            table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    before = counts()
    try:
        for label, kind, path, value in MALFORMED:
            result = executor_actions.run_action_spec(
                project,
                _action(kind, _malformed(kind, path, value)),
                project_id="malformed",
            )
            assert result.status == "failed", label
            assert result.errors, label
        assert counts() == before
    finally:
        project.close()


@pytest.mark.parametrize(
    ("model", "params", "expected"),
    [
        (
            ReviewDecisionParams,
            {
                "run_id": 1,
                "row_id": 2,
                "column_id": 3,
                "decision": "edit",
                "value": None,
            },
            {
                "run_id": 1,
                "row_id": 2,
                "column_id": 3,
                "decision": "edit",
                "value": None,
                "note": None,
            },
        ),
        (
            ReplayAcceptParams,
            {
                "sheet_id": 1,
                "row_id": 2,
                "column_id": 3,
                "run_id": 4,
                "generated_value_hash": _HASH,
            },
            {
                "sheet_id": 1,
                "row_id": 2,
                "column_id": 3,
                "run_id": 4,
                "generated_value_hash": _HASH,
            },
        ),
        (
            ReplayAcceptColumnParams,
            {"sheet_id": 1, "column_id": 3},
            {"sheet_id": 1, "column_id": 3},
        ),
        (
            ReplayDismissParams,
            {
                "sheet_id": 1,
                "row_id": 2,
                "column_id": 3,
                "run_id": 4,
                "generated_value_hash": _HASH,
            },
            {
                "sheet_id": 1,
                "row_id": 2,
                "column_id": 3,
                "run_id": 4,
                "generated_value_hash": _HASH,
            },
        ),
        (
            CellEditParams,
            {"edits": [{"row_id": 1, "column_id": 2, "value": None}]},
            {"edits": [{"row_id": 1, "column_id": 2, "value": None}]},
        ),
        (
            CellEditQueryParams,
            {"query": {}, "column_id": 1, "value": None},
            {"query": {}, "column_id": 1, "value": None},
        ),
        (
            ColumnPatchParams,
            {"column_id": 2, "sheet_id": 1, "format": "markdown"},
            {"column_id": 2, "sheet_id": 1, "format": "markdown"},
        ),
        (
            ColumnAddParams,
            {"sheet_id": 1, "name": " notes ", "type": "boolean"},
            {"sheet_id": 1, "name": "notes", "type": "boolean", "position": None},
        ),
        (
            ColumnSetTypeParams,
            {"column_id": 2, "sheet_id": 1, "type": "url"},
            {"column_id": 2, "sheet_id": 1, "type": "link"},
        ),
        (
            RowAddParams,
            {"sheet_id": 1, "cells": {"name": "Ada"}},
            {"sheet_id": 1, "cells": {"name": "Ada"}},
        ),
        (
            RowDeleteParams,
            {"sheet_id": 1, "row_ids": [2, 1, 2]},
            {"sheet_id": 1, "row_ids": [2, 1]},
        ),
    ],
)
def test_typed_project_mutation_params_are_canonical(model, params, expected) -> None:
    assert model.model_validate(params).model_dump(mode="json") == expected


@pytest.mark.parametrize(
    ("model", "params"),
    [
        (CellEditParams, {"edits": []}),
        (CellEditParams, {"edits": [{"row_id": 1, "column_id": 2}]}),
        (
            CellEditParams,
            {"edits": [{"row_id": False, "column_id": 2, "value": None}]},
        ),
        (
            CellEditParams,
            {"edits": [{"row_id": "1", "column_id": 2, "value": None}]},
        ),
        (
            CellEditParams,
            {"edits": [{"row_id": 1, "column_id": 2, "value": object()}]},
        ),
        (
            CellEditParams,
            {
                "edits": [
                    {"row_id": 1, "column_id": 2, "value": None},
                    {"row_id": 1, "column_id": 2, "value": "duplicate"},
                ]
            },
        ),
        (CellEditQueryParams, {"query": [], "column_id": 1, "value": None}),
        (CellEditQueryParams, {"query": {}, "column_id": 1}),
        (
            CellEditQueryParams,
            {"query": {}, "column_id": 1, "value": object()},
        ),
        (CellEditQueryParams, {"query": {}, "column_id": "1", "value": None}),
        (ColumnPatchParams, {"column_id": True, "format": "markdown"}),
        (ColumnPatchParams, {"column_id": 1}),
        (ColumnAddParams, {"sheet_id": True, "name": "notes"}),
        (ColumnAddParams, {"sheet_id": 1, "name": "notes", "position": "1"}),
        (ColumnSetTypeParams, {"column_id": True, "type": "date"}),
        (ColumnSetTypeParams, {"column_id": 1, "sheet_id": "1", "type": "date"}),
        (ColumnSetTypeParams, {"column_id": 1, "type": ""}),
        (RowAddParams, {"sheet_id": True, "cells": {}}),
        (RowAddParams, {"sheet_id": 1, "cells": []}),
        (RowAddParams, {"sheet_id": 1, "cells": {"": "x"}}),
        (RowAddParams, {"sheet_id": 1, "cells": {"x": object()}}),
        (RowAddParams, {"sheet_id": 1, "cells": {str(i): None for i in range(257)}}),
        (RowDeleteParams, {"sheet_id": 1, "row_ids": [False]}),
        (RowDeleteParams, {"sheet_id": 1, "row_ids": ["1"]}),
        (RowDeleteParams, {"sheet_id": 1, "row_ids": list(range(1, 10_002))}),
    ],
)
def test_typed_project_mutation_params_reject_malformed_values(model, params) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(params)


def test_cell_mutation_results_do_not_expose_host_receipt_facts() -> None:
    assert {field.name for field in fields(RetypedColumn)} == {
        "sheet_id",
        "sheet_name",
        "column_id",
        "column_name",
        "type_before",
        "type_after",
        "op_id",
    }
    assert {field.name for field in fields(EditedCells)} == {"op_id", "targets"}
    assert {field.name for field in fields(QueryEditedCells)} == {
        "sheet_id",
        "column_id",
        "column_name",
        "query_hash",
        "row_ids",
        "op_id",
    }

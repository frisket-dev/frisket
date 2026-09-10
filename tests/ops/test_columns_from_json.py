"""Behavior and lifecycle coverage for the typed ``map.columns_from_json``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from action_test_helpers import run_typed_map_request, typed_map_request
from frisket.actions.types import ActionRequest, Row, SheetRows
from frisket.actions.extract import (
    COLUMNS_FROM_JSON,
    ColumnsFromJsonParams,
    columns_from_json,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    build_typed_map_rows_plan,
)
from frisket.engine.store import Project


def _params(*, source: str = "api_result", routes: list[dict] | None = None):
    return ColumnsFromJsonParams(
        source_column=source,
        routes=routes or [{"name": "city", "path": "$.city"}],
    )


def _request(
    sheet_id: int,
    *,
    source: str = "api_result",
    routes: list[dict] | None = None,
) -> ActionRequest:
    return ActionRequest(
        action_id="map.columns_from_json",
        scope=SheetRows(sheet_id=sheet_id),
        params={
            "source_column": source,
            "routes": routes or [{"name": "city", "path": "$.city"}],
        },
        idempotency_key="columns-from-json@1",
    )


def _plan(project: Project, request: ActionRequest):
    return build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )


def test_registered_catalog_derives_json_source_and_dynamic_outputs() -> None:
    registered = ACTION_REGISTRY.get("map.columns_from_json")
    entry = registered.catalog_entry()

    assert registered.definition is COLUMNS_FROM_JSON
    assert entry["ui_hints"]["source_requirements"] == [
        {
            "id": "source_column",
            "param": "source_column",
            "label": "Source column",
            "mode": "column",
            "min": 1,
            "accepted_column_types": ["json"],
        }
    ]
    assert entry["ui_hints"]["dynamic_outputs"] is True


def test_output_fields_are_json_and_follow_route_names() -> None:
    fields = COLUMNS_FROM_JSON.run.resolve_output_fields(
        _params(
            routes=[
                {"name": "city", "path": "$.address.city"},
                {"name": "raw", "path": "$"},
            ]
        )
    )

    assert [(field.key, field.column_type, dict(field.schema)) for field in fields] == [
        ("city", "json", {}),
        ("raw", "json", {}),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "confidence",
        "score_confidence",
        "outcome",
        "justification",
        "error",
        "error_code",
        "__field_errors__",
    ],
)
def test_route_names_reject_runner_control_metadata(name: str) -> None:
    with pytest.raises(ValidationError, match="row metadata"):
        _params(routes=[{"name": name, "path": "$.value"}])


def test_duplicate_trimmed_route_names_are_rejected() -> None:
    with pytest.raises(ValidationError, match="route output names must be unique"):
        _params(
            routes=[
                {"name": "city", "path": "$.city"},
                {"name": " city ", "path": "$.fallback_city"},
            ]
        )


def test_route_family_rejects_justification_sidecar_collision() -> None:
    with pytest.raises(ValidationError, match="route output names must be unique"):
        _params(
            routes=[
                {"name": "score", "path": "$.score"},
                {"name": "score_justification", "path": "$.reason"},
            ]
        )


@pytest.mark.parametrize(
    "routes",
    [
        [],
        ["city"],
        [{"path": "$.city"}],
        [{"name": " ", "path": "$.city"}],
        [{"name": "city"}],
        [{"name": "city", "path": "  "}],
    ],
)
def test_typed_params_reject_invalid_route_shapes(routes: list[object]) -> None:
    with pytest.raises(ValidationError):
        ColumnsFromJsonParams(source_column="api_result", routes=routes)


def test_invalid_routes_fail_before_creating_a_run(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "invalid-columns.frisket")
    try:
        sheet_id = project.add_sheet("data")
        project.add_column(sheet_id, "api_result", type="json")
        request = ActionRequest.model_validate(
            typed_map_request(
                "map.columns_from_json",
                sheet_id,
                params={
                    "source_column": "api_result",
                    "routes": [
                        {"name": "score", "path": "$.score"},
                        {"name": "score_justification", "path": "$.reason"},
                    ],
                },
                output_names={},
                idempotency_key="invalid-columns@1",
            )
        )

        with pytest.raises(ValidationError, match="route output names must be unique"):
            BoundTypedActionRequest.bind(
                ACTION_REGISTRY.get(request.action_id), request
            )
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


def test_direct_python_extracts_nested_and_array_paths() -> None:
    result = columns_from_json(
        _params(
            routes=[
                {"name": "city", "path": "$.address.city"},
                {"name": "first_tag", "path": "$.tags[0]"},
                {"name": "last_tag", "path": "$.tags[-1]"},
            ]
        ),
        Row(
            {
                "api_result": {
                    "address": {"city": "Paris"},
                    "tags": ["alpha", "beta", "gamma"],
                }
            }
        ),
    )

    assert result.output.root == {
        "city": "Paris",
        "first_tag": "alpha",
        "last_tag": "gamma",
    }


@pytest.mark.parametrize("source", [None, "not-json"])
def test_direct_python_non_structured_source_yields_none(source: object) -> None:
    result = columns_from_json(
        _params(
            routes=[
                {"name": "city", "path": "$.address.city"},
                {"name": "zip", "path": "$.address.zip"},
            ]
        ),
        Row({"api_result": source}),
    )

    assert result.output.root == {"city": None, "zip": None}


def test_direct_python_missing_path_yields_none() -> None:
    result = columns_from_json(
        _params(
            routes=[
                {"name": "city", "path": "$.address.city"},
                {"name": "zip", "path": "$.address.zip"},
            ]
        ),
        Row({"api_result": {"address": {"city": "Paris"}}}),
    )

    assert result.output.root == {"city": "Paris", "zip": None}


def _sheet_with_json_column(tmp_path: Path) -> tuple[Project, int]:
    project = Project.create(tmp_path / "cfj.frisket")
    sheet_id = project.add_sheet("data")
    project.add_column(sheet_id, "api_result", type="json")
    project.add_column(sheet_id, "note", type="text")
    return project, sheet_id


def test_plan_rejects_missing_source_column(tmp_path: Path) -> None:
    project, sheet_id = _sheet_with_json_column(tmp_path)
    try:
        with pytest.raises(TypedMapRowsPlanError) as excinfo:
            _plan(project, _request(sheet_id, source="missing"))
        assert excinfo.value.code == "invalid_input_ref"
        assert excinfo.value.details == {"columns": ["missing"]}
    finally:
        project.close()


def test_plan_rejects_non_json_source_column(tmp_path: Path) -> None:
    project, sheet_id = _sheet_with_json_column(tmp_path)
    try:
        with pytest.raises(TypedMapRowsPlanError) as excinfo:
            _plan(project, _request(sheet_id, source="note"))
        assert excinfo.value.code == "invalid_input_ref"
        assert excinfo.value.details["columns"][0]["actual_type"] == "text"
    finally:
        project.close()


def test_plan_accepts_default_hidden_json_source(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "default-hidden.frisket")
    try:
        sheet_id = project.add_sheet("data")
        source_id = project.add_column(
            sheet_id, "api_result", type="json", default_hidden=True
        )

        plan = _plan(project, _request(sheet_id))

        assert plan.source_columns == ("api_result",)
        assert plan.source_column_ids == {"api_result": source_id}
        assert plan.source_column_types == {"api_result": "json"}
    finally:
        project.close()


def test_plan_rejects_truly_hidden_json_source(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "hidden.frisket")
    try:
        sheet_id = project.add_sheet("data")
        project.add_column(sheet_id, "plumbing", type="json", hidden=True)

        with pytest.raises(TypedMapRowsPlanError) as excinfo:
            _plan(project, _request(sheet_id, source="plumbing"))
        assert excinfo.value.code == "invalid_input_ref"
        assert excinfo.value.details == {"columns": ["plumbing"]}
    finally:
        project.close()


def test_typed_lifecycle_persists_values_receipt_and_replays(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "columns-lifecycle.frisket")
    try:
        sheet_id = project.add_sheet("data")
        source_id = project.add_column(sheet_id, "api_result", type="json")
        row_ids = project.add_rows(
            sheet_id,
            [
                {"api_result": {"address": {"city": "Paris"}}},
                {"api_result": {"address": {}}},
            ],
            {"api_result": source_id},
        )
        request = typed_map_request(
            "map.columns_from_json",
            sheet_id,
            params={
                "source_column": "api_result",
                "routes": [{"name": "city", "path": "$.address.city"}],
            },
            output_names={"city": "location"},
            idempotency_key="columns-lifecycle@1",
        )

        first = run_typed_map_request(project, request, project_id="columns-lifecycle")
        replay = run_typed_map_request(project, request, project_id="columns-lifecycle")

        assert first.status == "completed", first.errors
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.run_id == first.run_id
        assert [(output.kind, output.name) for output in first.outputs] == [
            ("column", "location")
        ]
        output_id = first.outputs[0].column_id
        assert output_id is not None
        assert project.get_values(sheet_id, output_id, row_ids=row_ids) == {
            row_ids[0]: "Paris",
            row_ids[1]: None,
        }
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    finally:
        project.close()

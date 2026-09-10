from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Outcome,
    Row,
    RowResult,
    SheetRows,
    Template,
)
from frisket.ai.llm import ModelRouter
from frisket.actions.system import BoundTypedActionRequest
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    build_typed_map_rows_plan,
)
from frisket.engine.runner import MapRunner, validation
from frisket.engine.runner.row_execution import AdaptiveThrottle, execute_row
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import OpContext, Recipe


class RenderParams(ActionParams):
    source: ColumnRef[str]
    template: Template[str]


class RenderOutput(BaseModel):
    rendered: str


def render(params: RenderParams, row: Row) -> RowResult[RenderOutput]:
    return RowResult(
        output=RenderOutput(
            rendered=f"{params.source.read(row)} / {params.template.render(row)}"
        )
    )


class AssessedParams(ActionParams):
    source: ColumnRef[str]


class AssessedOutput(BaseModel):
    cleaned: Outcome[str]


async def assess(params: AssessedParams, row: Row) -> RowResult[AssessedOutput]:
    return RowResult(
        output=AssessedOutput(
            cleaned=Outcome.ok(
                params.source.read(row).upper(),
                confidence=0.75,
                justification="normalized",
            )
        )
    )


class DateOutput(BaseModel):
    parsed: dt.date


def parse_date(params: AssessedParams, row: Row) -> RowResult[DateOutput]:
    del params, row
    return RowResult(output=DateOutput(parsed=dt.date(2026, 9, 4)))


class MultipleOutput(BaseModel):
    first: Outcome[str]
    second: Outcome[str]
    error: str
    score_confidence: str


def assess_multiple(params: AssessedParams, row: Row) -> RowResult[MultipleOutput]:
    value = params.source.read(row)
    return RowResult(
        output=MultipleOutput(
            first=Outcome.ok(value.upper(), confidence=0.9, justification="first"),
            second=(
                Outcome.failed("second_unavailable", "second could not be produced")
                if value == "two"
                else Outcome.ok(value[::-1], confidence=0.4, justification="second")
            ),
            error=f"literal error value for {value}",
            score_confidence=f"literal confidence value for {value}",
        )
    )


REGISTRY = ActionRegistry(
    [
        ActionNamespace(
            "map",
            actions=[
                action(
                    name="render_test",
                    title="Render test",
                    description="Render admitted values.",
                    category=ActionCategory.TEXT,
                    run=map_rows(render),
                ),
                action(
                    name="assess_test",
                    title="Assess test",
                    description="Return an assessed value.",
                    category=ActionCategory.CLEANUP,
                    run=map_rows(assess),
                ),
                action(
                    name="date_test",
                    title="Date test",
                    description="Return a typed date.",
                    category=ActionCategory.CLEANUP,
                    run=map_rows(parse_date),
                ),
                action(
                    name="multiple_test",
                    title="Multiple test",
                    description="Return independent typed outcomes.",
                    category=ActionCategory.CLEANUP,
                    run=map_rows(assess_multiple),
                ),
            ],
        )
    ]
)


@pytest.fixture
def project(tmp_path: Path):
    value = Project.create(tmp_path / "typed-map-rows.frisket")
    sheet_id = value.add_sheet("data")
    source_id = value.add_column(sheet_id, "source")
    note_id = value.add_column(sheet_id, "note")
    row_ids = value.add_rows(
        sheet_id,
        [
            {"source": "one", "note": "first"},
            {"source": "two", "note": "second"},
        ],
        {"source": source_id, "note": note_id},
    )
    yield value, sheet_id, row_ids
    value.close()


def _runner(project: Project) -> MapRunner:
    return MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def _forbid_registry_lookup(*_args: Any, **_kwargs: Any) -> Recipe:
    raise AssertionError("typed map_rows programs must not use the recipe registry")


class _LegacyControlRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"
    llm = False

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [{"name": "first"}, {"name": "second"}]

    async def execute(
        self,
        row_values: dict[str, Any],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[str, Any]:
        del row_values, spec, ctx
        return {"first": "one", "second": "two", "first_confidence": 0.3}


def test_prepared_publication_seam_leaves_legacy_control_behavior_unchanged(
    project: tuple[Project, int, list[int]],
) -> None:
    store, _sheet_id, _row_ids = project

    cells = asyncio.run(
        execute_row(
            store,
            ModelRouter(cache=None, cache_mode="off"),
            AdaptiveThrottle(),
            _LegacyControlRecipe(llm=False),
            {},
            {"action_kind": "map.legacy_test"},
            OpContext(project=store),
        )
    )

    assert cells["first"]["confidence"] == 0.3
    assert cells["second"]["confidence"] == 0.3


def test_plan_resolves_semantic_refs_and_logical_output_names(
    project: tuple[Project, int, list[int]],
) -> None:
    store, sheet_id, row_ids = project
    request = ActionRequest(
        action_id="map.render_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source", "template": {"text": "{{note}}"}},
        output_names={"rendered": "combined"},
        idempotency_key="render@1",
    )

    plan = build_typed_map_rows_plan(
        store, BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)
    )

    assert plan.source_columns == ("source", "note")
    assert plan.source_column_types == {"source": "text", "note": "text"}
    assert plan.output_names == {"rendered": "combined"}
    assert plan.output_fields[0]["name"] == "combined"
    assert plan.spec_dict() == {
        "action_kind": "map.render_test",
        "action_version": "1",
        "sheet_id": sheet_id,
        "input_columns": ["source", "note"],
        "params": {"source": "source", "template": {"text": "{{note}}"}},
        "output_names": {"rendered": "combined"},
        "output_target_preconditions": {"combined": None},
        "replace_existing": False,
        "row_ids": row_ids,
    }


def test_program_previews_sync_handler_without_registry_or_writes(
    project: tuple[Project, int, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sheet_id, row_ids = project
    request = ActionRequest(
        action_id="map.render_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source", "template": {"text": "{{note}}"}},
        output_names={"rendered": "combined"},
        idempotency_key="render@preview",
    )
    plan = build_typed_map_rows_plan(
        store, BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)
    )
    before = [column["name"] for column in store.columns(sheet_id)]
    monkeypatch.setattr(validation, "recipe_for_spec", _forbid_registry_lookup)

    preview = asyncio.run(
        _runner(store).preview(plan.spec_dict(), program=plan.program)
    )

    assert preview.row_ids == row_ids
    assert [preview.values[row_id]["combined"]["value"] for row_id in row_ids] == [
        "one / first",
        "two / second",
    ]
    assert [column["name"] for column in store.columns(sheet_id)] == before


def test_program_projects_outcome_metadata_and_async_handler(
    project: tuple[Project, int, list[int]],
) -> None:
    store, sheet_id, row_ids = project
    request = ActionRequest(
        action_id="map.assess_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source"},
        output_names={"cleaned": "upper"},
        idempotency_key="assess@preview",
    )
    plan = build_typed_map_rows_plan(
        store, BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)
    )

    preview = asyncio.run(
        _runner(store).preview(plan.spec_dict(), program=plan.program)
    )

    assert preview.values[row_ids[0]]["upper"] == {
        "value": "ONE",
        "tokens_in": None,
        "tokens_out": None,
        "cost": 0.0,
        "confidence": 0.75,
        "justification": "normalized",
    }


def test_program_keeps_independent_outcomes_and_control_looking_values(
    project: tuple[Project, int, list[int]],
) -> None:
    store, sheet_id, row_ids = project
    request = ActionRequest(
        action_id="map.multiple_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source"},
        idempotency_key="multiple@preview",
    )
    plan = build_typed_map_rows_plan(
        store, BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)
    )

    preview = asyncio.run(
        _runner(store).preview(plan.spec_dict(), program=plan.program)
    )

    first = preview.values[row_ids[0]]
    assert first["first"]["confidence"] == 0.9
    assert first["first"]["justification"] == "first"
    assert first["second"]["confidence"] == 0.4
    assert first["second"]["justification"] == "second"
    assert first["error"]["value"] == "literal error value for one"
    assert "error" not in first["error"]
    assert first["score_confidence"]["value"] == "literal confidence value for one"
    assert "confidence" not in first["score_confidence"]

    second = preview.values[row_ids[1]]
    assert second["first"]["value"] == "TWO"
    assert second["second"]["value"] is None
    assert second["second"]["error"] == "second could not be produced"
    assert second["second"]["error_code"] == "second_unavailable"
    assert second["error"]["value"] == "literal error value for two"


def test_output_schema_is_projected_from_typed_output_model(
    project: tuple[Project, int, list[int]],
) -> None:
    store, sheet_id, row_ids = project
    request = ActionRequest(
        action_id="map.date_test",
        scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
        params={"source": "source"},
        output_names={"parsed": "day"},
        idempotency_key="date@preview",
    )

    plan = build_typed_map_rows_plan(
        store, BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)
    )

    assert plan.output_fields[0]["column_type"] == "date"
    assert plan.output_fields[0]["schema"] == {
        "format": "date",
        "type": "string",
    }


@pytest.mark.parametrize(
    ("params", "output_names", "code", "columns"),
    [
        (
            {"source": "missing", "template": {"text": "literal"}},
            {"rendered": "combined"},
            "invalid_input_ref",
            ["missing"],
        ),
        (
            {"source": "source", "template": {"text": "{{unknown}}"}},
            {"rendered": "combined"},
            "invalid_input_ref",
            ["unknown"],
        ),
        (
            {"source": "source", "template": {"text": "literal"}},
            {"rendered": "note"},
            "output_column_exists",
            ["note"],
        ),
    ],
)
def test_plan_refuses_missing_sources_and_output_collisions(
    project: tuple[Project, int, list[int]],
    params: dict[str, str],
    output_names: dict[str, str],
    code: str,
    columns: list[str],
) -> None:
    store, sheet_id, _ = project
    request = ActionRequest(
        action_id="map.render_test",
        scope=SheetRows(sheet_id=sheet_id),
        params=params,
        output_names=output_names,
        idempotency_key=f"failure@{code}",
    )

    with pytest.raises(TypedMapRowsPlanError) as caught:
        build_typed_map_rows_plan(
            store,
            BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request),
        )

    assert caught.value.code == code
    assert caught.value.details == {"columns": columns}


def test_plan_refuses_a_column_with_an_incompatible_stored_type(
    project: tuple[Project, int, list[int]],
) -> None:
    store, sheet_id, _ = project
    store.add_column(sheet_id, "count", type="integer")
    request = ActionRequest(
        action_id="map.assess_test",
        scope=SheetRows(sheet_id=sheet_id),
        params={"source": "count"},
        output_names={"cleaned": "cleaned_count"},
        idempotency_key="wrong-source-type",
    )

    with pytest.raises(TypedMapRowsPlanError) as caught:
        build_typed_map_rows_plan(
            store,
            BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request),
        )

    assert caught.value.code == "invalid_input_ref"
    assert caught.value.details == {
        "columns": [
            {
                "name": "count",
                "actual_type": "integer",
                "accepted_column_types": ["text"],
            }
        ]
    }

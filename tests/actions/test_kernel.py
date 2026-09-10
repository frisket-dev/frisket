from __future__ import annotations

import datetime as dt
from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel, Field, ValidationError

from frisket.actions.core import ActionRegistry
from frisket.actions.grounding_types import EvidenceClaim
from frisket.actions.types import (
    ActionRequest,
    InputReference,
    ProjectScope,
    SheetRows,
    discover_references,
)
from frisket.sdk import (
    ActionCategory,
    ActionNamespace,
    ActionParams,
    ColumnRef,
    Outcome,
    Row,
    RowResult,
    RowScope,
    SourceCreator,
    SourceRecord,
    Template,
    action,
    map_rows,
)
from frisket.contracts.action import ActionCatalogEntry


class TemplateParams(ActionParams):
    template: Template[str]


class TemplateOutput(BaseModel):
    rendered: str


def render_template(params: TemplateParams, row: Row) -> RowResult[TemplateOutput]:
    return RowResult(output=TemplateOutput(rendered=params.template.render(row)))


class CleanDatesParams(ActionParams):
    source: ColumnRef[str]
    format: str | None = None


class CleanDatesOutput(BaseModel):
    parsed: Outcome[dt.date | None]


class UnsupportedOutput(BaseModel):
    values: set[str]


class DefaultedOutput(BaseModel):
    value: str = ""


class MultipleAssessedOutput(BaseModel):
    first: Outcome[str]
    second: Outcome[str]
    error: str
    score_confidence: str


class BadParams(ActionParams):
    sheet_id: int


class LooseParams(BaseModel):
    value: str


class ListedSemanticParams(ActionParams):
    sources: list[ColumnRef[str]]


class OptionalSemanticParams(ActionParams):
    source: ColumnRef[str] | None = None


class NestedSemanticConfig(BaseModel):
    template: Template[str]


class NestedModelParams(ActionParams):
    config: NestedSemanticConfig | None = None


class MixedSourceParams(ActionParams):
    template: Template[str]
    source: ColumnRef[int | float]


class ProjectCreateParams(ActionParams):
    name: str


def create_project_source(
    params: ProjectCreateParams, sources: SourceCreator
) -> SourceRecord:
    return sources.create(
        name=params.name,
        kind="plugin",
        url=None,
        config={},
        sheet_id=None,
        schedule=None,
        enabled=True,
    )


def unsupported_project_capability(
    params: ProjectCreateParams, capability: object
) -> SourceRecord:
    del params, capability
    raise AssertionError


def clean_dates(params: CleanDatesParams, row: Row) -> RowResult[CleanDatesOutput]:
    return RowResult(
        output=CleanDatesOutput(
            parsed=Outcome.ok(dt.date.fromisoformat(params.source.read(row)))
        )
    )


TEMPLATE = action(
    name="template",
    title="Template",
    description="Build text from values in each row.",
    category=ActionCategory.TEXT,
    run=map_rows(render_template),
)
CLEAN_DATES = action(
    name="clean_dates",
    title="Clean dates",
    description="Parse dates.",
    category=ActionCategory.CLEANUP,
    run=map_rows(clean_dates),
)


def test_semantic_values_read_render_and_discover_references() -> None:
    params = TemplateParams(template={"text": "{{name}} / {{date}} / {{name}}"})
    row = Row({"name": "Ada", "date": "1815-12-10"})

    assert params.template.render(row) == "Ada / 1815-12-10 / Ada"
    assert [ref.column for ref in discover_references(params)] == ["name", "date"]
    assert CleanDatesParams(source="date").source.read(row) == "1815-12-10"
    with pytest.raises(KeyError, match="admitted column"):
        ColumnRef[str]("missing").read(row)


def test_literal_only_template_is_valid_and_references_nothing() -> None:
    template = Template[str](text="Unknown")

    assert template.render(Row({})) == "Unknown"
    assert template.references() == ()


def test_discovered_column_refs_carry_and_merge_their_type_contract() -> None:
    params = MixedSourceParams(template={"text": "{{source}}"}, source="source")
    references = discover_references(params)

    def mixed_source(params: MixedSourceParams, row: Row) -> RowResult[TemplateOutput]:
        return RowResult(output=TemplateOutput(rendered=params.template.render(row)))

    definition = action(
        name="mixed_source",
        title="Mixed source",
        description="Read numeric source columns in a template action.",
        category=ActionCategory.TEXT,
        run=map_rows(mixed_source),
    )
    requirements = (
        ActionRegistry([ActionNamespace("map", actions=[definition])])
        .get("map.mixed_source")
        .catalog_entry()["ui_hints"]["source_requirements"]
    )

    assert references == (InputReference("source", ("integer", "number")),)
    assert references[0].accepted_column_types is not None
    assert "integer" in references[0].accepted_column_types
    assert "text" not in references[0].accepted_column_types
    assert requirements[1]["accepted_column_types"] == ["integer", "number"]


def test_registry_binds_canonical_ids_and_projects_existing_catalog_shape() -> None:
    registry = ActionRegistry([ActionNamespace("map", actions=[TEMPLATE, CLEAN_DATES])])

    assert registry.get("map.template").definition is TEMPLATE
    entries = {action.action_id: action.catalog_entry() for action in registry.actions}
    assert entries["map.template"]["ui_hints"] == {
        "form": "generated",
        "category": "text",
        "semantic_controls": {"template": "template"},
        "source_requirements": [
            {
                "id": "template",
                "param": "template",
                "label": "Template",
                "mode": "template",
                "min": 0,
                "template_columns": "union",
            }
        ],
        "logical_outputs": [{"key": "rendered", "column_type": "text"}],
    }
    assert entries["map.clean_dates"]["ui_hints"]["source_requirements"] == [
        {
            "id": "source",
            "param": "source",
            "label": "Source",
            "mode": "column",
            "min": 1,
            "accepted_column_types": ["text"],
        }
    ]
    assert entries["map.clean_dates"]["ui_hints"]["logical_outputs"] == [
        {"key": "parsed", "column_type": "date"}
    ]
    assert entries["map.clean_dates"]["ui_hints"]["category"] == "cleanup"
    assert "write_map_op" in entries["map.template"]["side_effects"]
    assert {error["code"] for error in entries["map.template"]["errors"]} >= {
        "invalid_params",
        "invalid_input_ref",
        "output_column_exists",
        "map_rows_failed",
        "idempotency_conflict",
    }
    for entry in entries.values():
        ActionCatalogEntry.model_validate(entry)


def test_registered_action_rebuilds_params_and_checks_request_owned_names() -> None:
    registered = ActionRegistry([ActionNamespace("map", actions=[TEMPLATE])]).get(
        "map.template"
    )
    request = ActionRequest(
        action_id="map.template",
        scope=SheetRows(sheet_id=7, row_ids=(2, 3)),
        params={"template": {"text": "Hello {{name}}"}},
        output_names={"rendered": "greeting"},
        idempotency_key="request-1",
    )

    params = registered.validate_request(request)
    assert isinstance(params, TemplateParams)
    assert params.template.text == "Hello {{name}}"

    with pytest.raises(ValueError, match="does not match registered action"):
        registered.validate_request(
            request.model_copy(update={"action_id": "map.other"})
        )
    with pytest.raises(ValueError, match="unknown output names"):
        registered.validate_request(
            request.model_copy(update={"output_names": {"other": "greeting"}})
        )
    with pytest.raises(ValueError, match="sheet_rows"):
        registered.validate_request(
            request.model_copy(update={"scope": {"kind": "project"}})
        )


def test_project_callable_infers_one_source_capability_and_rejects_row_controls() -> (
    None
):
    definition = action(
        name="create",
        title="Create",
        description="Create a source.",
        category=ActionCategory.SOURCES,
        run=create_project_source,
        form="source_create",
    )
    registered = ActionRegistry([ActionNamespace("source", actions=[definition])]).get(
        "source.create"
    )
    request = ActionRequest(
        action_id="source.create",
        scope=ProjectScope(),
        params={"name": "Feed"},
        idempotency_key="create-1",
    )

    assert registered.validate_request(request).model_dump() == {"name": "Feed"}
    assert registered.catalog_entry()["ui_hints"]["form"] == "source_create"
    with pytest.raises(ValueError, match="project scope"):
        registered.validate_request(
            request.model_copy(update={"scope": SheetRows(sheet_id=1)})
        )
    with pytest.raises(ValueError, match="output_names"):
        registered.validate_request(
            request.model_copy(update={"output_names": {"source": "other"}})
        )
    with pytest.raises(ValueError, match="replace_existing"):
        registered.validate_request(
            request.model_copy(update={"replace_existing": True})
        )


def test_project_callable_rejects_unknown_capability_annotation() -> None:
    with pytest.raises(TypeError, match="admitted capabilities"):
        action(
            name="unsupported",
            title="Unsupported",
            description="Reject unknown authority.",
            category=ActionCategory.SOURCES,
            run=unsupported_project_capability,
        )


def test_all_rows_actions_reject_a_membership_scope() -> None:
    definition = action(
        name="whole_sheet",
        title="Whole sheet",
        description="Use every visible row.",
        category=ActionCategory.CLEANUP,
        row_scope=RowScope.ALL_ROWS,
        run=map_rows(render_template),
    )
    registered = ActionRegistry([ActionNamespace("map", actions=[definition])]).get(
        "map.whole_sheet"
    )

    assert registered.catalog_entry()["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows"],
    }
    with pytest.raises(ValueError, match="requires all rows"):
        registered.validate_request(
            ActionRequest(
                action_id="map.whole_sheet",
                scope=SheetRows(sheet_id=7, row_ids=(2, 3)),
                params={"template": {"text": "{{name}}"}},
                idempotency_key="whole-sheet@1",
            )
        )


def test_values_and_definitions_are_immutable() -> None:
    with pytest.raises((FrozenInstanceError, ValidationError)):
        TEMPLATE.name = "changed"  # type: ignore[misc]
    assert TemplateParams(template={"text": " padded "}).template.text == "padded"
    with pytest.raises(ValidationError):
        ActionRequest(
            action_id="map.template",
            scope={"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1, 1]},
            params={},
            idempotency_key="key",
        )


@pytest.mark.parametrize("failure", ["extra_parameter", "bad_row", "bad_return"])
def test_map_rows_rejects_unsupported_handler_signatures(failure: str) -> None:
    def extra_parameter(
        params: TemplateParams, row: Row, unknown: object
    ) -> RowResult[TemplateOutput]: ...

    def bad_row(
        params: TemplateParams, row: dict[str, object]
    ) -> RowResult[TemplateOutput]: ...

    def bad_return(params: TemplateParams, row: Row) -> TemplateOutput: ...

    with pytest.raises(TypeError):
        map_rows(locals()[failure])


def test_registration_rejects_unsupported_outputs_and_contract_drift() -> None:
    def loose_params(params: LooseParams, row: Row) -> RowResult[TemplateOutput]: ...

    with pytest.raises(TypeError, match="inherit ActionParams"):
        map_rows(loose_params)

    def unsupported(
        params: TemplateParams, row: Row
    ) -> RowResult[UnsupportedOutput]: ...

    def defaulted(params: TemplateParams, row: Row) -> RowResult[DefaultedOutput]: ...

    with pytest.raises(TypeError, match="unsupported output"):
        map_rows(unsupported)
    with pytest.raises(TypeError, match="must be required"):
        map_rows(defaulted)

    def multiple_assessed(
        params: TemplateParams, row: Row
    ) -> RowResult[MultipleAssessedOutput]: ...

    terminal = map_rows(multiple_assessed)
    assert [field.key for field in terminal.output_fields] == [
        "first",
        "second",
        "error",
        "score_confidence",
    ]

    def bad_params(params: BadParams, row: Row) -> RowResult[TemplateOutput]: ...

    with pytest.raises(TypeError, match="request-owned"):
        action(
            name="bad",
            title="Bad",
            description="Bad params.",
            category=ActionCategory.TEXT,
            run=map_rows(bad_params),
        )


@pytest.mark.parametrize("value", ["yes", "true", 1, 0])
def test_action_request_replace_existing_is_strict_boolean(value: object) -> None:
    body = {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"template": {"text": "{{body}}"}},
        "output_names": {"rendered": "rendered"},
        "replace_existing": value,
        "idempotency_key": "strict-replacement@1",
    }

    with pytest.raises(ValidationError):
        ActionRequest.model_validate(body)


def test_registration_supports_lists_of_column_refs() -> None:
    def listed_semantic(
        params: ListedSemanticParams, row: Row
    ) -> RowResult[TemplateOutput]: ...

    definition = action(
        name="listed_semantic",
        title="Listed semantic",
        description="Read several text columns.",
        category=ActionCategory.TEXT,
        run=map_rows(listed_semantic),
    )
    entry = (
        ActionRegistry([ActionNamespace("map", actions=[definition])])
        .actions[0]
        .catalog_entry()
    )

    assert entry["ui_hints"]["semantic_controls"] == {"sources": "columns"}
    assert entry["ui_hints"]["source_requirements"] == [
        {
            "id": "sources",
            "param": "sources",
            "label": "Sources",
            "mode": "columns",
            "min": 0,
            "accepted_column_types": ["text"],
        }
    ]
    params = ListedSemanticParams(sources=["first", "second"])
    assert discover_references(params) == (
        InputReference("first", ("text",)),
        InputReference("second", ("text",)),
    )


@pytest.mark.parametrize("minimum", [0, 2])
def test_column_list_requirement_uses_declared_length(minimum):
    class Params(ActionParams):
        sources: list[ColumnRef[str]] = Field(min_length=minimum)

    def listed(params, row: Row) -> RowResult[TemplateOutput]: ...

    listed.__annotations__["params"] = Params
    definition = action(
        name="listed",
        title="Listed",
        description="Read a constrained list of columns.",
        category=ActionCategory.TEXT,
        run=map_rows(listed),
    )
    registered = ActionRegistry(
        [ActionNamespace("test", actions=[definition])]
    ).actions[0]
    [requirement] = registered.catalog_entry()["ui_hints"]["source_requirements"]
    assert requirement["min"] == minimum
    Params(sources=["source"] * minimum)
    if minimum:
        with pytest.raises(ValidationError):
            Params(sources=[])


def test_registration_supports_optional_semantic_params() -> None:
    def optional_semantic(
        params: OptionalSemanticParams,
        row: Row,
    ) -> RowResult[TemplateOutput]: ...

    definition = action(
        name="optional_semantic",
        title="Optional semantic",
        description="Read a text column when provided.",
        category=ActionCategory.TEXT,
        run=map_rows(optional_semantic),
    )
    registered = ActionRegistry([ActionNamespace("map", actions=[definition])]).actions[
        0
    ]
    assert registered.catalog_entry()["ui_hints"]["semantic_controls"] == {
        "source": "column"
    }
    assert discover_references(OptionalSemanticParams()) == ()
    assert discover_references(OptionalSemanticParams(source="body")) == (
        InputReference("body", ("text",)),
    )


def test_registration_rejects_nested_semantic_params() -> None:
    def nested_semantic(params: BaseModel, row: Row) -> RowResult[TemplateOutput]: ...

    nested_semantic.__annotations__["params"] = NestedModelParams
    with pytest.raises(TypeError, match="nested semantic param"):
        action(
            name="nested_semantic",
            title="Nested semantic",
            description="Nested semantic refs are not catalogued.",
            category=ActionCategory.TEXT,
            run=map_rows(nested_semantic),
        )


def test_registry_rejects_duplicate_namespaces_ids_and_action_ownership() -> None:
    with pytest.raises(ValueError, match="duplicate action name"):
        ActionNamespace("map", actions=[TEMPLATE, TEMPLATE])
    with pytest.raises(ValueError, match="multiple namespaces"):
        ActionRegistry(
            [
                ActionNamespace("map", actions=[TEMPLATE]),
                ActionNamespace("text", actions=[TEMPLATE]),
            ]
        )


def test_outcome_keeps_successful_absence_distinct_from_failure() -> None:
    assert Outcome[dt.date | None].ok(None).status == "ok"
    failure = Outcome[dt.date | None].failed("invalid_date", "Could not parse")
    assert failure.status == "failed"
    with pytest.raises(ValidationError, match="code and message"):
        Outcome[str](status="failed", code="invalid", message="bad", value="value")
    with pytest.raises(ValidationError, match="String should match pattern"):
        Outcome[str].failed("Invalid code", "bad")


def test_result_annotations_validate_at_the_cell_outcome_boundary() -> None:
    output = TemplateOutput(rendered="value")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RowResult[TemplateOutput](output=output, warnings=("later",))
    with pytest.raises(ValidationError, match="EvidenceClaim"):
        Outcome[str](status="ok", value="value", evidence=("later",))
    claim = EvidenceClaim(quote="value", segment_indices=(0,))
    assessed = Outcome[str].ok("value", evidence=(claim,), warnings=("unverified",))
    assert assessed.evidence == (claim,)
    assert assessed.warnings == ("unverified",)
    with pytest.raises(ValidationError, match="code and message"):
        Outcome[str](status="failed", code="invalid", message="bad", evidence=(claim,))

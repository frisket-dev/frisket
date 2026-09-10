from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, ValidationError

from frisket.actions.core import ActionRegistry
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import root_action_catalog, validate_root_action
from frisket.actions.types import ActionRequest, DynamicTableResult
from frisket.sdk import (
    ActionCategory,
    ActionNamespace,
    ActionParams,
    Row,
    RowResult,
    SourceCreator,
    SourceRecord,
    TableResult,
    action,
    create_sheet,
    map_rows,
)


class ExampleParams(ActionParams):
    message: str = Field(alias="text")
    labels: list[str] = Field(default_factory=list)


class Output(BaseModel):
    value: str


def row_handler(params: ExampleParams, row: Row) -> RowResult[Output]:
    raise AssertionError("catalog examples must not execute row handlers")


def table_handler(params: ExampleParams) -> TableResult[Output]:
    raise AssertionError("catalog examples must not produce table rows")


def runtime_handler(params: ExampleParams) -> DynamicTableResult:
    raise AssertionError("catalog examples must not discover runtime schemas")


def project_handler(params: ExampleParams, sources: SourceCreator) -> SourceRecord:
    raise AssertionError("catalog examples must not invoke capabilities")


def _definition(*, examples=(), run=None):
    return action(
        name="describe",
        title="Describe",
        description="Demonstrate the typed authoring boundary.",
        category=ActionCategory.TEXT,
        run=run or map_rows(row_handler),
        examples=examples,
    )


def _register(definition):
    return ActionRegistry([ActionNamespace("demo", actions=[definition])]).get(
        "demo.describe"
    )


@pytest.mark.parametrize(
    "registered", ACTION_REGISTRY.actions, ids=lambda a: a.action_id
)
def test_every_typed_action_publishes_exact_schema_valid_examples(registered):
    entry = registered.catalog_entry()
    assert entry["examples"], f"{registered.action_id} needs a useful public example"
    request_schema = Draft202012Validator(ActionRequest.model_json_schema())
    params_schema = Draft202012Validator(entry["input_schema"])
    for example in entry["examples"]:
        request_schema.validate(example)
        params_schema.validate(example["params"])
        request = ActionRequest.model_validate(example)
        registered.bind_request(request)
        assert validate_root_action(example).ok
        assert request.action_id == registered.action_id
        assert request.idempotency_key.startswith(f"example:{registered.action_id}:")
        assert request.replace_existing is False
        assert (
            not {"confirmation", "kind", "schema_version", "capabilities", "row_scope"}
            & example.keys()
        )


def test_public_root_catalog_retains_all_typed_examples():
    entries = {entry.kind: entry for entry in root_action_catalog().actions}
    for registered in ACTION_REGISTRY.actions:
        assert (
            entries[registered.action_id].examples
            == registered.catalog_entry()["examples"]
        )


@pytest.mark.parametrize(
    "run,scope,sheet_name",
    [
        (map_rows(row_handler), {"kind": "sheet_rows", "sheet_id": 1}, None),
        (create_sheet(table_handler), {"kind": "project"}, "Example results"),
        (create_sheet(runtime_handler), {"kind": "project"}, "Example results"),
        (project_handler, {"kind": "project"}, None),
    ],
)
def test_examples_derive_request_fields_without_running_handlers(
    run, scope, sheet_name
):
    registered = _register(
        _definition(examples=(ExampleParams(text="Example"),), run=run)
    )
    [example] = registered.catalog_entry()["examples"]
    assert example == {
        "action_id": "demo.describe",
        "scope": scope,
        "params": {"text": "Example"},
        "output_names": {},
        "replace_existing": False,
        "idempotency_key": "example:demo.describe:1",
        **({"sheet_name": sheet_name} if sheet_name else {}),
    }
    registered.bind_request(ActionRequest.model_validate(example))


def test_examples_snapshot_authored_params_and_return_independent_json():
    params = ExampleParams(text="Original", labels=["one"])
    authored = [params]
    definition = _definition(examples=authored)
    params.labels.append("changed before registration")
    params.message = "Changed"
    authored.clear()
    registered = _register(definition)
    first = registered.catalog_entry()["examples"]
    assert first[0]["params"] == {"text": "Original", "labels": ["one"]}
    first[0]["params"]["labels"].append("changed after registration")
    first[0]["scope"]["sheet_id"] = 99
    first.clear()
    second = registered.catalog_entry()["examples"]
    assert second[0]["params"] == {"text": "Original", "labels": ["one"]}
    assert second[0]["scope"]["sheet_id"] == 1
    assert json.loads(json.dumps(second)) == second


@pytest.mark.parametrize("invalid", [{"text": "raw mapping"}, ActionParams()])
def test_examples_require_the_handlers_exact_params_model(invalid):
    with pytest.raises(TypeError, match="ExampleParams instances"):
        _definition(examples=(invalid,))


def test_registration_revalidates_constructed_params():
    unchecked = ExampleParams.model_construct(message=None)
    definition = _definition(examples=(unchecked,))
    with pytest.raises(ValidationError):
        _register(definition)


def test_registration_uses_output_binding_but_catalog_reads_do_not_rebind():
    calls = []

    def active_outputs(params):
        calls.append(params.message)
        return (params.message,)

    run = map_rows(row_handler, active_outputs=active_outputs)
    with pytest.raises(TypeError, match="unknown active outputs"):
        _register(_definition(examples=(ExampleParams(text="missing"),), run=run))
    calls.clear()
    registered = _register(
        _definition(examples=(ExampleParams(text="value"),), run=run)
    )
    assert calls == ["value"]
    registered.catalog_entry()
    registered.catalog_entry()
    assert calls == ["value"]


def test_omitted_fields_and_explicit_null_are_not_interchanged():
    [accepted, edited] = ACTION_REGISTRY.get("review.decision").catalog_entry()[
        "examples"
    ]
    assert "value" not in accepted["params"]
    assert edited["params"]["value"] is None
    assert validate_root_action(accepted).ok
    assert validate_root_action(edited).ok
    [updated] = ACTION_REGISTRY.get("source.update").catalog_entry()["examples"]
    assert updated["params"]["patch"] == {"enabled": False}


def test_examples_are_optional_for_new_authors():
    registered = _register(_definition())
    assert registered.catalog_entry()["examples"] == []

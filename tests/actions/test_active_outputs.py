from __future__ import annotations

import datetime as dt
from dataclasses import FrozenInstanceError
from enum import StrEnum
from typing import Any, Literal, Mapping

import pytest
from pydantic import BaseModel, Field, ValidationError

from frisket.actions.core import ActionRegistry, has_dynamic_outputs
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, DynamicOutput, SheetRows, StagedFile
from frisket.sdk import (
    ActionCategory,
    ActionNamespace,
    ActionParams,
    MediaMetadataReader,
    Outcome,
    Row,
    RowResult,
    action,
    map_rows,
)


class Params(ActionParams):
    expanded: bool = False


class Details(BaseModel):
    title: str
    tags: list[str]


class ConditionalOutput(BaseModel):
    details: Details
    title: str | None = None
    size: int | None = Field(default=None, json_schema_extra={"format": "filesize"})


def describe(params: Params, row: Row) -> RowResult[ConditionalOutput]:
    raise AssertionError("registration and binding must not invoke the handler")


def _registered(handler=describe, *, active_outputs=None):
    definition = action(
        name="describe",
        title="Describe",
        description="Describe an admitted row.",
        category=ActionCategory.CONVERT,
        run=map_rows(handler, active_outputs=active_outputs),
    )
    return ActionRegistry([ActionNamespace("test", actions=[definition])]).get(
        "test.describe"
    )


def test_active_subset_is_frozen_before_names_without_reauthoring_schema():
    selected = ["details"]
    calls = []

    def active(params):
        calls.append(params.expanded)
        return selected

    registered = _registered(active_outputs=active)
    terminal = registered.definition.run
    catalog = registered.catalog_entry()
    assert calls == []
    assert catalog["output_schema"] == ConditionalOutput.model_json_schema()
    assert catalog["ui_hints"]["logical_outputs"] == [
        {"key": "details", "column_type": "json"},
        {"key": "title", "column_type": "text"},
        {"key": "size", "column_type": "integer"},
    ]
    assert catalog["ui_hints"]["dynamic_outputs"] is True
    assert has_dynamic_outputs(terminal)
    request = ActionRequest(
        action_id="test.describe",
        scope=SheetRows(sheet_id=1),
        params={},
        output_names={"details": "Metadata"},
        idempotency_key="describe-1",
    )
    bound = BoundTypedActionRequest.bind(registered, request)
    assert calls == [False]
    assert bound.output_fields == (terminal.output_fields[0],)
    selected[:] = ["size", "title", "details"]
    assert [field.key for field in bound.output_fields] == ["details"]
    assert [
        field.key for field in terminal.resolve_output_fields(Params(expanded=True))
    ] == ["details", "title", "size"]
    assert bound.output_fields == (terminal.output_fields[0],)
    with pytest.raises(FrozenInstanceError):
        bound.output_fields[0].key = "changed"


def test_active_subset_rejects_inactive_renames_and_colliding_active_names():
    registered = _registered(
        active_outputs=lambda params: (
            ("details", "title") if params.expanded else ("details",)
        )
    )
    with pytest.raises(ValueError, match="unknown output names: title"):
        registered.bind_values(
            scope=SheetRows(sheet_id=1), params={}, output_names={"title": "Title"}
        )
    with pytest.raises(ValueError, match="final output names must be unique"):
        registered.bind_values(
            scope=SheetRows(sheet_id=1),
            params={"expanded": True},
            output_names={"details": "same", "title": "same"},
        )


@pytest.mark.parametrize(
    "selected",
    [
        (),
        "details",
        {"details": str},
        ["details", "details"],
        ["missing"],
        [" details"],
        [None],
    ],
)
def test_active_subset_must_be_unique_known_logical_keys(selected):
    terminal = map_rows(describe, active_outputs=lambda params: selected)
    with pytest.raises(TypeError):
        terminal.resolve_output_fields(Params())


@pytest.mark.parametrize(
    "resolver", [False, lambda: ("details",), lambda params, row: ("details",)]
)
def test_active_resolver_has_only_the_params_boundary(resolver):
    with pytest.raises(TypeError, match="active_outputs"):
        map_rows(describe, active_outputs=resolver)


def test_activation_and_dynamic_schema_cannot_supply_competing_authorities():
    def dynamic(params: Params, row: Row) -> RowResult[DynamicOutput]:
        raise AssertionError

    with pytest.raises(TypeError, match="mutually exclusive"):
        map_rows(
            dynamic,
            dynamic_outputs=lambda params: {"title": str},
            active_outputs=lambda params: ("title",),
        )
    with pytest.raises(TypeError, match="statically declared output"):
        map_rows(dynamic, active_outputs=lambda params: ("title",))

    async def asynchronous(params):
        return ("details",)

    with pytest.raises(TypeError, match="synchronous"):
        map_rows(describe, active_outputs=asynchronous)
    with pytest.raises(TypeError, match="must be required"):
        map_rows(describe)
    # Defaults remain ordinary model defaults; the runtime must require explicit
    # presence of active fields and ignore defaulted inactive fields.
    output = ConditionalOutput(details=Details(title="Report", tags=[]))
    assert output.model_fields_set == {"details"}
    assert output.title is None
    assert output.size is None


class Category(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"


class StructuredOutput(BaseModel):
    details: Details
    items: list[Details]
    mapping: Mapping[str, list[str]]
    arbitrary: dict[str, Any]
    assessed: Outcome[list[Details] | None]
    kind: Category
    status: Literal["ok", "missing"]
    captured: dt.datetime | None
    size: int = Field(json_schema_extra={"format": "filesize"})


def structured(params: Params, row: Row) -> RowResult[StructuredOutput]:
    raise AssertionError


def test_plain_structures_remain_single_columns_and_preserve_semantics():
    terminal = map_rows(structured)
    fields = {field.key: field for field in terminal.output_fields}
    assert {name: field.column_type for name, field in fields.items()} == {
        "details": "json",
        "items": "json",
        "mapping": "json",
        "arbitrary": "json",
        "assessed": "json",
        "kind": "category",
        "status": "category",
        "captured": "date",
        "size": "integer",
    }
    assert fields["details"].schema["type"] == "object"
    assert fields["items"].schema["type"] == "array"
    assert fields["mapping"].schema["type"] == "object"
    assert fields["assessed"].schema["type"] == "array"
    assert fields["size"].format == "filesize"
    assert terminal.resolve_output_fields(Params()) == terminal.output_fields
    assert not has_dynamic_outputs(terminal)


class NestedHandle(BaseModel):
    file: StagedFile


@pytest.mark.parametrize(
    ("annotation", "wrap", "column_type"),
    [
        (StagedFile, lambda value: value, "file"),
        (list[StagedFile], lambda value: [value], "json"),
        (dict[str, StagedFile], lambda value: {"attachment": value}, "json"),
        (NestedHandle, lambda value: {"file": value}, "json"),
        (list[NestedHandle], lambda value: [{"file": value}], "json"),
    ],
)
def test_declared_row_file_shapes_keep_opaque_leaves(annotation, wrap, column_type):
    from pydantic import create_model

    output = create_model("FileShapeOutput", value=(annotation, ...))

    def produce(params, row):
        raise AssertionError

    produce.__annotations__ = {
        "params": Params,
        "row": Row,
        "return": RowResult[output],
    }
    terminal = map_rows(produce)
    assert [(field.key, field.column_type) for field in terminal.output_fields] == [
        ("value", column_type)
    ]
    output.model_validate({"value": wrap(StagedFile(10))})
    # Static shape support never reconstructs opaque authority from cell JSON.
    envelope = {"blob": "a" * 64, "mime": "text/plain", "filename": "a.txt"}
    with pytest.raises(ValidationError):
        output.model_validate({"value": wrap(envelope)})


@pytest.mark.parametrize("column_format", ["", " filesize", 3])
def test_static_output_format_must_be_valid_metadata(column_format):
    from pydantic import create_model

    output = create_model(
        "BadFormat", value=(int, Field(json_schema_extra={"format": column_format}))
    )

    def invalid(params, row):
        raise AssertionError

    invalid.__annotations__ = {
        "params": Params,
        "row": Row,
        "return": RowResult[output],
    }
    with pytest.raises(TypeError, match="column format"):
        map_rows(invalid)


async def describe_media(
    params: Params, row: Row, metadata: MediaMetadataReader
) -> RowResult[ConditionalOutput]:
    raise AssertionError("catalog and binding must not probe media")


class RenamedParams(ActionParams):
    force_probe: bool = False


async def describe_other_media(
    settings: RenamedParams, record: Row, reader: MediaMetadataReader
) -> RowResult[StructuredOutput]:
    raise AssertionError("catalog and binding must not probe media")


def test_map_rows_derives_reusable_capability_and_honest_local_catalog():
    first = action(
        name="first",
        title="First",
        description="Read metadata",
        category=ActionCategory.CONVERT,
        run=map_rows(describe_media, active_outputs=lambda params: ("details",)),
    )
    second = action(
        name="second",
        title="Second",
        description="Read metadata differently",
        category=ActionCategory.CONVERT,
        run=map_rows(describe_other_media),
    )
    registry = ActionRegistry([ActionNamespace("test", actions=[first, second])])
    for registered in registry.actions:
        assert registered.definition.run.capabilities == (MediaMetadataReader,)
        catalog = registered.catalog_entry()
        assert catalog["required_capabilities"] == ["project:write", "project:read"]
        assert set(catalog["side_effects"]) >= {
            "read_media_blobs",
            "call_local_metadata_adapters",
            "write_media_metadata_cache",
        }
        assert "call_external_provider" not in catalog["side_effects"]
        assert catalog["cost_policy"]["kind"] == "none"
        assert catalog["cost_policy"]["requires_confirmation"] is False
        registered.bind_values(scope=SheetRows(sheet_id=1), params={}, output_names={})
    assert first.run.params_model is Params
    assert second.run.params_model is RenamedParams
    assert map_rows(structured).capabilities == ()
    with pytest.raises(FrozenInstanceError):
        first.run.capabilities = ()


@pytest.mark.parametrize(
    "signature",
    ["unknown", "duplicate", "varargs", "keyword_only", "optional", "unannotated"],
)
def test_map_rows_rejects_unknown_or_ambiguous_injection(signature):
    def unknown(
        params: Params, row: Row, metadata: object
    ) -> RowResult[StructuredOutput]: ...

    def duplicate(
        params: Params,
        row: Row,
        first: MediaMetadataReader,
        second: MediaMetadataReader,
    ) -> RowResult[StructuredOutput]: ...

    def varargs(
        params: Params, row: Row, *metadata: MediaMetadataReader
    ) -> RowResult[StructuredOutput]: ...

    def keyword_only(
        params: Params, row: Row, *, metadata: MediaMetadataReader
    ) -> RowResult[StructuredOutput]: ...

    def optional(
        params: Params, row: Row, metadata: MediaMetadataReader | None
    ) -> RowResult[StructuredOutput]: ...

    def unannotated(
        params: Params, row: Row, metadata
    ) -> RowResult[StructuredOutput]: ...

    handlers = {
        "unknown": unknown,
        "duplicate": duplicate,
        "varargs": varargs,
        "keyword_only": keyword_only,
        "optional": optional,
        "unannotated": unannotated,
    }
    with pytest.raises(TypeError, match="map_rows"):
        map_rows(handlers[signature])

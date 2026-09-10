from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field, create_model

from frisket.actions import core
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    GeneratedColumnRef,
    ModelPrompt,
    ModelRef,
    Row,
    Template,
    source_kind,
)


@pytest.mark.parametrize(
    ("annotation", "kind", "control"),
    [
        (ColumnRef[str], "column", "column"),
        (GeneratedColumnRef[str], "column", "column"),
        (Template[str], "template", "template"),
        (list[ColumnRef[Any]], "columns", "columns"),
        (tuple[ColumnRef[str]], "columns", "columns"),
        (ColumnRef[str] | Template[str], "column_or_template", "column_or_template"),
        (list[ColumnRef[Any]] | Template[str], "columns_or_template", "rich_source"),
        (Annotated[list[ColumnRef[str]], Field(min_length=1)], "columns", "columns"),
        (Annotated[ColumnRef[str] | None, "metadata"], "column", "column"),
        (Annotated[ColumnRef[str], "metadata"] | None, "column", "column"),
        (Template[str] | None, "template", "template"),
        (ColumnRef[str] | str, None, None),
        (ColumnRef[str] | Template[str] | None, None, None),
        (list[Annotated[ColumnRef[str], "metadata"]], None, None),
        (tuple[ColumnRef[str], ...], None, None),
        (list[str], None, None),
        (str, None, None),
    ],
)
def test_semantic_source_shapes_and_presentation(annotation, kind, control):
    assert source_kind(annotation) == kind
    assert core._semantic_control(annotation) == control


class _Reply(BaseModel):
    answer: str


def _handler(source_annotation, generated_annotation=GeneratedColumnRef[str]):
    params_type = create_model(
        "PromptParams",
        __base__=ActionParams,
        source=(source_annotation, ...),
        model=(ModelRef, ...),
        generated=(generated_annotation, ...),
    )

    def render(params: params_type, row: Row) -> ModelPrompt[_Reply]:
        return ModelPrompt(messages=({"role": "user", "content": "test"},))

    return render


@pytest.mark.parametrize(
    "annotation",
    [
        ColumnRef[str],
        ColumnRef[str] | None,
        Annotated[ColumnRef[str], Field(description="Source")],
        ColumnRef[str] | Template[str],
        list[ColumnRef[Any]] | Template[str],
    ],
)
def test_model_source_discovery_does_not_consult_presentation(monkeypatch, annotation):
    def refuse_presentation(_annotation):
        pytest.fail("ModelRows source discovery consulted presentation")

    monkeypatch.setattr(core, "_semantic_control", refuse_presentation)
    handler = _handler(annotation)
    assert core.model_rows(handler).source_param == "source"
    assert core.model_rows(handler, source_param="source").source_param == "source"
    with pytest.raises(TypeError, match="must name a typed source field"):
        core.model_rows(handler, source_param="generated")


def test_optional_generated_reference_keeps_existing_outer_annotation_boundary():
    handler = _handler(ColumnRef[str], GeneratedColumnRef[str] | None)
    with pytest.raises(TypeError, match="exactly one typed source"):
        core.model_rows(handler)
    assert core.model_rows(handler, source_param="source").source_param == "source"
    assert (
        core.model_rows(handler, source_param="generated").source_param == "generated"
    )

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from frisket.actions.model_rows import RichSource
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    Row,
    Template,
    discover_references,
)


@pytest.mark.parametrize("reverse", [False, True])
def test_source_union_roundtrips_without_guessing_from_text(reverse):
    source_type = (
        Template[str] | ColumnRef[str] if reverse else ColumnRef[str] | Template[str]
    )

    class Params(ActionParams):
        source: source_type

    for text in ("address", "literal address", "{{street}}"):
        for source in (ColumnRef[str](text), Template[str](text=text)):
            params = Params(source=source)
            wire = params.model_dump(mode="json")
            assert wire == {
                "source": text if isinstance(source, ColumnRef) else {"text": text}
            }
            for restored in (
                Params.model_validate(wire),
                Params.model_validate_json(params.model_dump_json()),
            ):
                assert type(restored.source) is type(source)
                assert restored.source == source


def test_template_nested_optional_default_and_plural_sources_roundtrip():
    class Nested(BaseModel):
        source: ColumnRef[str] | Template[str]

    class Params(ActionParams):
        nested: Nested
        sources: list[ColumnRef[str] | Template[str] | None]
        rich: RichSource
        default: Template[str] = Template[str](text="fallback")

    params = Params(
        nested={"source": {"text": "literal"}},
        sources=["first", {"text": "{{second}}"}, None],
        rich=["first", "second"],
    )
    restored = Params.model_validate_json(params.model_dump_json())
    assert isinstance(restored.nested.source, Template)
    assert isinstance(restored.sources[0], ColumnRef)
    assert isinstance(restored.sources[1], Template)
    assert restored.sources[2] is None
    assert [ref.name for ref in restored.rich] == ["first", "second"]
    assert restored.default.text == "fallback"
    rich_template = params.model_copy(update={"rich": Template[Any](text="{{first}}")})
    assert isinstance(
        Params.model_validate_json(rich_template.model_dump_json()).rich, Template
    )


@pytest.mark.parametrize(
    "value",
    ["bare string", {}, {"text": " "}, {"text": 3}, {"text": "x", "kind": "template"}],
)
def test_template_refuses_old_strings_and_invalid_objects(value):
    with pytest.raises(ValidationError):
        Template[str].model_validate(value)


def test_template_is_frozen_and_preserves_rendering_and_reference_order():
    template = Template[str](text="  {{city}}, {{street}}, {{city}}  ")
    assert template.text == "{{city}}, {{street}}, {{city}}"
    assert template.render(Row({"city": "London", "street": "Baker St"})) == (
        "London, Baker St, London"
    )
    assert [ref.column for ref in discover_references(template)] == ["city", "street"]
    assert Template[str](text="literal").render(Row({})) == "literal"
    assert Template[str](text="literal").references() == ()
    with pytest.raises(ValidationError, match="frozen"):
        template.text = "changed"


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_template_schema_is_the_same_closed_object_in_both_directions(mode):
    schema = Template[str].model_json_schema(mode=mode)
    assert schema["type"] == "object"
    assert schema["required"] == ["text"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"text"}
    assert schema["properties"]["text"]["type"] == "string"

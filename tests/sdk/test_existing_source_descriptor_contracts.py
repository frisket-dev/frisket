from __future__ import annotations

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import discover_references


def test_model_actions_discover_all_direct_and_template_sources() -> None:
    for action_id in ("map.classify", "map.extract", "map.mcp_extract", "map.ner"):
        declaration = ACTION_REGISTRY.get(action_id).definition
        model = declaration.run.params_model
        options = {
            "map.classify": {
                "engine": "llm",
                "fields": [{"name": "value", "type": "text"}],
            },
            "map.extract": {"fields": [{"name": "value", "type": "text"}]},
            "map.mcp_extract": {
                "fields": [{"name": "value", "type": "text"}],
                "mcp_server_ids": ["crm"],
            },
            "map.ner": {"engine": "llm", "labels": ["person"]},
        }
        example = model.model_validate(
            {
                "source": ["source"],
                "model": "anthropic/claude-haiku-4-5",
                **options[action_id],
            }
        )
        direct = model.model_validate(
            {**example.model_dump(mode="json"), "source": ["direct", "templated"]}
        )
        template = model.model_validate(
            {
                **example.model_dump(mode="json"),
                "source": {"text": "{{templated}} / {{direct}}"},
            }
        )
        assert {ref.column for ref in discover_references(direct)} == {
            "direct",
            "templated",
        }
        assert {ref.column for ref in discover_references(template)} == {
            "direct",
            "templated",
        }

    from frisket.actions.model_rows import RichColumn
    from frisket.actions.ner import NerColumn

    assert RichColumn.accepted_column_types == (
        "text",
        "timestamped_transcript",
        "category",
        "date",
        "number",
        "integer",
        "boolean",
        "json",
        "image",
    )
    assert NerColumn.accepted_column_types == ("text", "timestamped_transcript")

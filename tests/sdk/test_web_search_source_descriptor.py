from __future__ import annotations

from frisket.actions.core import MapRows
from frisket.actions.research_types import SearchAnswerer
from frisket.actions.registry import ACTION_REGISTRY, COPILOT_ACTION_IDS
from frisket.actions.system import root_action_catalog
from frisket.actions.types import discover_references


def test_web_search_uses_typed_params_and_template_source_discovery() -> None:
    registered = ACTION_REGISTRY.get("research.web_search")
    params = registered.definition.run.params_model.model_validate(
        {
            "query": {"text": "{{ city }} {{topic}} {{city}}"},
            "model": "anthropic/claude-haiku-4-5",
            "max_results": 8,
        }
    )

    assert [ref.column for ref in discover_references(params)] == ["city", "topic"]
    assert params.model_dump(mode="json") == {
        "query": {"text": "{{ city }} {{topic}} {{city}}"},
        "max_results": 8,
        "model": "anthropic/claude-haiku-4-5",
    }


def test_web_search_catalog_is_generated_and_available_to_copilot() -> None:
    catalog = next(
        entry
        for entry in root_action_catalog().actions
        if entry.kind == "research.web_search"
    )

    assert catalog.ui_hints["form"] == "generated"
    assert catalog.ui_hints["source_requirements"] == [
        {
            "id": "query",
            "param": "query",
            "label": "Query",
            "mode": "template",
            "min": 0,
            "template_columns": "union",
        }
    ]
    assert catalog.ui_hints["logical_outputs"] == [
        {"key": "answer", "column_type": "text"},
        {"key": "search_results", "column_type": "json"},
    ]
    assert catalog.required_capabilities == [
        "project:write",
        "model:complete",
        "external:web_search",
    ]
    assert catalog.cost_policy.kind == "unknown"
    assert catalog.cost_policy.requires_confirmation is True
    assert catalog.async_mode == "queued"
    assert "research.web_search" in COPILOT_ACTION_IDS


def test_web_search_requests_the_host_capability_without_provider_metadata() -> None:
    terminal = ACTION_REGISTRY.get("research.web_search").definition.run
    assert type(terminal) is MapRows
    assert terminal.capabilities == (SearchAnswerer,)

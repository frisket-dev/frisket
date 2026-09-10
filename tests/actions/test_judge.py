from __future__ import annotations

import json

import pytest

from frisket.actions.core import ModelRows, ModelRowsEvaluationContext
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest, Row, discover_references


def _request(*, output_names: dict[str, str] | None = None) -> ActionRequest:
    return ActionRequest.model_validate(
        {
            "action_id": "map.judge",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {
                "source": ["story"],
                "judged_column": "answer",
                "model": "openai/gpt-5-mini",
                "guidelines": "Every claim must be supported.",
                "include_original_prompt": True,
            },
            "output_names": output_names or {},
            "idempotency_key": "judge-test",
        }
    )


def test_judge_is_a_closed_model_rows_evaluation_profile() -> None:
    registered = ACTION_REGISTRY.get("map.judge")
    terminal = registered.definition.run
    assert isinstance(terminal, ModelRows)
    assert terminal.evaluation is not None
    assert tuple(terminal.output_model.model_fields) == ("verdict", "judge_note")

    bound, _ = registered.bind_request(_request())
    references = {item.column: item for item in discover_references(bound)}
    assert references["story"].ai_generated_only is False
    assert references["answer"].ai_generated_only is True

    catalog = registered.catalog_entry()
    requirement = next(
        item
        for item in catalog["ui_hints"]["source_requirements"]
        if item["param"] == "judged_column"
    )
    assert requirement["ai_generated_only"] is True


def test_judge_renderer_uses_only_frozen_host_context() -> None:
    registered = ACTION_REGISTRY.get("map.judge")
    terminal = registered.definition.run
    params, _ = registered.bind_request(_request())
    prompt = terminal.renderer(
        params,
        Row({"story": "A source.", "answer": "A claim."}),
        ModelRowsEvaluationContext(
            subject_column="answer",
            subject_column_id=2,
            source_run_id=7,
            original_prompt='{"question": "What happened?"}',
        ),
    )
    rendered = json.dumps(prompt.messages)
    assert "The column under review is 'answer'" in rendered
    assert "<original_prompt>" in rendered
    assert "What happened?" in rendered


def test_judge_output_names_cannot_overlap_inputs() -> None:
    registered = ACTION_REGISTRY.get("map.judge")
    with pytest.raises(ValueError, match="output names cannot overlap input columns"):
        registered.bind_request(_request(output_names={"verdict": "story"}))

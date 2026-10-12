from __future__ import annotations

from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolve_for_action import resolve_for_action
from tests.typed_model_fixtures import model_plan


@pytest.mark.parametrize(
    ("keys", "cache_mode"),
    [
        ({"openai": "own-openai-key"}, "off"),
        ({}, "replay"),
    ],
)
def test_own_llm_admission_does_not_render_rows(
    tmp_path: Any,
    keys: dict[str, str],
    cache_mode: str,
) -> None:
    project = Project.create(tmp_path / "project.frisket", name="Render cost")
    try:
        sheet_id = project.add_sheet("Data")
        source_id = project.add_column(sheet_id, "text", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"text": "first"}, {"text": "second"}],
            {"text": source_id},
        )
        legacy_spec = {
            "action_kind": "map.classify",
            "model": "openai/gpt-5-mini",
            "sheet_id": sheet_id,
            "input_columns": ["text"],
            "context": "render counter",
            "fields": [
                {
                    "name": "relevance",
                    "type": "score",
                    "description": "0-10 relevance",
                }
            ],
        }
        plan = model_plan(legacy_spec)
        spec = {**legacy_spec, **plan.spec_dict()}
        recipe = plan.program
        original_render = recipe.render
        renders = 0

        def counted_render(values: dict[str, Any], bound_spec: dict[str, Any]) -> Any:
            nonlocal renders
            renders += 1
            return original_render(values, bound_spec)

        recipe.render = counted_render
        router = ModelRouter(
            keys=keys,
            use_env_keys=False,
            cache_mode=cache_mode,
        )
        composition = open_execution_composition(
            project,
            router,
            ExecutionCompositionContext.direct(),
        )

        outcome = resolve_for_action(
            project,
            spec,
            recipe,
            composition=composition,
            scope_row_ids=row_ids,
        )

        assert outcome is None
        assert renders == 0
    finally:
        project.close()

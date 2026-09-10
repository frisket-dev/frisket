from __future__ import annotations

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.translate import TranslateParams, translate_prompt
from frisket.actions.translate_types import translation_text
from frisket.actions.types import ActionRequest, Row
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner.row_inputs import row_values
from frisket.engine.store import Project


def _bound(sheet_id, source):
    return BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("map.translate"),
        ActionRequest.model_validate(
            {
                "action_id": "map.translate",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": source, "model": "anthropic/claude-haiku-4-5"},
                "output_names": {"translation": "translated"},
                "idempotency_key": "translation-source@1",
            }
        ),
    )


def test_translate_params_accept_a_template_without_duplicate_input_columns():
    params = TranslateParams(
        source={"text": "{{speaker}}: {{ statement }}"}, model="anthropic/example"
    )
    assert params.source.text == "{{speaker}}: {{ statement }}"


def test_translate_source_rejects_duplicate_or_ambiguous_selection():
    for source in (["text", "text"], [], {"text": "literal without references"}):
        with pytest.raises(ValueError):
            TranslateParams(source=source, model="anthropic/example")


def test_translate_keeps_llm_multimodal_sources_resolvable(tmp_path):
    project = Project.create(tmp_path / "translate-image.frisket", name="translate")
    try:
        sheet_id = project.add_sheet("Images")
        image_id = project.add_column(sheet_id, "scan", type="image")
        plan = build_typed_map_rows_plan(project, _bound(sheet_id, ["scan"]))
        assert plan.source_column_ids == {"scan": image_id}
    finally:
        project.close()


def test_translate_template_preserves_order_and_composes_both_engine_inputs(tmp_path):
    project = Project.create(tmp_path / "translate-template.frisket", name="translate")
    try:
        sheet_id = project.add_sheet("Statements")
        columns = {
            name: project.add_column(sheet_id, name, type="text")
            for name in ("statement", "speaker")
        }
        [row_id] = project.add_rows(
            sheet_id, [{"statement": "Hola mundo", "speaker": "Ada"}], columns
        )
        bound = _bound(
            sheet_id, {"text": "{{speaker}} says: {{statement}} / {{speaker}}"}
        )
        plan = build_typed_map_rows_plan(project, bound)
        assert plan.source_columns == ("speaker", "statement")
        values = row_values(
            project, plan.program, plan.spec_dict(), columns, row_id, for_model=False
        )
        assert values == {"input": "Ada says: Hola mundo / Ada"}
        prompt = translate_prompt(bound.params, Row(values))
        assert "Ada says: Hola mundo / Ada" in str(prompt.messages)
        assert translation_text(values) == "Ada says: Hola mundo / Ada"
    finally:
        project.close()

from __future__ import annotations

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.translate import TranslateParams, translation_outputs
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import typed_request_hash


def _bound(**overrides):
    params = {
        "source": ["statement"],
        "model": "anthropic/claude-haiku-4-5",
        "target_language": "Spanish",
    }
    params.update(overrides)
    return BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("map.translate"),
        ActionRequest.model_validate(
            {
                "action_id": "map.translate",
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": params,
                "output_names": {
                    "translation": "statement_spanish",
                    **(
                        {"detected_language": "language"}
                        if params.get("save_detected_language")
                        else {}
                    ),
                },
                "idempotency_key": "translate@1",
            }
        ),
    )


def test_save_detected_language_default_and_explicit_false_have_same_identity():
    omitted = _bound()
    explicit = _bound(save_detected_language=False)
    assert typed_request_hash(omitted) == typed_request_hash(explicit)
    enabled = _bound(save_detected_language=True)
    assert typed_request_hash(enabled) != typed_request_hash(omitted)
    assert [field.key for field in enabled.output_fields] == [
        "translation",
        "detected_language",
    ]


def test_translate_contract_has_one_user_selected_primary_output():
    entry = ACTION_REGISTRY.get("map.translate").catalog_entry()
    properties = entry["input_schema"]["properties"]
    assert "source" in properties
    assert "output_name" not in properties
    bound = _bound()
    assert bound.request.output_names == {"translation": "statement_spanish"}
    assert [field.key for field in bound.output_fields] == ["translation"]


def test_detected_language_is_opt_in_and_off_by_default():
    params = TranslateParams(source=["statement"], model="anthropic/claude-haiku-4-5")
    assert params.save_detected_language is False
    assert translation_outputs(params) == ("translation",)
    params.save_detected_language = True
    assert translation_outputs(params) == ("translation", "detected_language")

from __future__ import annotations

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import typed_request_hash


def test_typed_params_hash_excludes_confirmation_but_retains_authored_intent() -> None:
    action = {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": ["text"],
            "engine": "llm",
            "model": "openai/gpt-5-mini",
            "fields": [{"name": "label", "type": "category", "labels": ["yes", "no"]}],
        },
        "idempotency_key": "idem_hash",
    }
    confirmed = {**action, "confirmation": "exact-quote-token"}
    changed = {**confirmed, "params": {**action["params"], "context": "Changed intent"}}
    assert typed_request_hash(typed_action_for_request(action)) == typed_request_hash(
        typed_action_for_request(confirmed)
    )
    assert typed_request_hash(typed_action_for_request(changed)) != typed_request_hash(
        typed_action_for_request(confirmed)
    )

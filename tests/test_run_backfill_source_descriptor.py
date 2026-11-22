from __future__ import annotations

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.run_backfill_action import (
    BackfillRefused,
    prepare_backfill_action,
)


def test_backfill_semantic_column_uses_host_generated_column_policy(make_project):
    entry = ACTION_REGISTRY.get("run.backfill").catalog_entry()
    assert entry["ui_hints"]["semantic_controls"] == {"column": "column"}
    project = make_project("backfill")
    sheet_id = project.add_sheet("Data")
    project.add_column(sheet_id, "requested", type="text")
    bound = typed_action_for_request(
        {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "requested"},
            "idempotency_key": "admission",
        }
    )
    with pytest.raises(BackfillRefused) as caught:
        prepare_backfill_action(project, bound)
    assert caught.value.error.code == "column_not_generated"
    assert "requested" in caught.value.error.message

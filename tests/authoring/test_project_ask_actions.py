from __future__ import annotations

import pytest

from frisket.actions.system import root_action_catalog_payload
from frisket.authoring.project_ask_actions import (
    prepare_validated_project_ask_draft,
    project_ask_action_catalog,
)
from frisket.engine.store import Project


def test_prepared_project_ask_draft_is_keyless_and_bound_to_the_catalog(tmp_path):
    project = Project.create(tmp_path / "ask-catalog.frisket")
    try:
        sheet_id = project.add_sheet("People")
        project.add_column(sheet_id, "Name")
        prepared = prepare_validated_project_ask_draft(
            project,
            {
                "action_id": "map.template",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"template": {"text": "Hello {{Name}}"}},
                "output_names": {},
            },
            catalog_payload=root_action_catalog_payload(),
        )

        assert prepared.draft["action_id"] == "map.template"
        assert "idempotency_key" not in prepared.draft
        assert prepared.request.idempotency_key == "project-ask-draft"
        assert prepared.catalog_entry["kind"] == "map.template"
        assert [reference.column for reference in prepared.references] == ["Name"]
        assert prepared.implementation_identity is None
    finally:
        project.close()


def test_prepared_project_ask_draft_requires_current_catalog_membership(tmp_path):
    project = Project.create(tmp_path / "ask-catalog.frisket")
    try:
        with pytest.raises(ValueError, match="not available"):
            prepare_validated_project_ask_draft(
                project,
                {
                    "action_id": "example.plugin.action",
                    "scope": {"kind": "project"},
                    "params": {},
                    "output_names": {},
                },
                catalog_payload={"actions": []},
            )
        assert "derive.join" in {
            entry["kind"]
            for entry in project_ask_action_catalog(root_action_catalog_payload())
        }
    finally:
        project.close()

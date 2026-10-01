from __future__ import annotations

import pytest

from frisket.actions.system import root_action_catalog_payload
from frisket.authoring.project_ask_actions import (
    describe_project_ask_action,
    prepare_validated_project_ask_draft,
    project_ask_action_catalog,
    search_project_ask_actions,
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


def test_ask_refuses_generated_code_catalog_entries(tmp_path):
    project = Project.create(tmp_path / "ask-catalog.frisket")
    try:
        sheet_id = project.add_sheet("People")
        project.add_column(sheet_id, "Name")
        catalog = root_action_catalog_payload()
        template = next(
            entry for entry in catalog["actions"] if entry["kind"] == "map.template"
        )
        unsafe_catalog = {
            "actions": [
                {
                    **template,
                    "required_capabilities": ["unsafe:local_code"],
                    "side_effects": ["execute_trusted_local_python"],
                }
            ]
        }
        assert project_ask_action_catalog(unsafe_catalog) == ()
        assert describe_project_ask_action(unsafe_catalog, "map.template") is None
        assert search_project_ask_actions(unsafe_catalog, "template", 5) == ([], False)
        with pytest.raises(ValueError, match="not available"):
            prepare_validated_project_ask_draft(
                project,
                {
                    "action_id": "map.template",
                    "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                    "params": {"template": {"text": "Hello {{Name}}"}},
                    "output_names": {},
                },
                catalog_payload=unsafe_catalog,
            )
    finally:
        project.close()


def test_ask_scope_rejects_a_secondary_sheet_column_outside_its_sources(tmp_path):
    project = Project.create(tmp_path / "ask-catalog.frisket")
    try:
        source_sheet = project.add_sheet("Source")
        project.add_column(source_sheet, "Company")
        target_sheet = project.add_sheet("Target")
        project.add_column(target_sheet, "Company")
        with pytest.raises(ValueError, match="outside the Ask source scope"):
            prepare_validated_project_ask_draft(
                project,
                {
                    "action_id": "join.semantic",
                    "scope": {"kind": "sheet_rows", "sheet_id": source_sheet},
                    "params": {
                        "source": "Company",
                        "target": {"sheet_id": target_sheet, "column": "Company"},
                    },
                    "output_names": {},
                    "sheet_name": "Matches",
                },
                catalog_payload=root_action_catalog_payload(),
                scope={
                    "kind": "sources",
                    "sources": [{"kind": "sheet", "sheet_id": source_sheet}],
                },
            )
    finally:
        project.close()


def test_ask_validates_the_secondary_sheet_column_exists(tmp_path):
    project = Project.create(tmp_path / "ask-catalog.frisket")
    try:
        source_sheet = project.add_sheet("Source")
        project.add_column(source_sheet, "Company")
        target_sheet = project.add_sheet("Target")
        with pytest.raises(ValueError, match="unknown_input_columns"):
            prepare_validated_project_ask_draft(
                project,
                {
                    "action_id": "join.semantic",
                    "scope": {"kind": "sheet_rows", "sheet_id": source_sheet},
                    "params": {
                        "source": "Company",
                        "target": {"sheet_id": target_sheet, "column": "Missing"},
                    },
                    "output_names": {},
                    "sheet_name": "Matches",
                },
                catalog_payload=root_action_catalog_payload(),
                scope={"kind": "project"},
            )
    finally:
        project.close()

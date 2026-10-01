from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.actions.system import root_action_catalog_payload
from frisket.authoring.project_ask_actions import (
    OwnedOutputInput,
    describe_project_ask_action,
    prepare_validated_project_ask_draft,
    project_ask_action_catalog,
    search_project_ask_actions,
)
from frisket.engine.store import Project


@dataclass(frozen=True)
class _OutputGrant:
    receipt_id: str
    sheet_id: int
    column_ids: frozenset[int] | None
    row_ids: frozenset[int] | None


def _allows_output_cell(
    grants: tuple[_OutputGrant, ...], *, sheet_id: int, row_id: int, column_id: int
) -> bool:
    return any(
        grant.sheet_id == sheet_id
        and (grant.row_ids is None or row_id in grant.row_ids)
        and (grant.column_ids is None or column_id in grant.column_ids)
        for grant in grants
    )


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


def test_file_scoped_ask_may_use_only_its_receipt_proven_derived_column(tmp_path):
    project = Project.create(tmp_path / "ask-catalog.frisket")
    try:
        sheet_id = project.add_sheet("Files")
        file_column = project.add_column(sheet_id, "File")
        markdown_column = project.add_column(sheet_id, "Markdown")
        sibling_column = project.add_column(sheet_id, "Private notes")
        [file_row, sibling_row] = project.add_rows(
            sheet_id,
            [
                {"File": "report.pdf", "Markdown": "derived", "Private notes": "a"},
                {"File": "other.pdf", "Markdown": "other", "Private notes": "b"},
            ],
            {
                "File": file_column,
                "Markdown": markdown_column,
                "Private notes": sibling_column,
            },
        )
        draft = {
            "action_id": "map.template",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"template": {"text": "{{Markdown}}"}},
            "output_names": {},
        }
        scope = {
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet_id,
                    "row_id": file_row,
                    "column_id": file_column,
                }
            ],
        }
        grant = _OutputGrant(
            receipt_id="receipt-markdown",
            sheet_id=sheet_id,
            column_ids=frozenset({markdown_column}),
            row_ids=frozenset({file_row}),
        )

        with pytest.raises(ValueError, match="outside the Ask source scope"):
            prepare_validated_project_ask_draft(
                project,
                draft,
                catalog_payload=root_action_catalog_payload(),
                scope=scope,
            )
        prepared = prepare_validated_project_ask_draft(
            project,
            draft,
            catalog_payload=root_action_catalog_payload(),
            scope=scope,
            output_grants=(grant,),
            output_grant_allows=_allows_output_cell,
        )
        assert prepared.draft["scope"]["row_ids"] == [file_row]
        assert prepared.owned_output_inputs == (
            OwnedOutputInput("receipt-markdown", sheet_id, file_row, markdown_column),
        )

        sibling_draft = {
            **draft,
            "params": {"template": {"text": "{{Private notes}}"}},
        }
        with pytest.raises(ValueError, match="outside the Ask source scope"):
            prepare_validated_project_ask_draft(
                project,
                sibling_draft,
                catalog_payload=root_action_catalog_payload(),
                scope=scope,
                output_grants=(grant,),
                output_grant_allows=_allows_output_cell,
            )
        with pytest.raises(ValueError, match="outside the Ask source scope"):
            prepare_validated_project_ask_draft(
                project,
                {
                    **draft,
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [sibling_row],
                    },
                },
                catalog_payload=root_action_catalog_payload(),
                scope=scope,
                output_grants=(grant,),
                output_grant_allows=_allows_output_cell,
            )
    finally:
        project.close()

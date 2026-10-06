"""Draft configuration is durable; remembered documents are current selections."""

from contextlib import closing
import sqlite3

import pytest

from frisket.actions.document_extraction_types import (
    ExtractionScope,
    ExtractionTemplate,
)
from frisket.contracts.http.document_extraction import (
    ExtractionLayoutDraft,
    ExtractionLayoutSelection,
    ExtractionScopeCountsRequest,
    ExtractionTemplateSave,
)
from frisket.engine.store import Project
from frisket.engine.store import extraction_layouts as layouts
from frisket.engine.store.extraction_layouts_migration import (
    EXTRACTION_LAYOUTS_FROM_DIGEST,
    EXTRACTION_LAYOUTS_TO_DIGEST,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.server.services.document_extraction import DocumentExtractionService
from frisket.server.workspace import Workspace
from tests.engine.test_visual_document_extraction_action import seed


def save(project, sheet, template, **kwargs):
    return layouts.save_layout(
        project,
        sheet_id=sheet,
        source="document",
        draft=template.model_dump(),
        **kwargs,
    )


def resolve(project, sheet, **scope):
    return layouts.resolve_document_scope(
        project, sheet_id=sheet, source="document", scope=ExtractionScope(**scope)
    )


def test_incomplete_drafts_and_selected_layout_survive_reload(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Layouts", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, template = seed(project)
    service = DocumentExtractionService(workspace)
    pending = {"tool": "key", "region": template.fields[0].key.model_dump()}
    body = ExtractionTemplateSave(
        sheet_id=sheet,
        source="document",
        reference_row_id=rows[0],
        draft=ExtractionLayoutDraft(pending=pending),
    )
    first = service.save("p", body)
    second = service.save("p", body)
    assert (first.name, second.name) == ("Layout 1", "Layout 2")
    assert first.draft.pending.region == template.fields[0].key
    assert first.draft.fields == []
    with pytest.raises(ValueError):
        ExtractionTemplate.model_validate(first.draft.model_dump(exclude={"pending"}))
    incomplete = template.model_dump()
    incomplete["fields"][0]["name"] = ""
    renamed = service.save(
        "p",
        body.model_copy(
            update={
                "id": first.id,
                "draft": ExtractionLayoutDraft(**incomplete, pending=pending),
            }
        ),
    )
    assert renamed.draft.fields[0].name == ""
    with pytest.raises(ValueError):
        ExtractionTemplate.model_validate(renamed.draft.model_dump(exclude={"pending"}))
    assert layouts.selected_layout_id(project, sheet, column) == second.id
    service.select(
        "p",
        ExtractionLayoutSelection(
            sheet_id=sheet, source="document", layout_id=first.id
        ),
    )
    with closing(Project(project.path)) as reloaded:
        assert layouts.selected_layout_id(reloaded, sheet, column) == first.id
        assert len(layouts.list_layouts(reloaded, sheet, column)) == 2
        assert layouts.get_layout(reloaded, first.id)["draft"]["pending"] == pending
        assert (
            reloaded.db.execute(
                "SELECT COUNT(*) FROM extraction_layout_documents"
            ).fetchone()[0]
            == 0
        )


def test_successful_document_assignments_transfer_without_losing_other_rows(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, count=3)
        first = save(project, sheet, template)
        second = save(project, sheet, template)
        layouts.remember_success(project, first["id"], rows, commit=True)
        layouts.remember_success(project, second["id"], [rows[1]], commit=True)
        assert resolve(project, sheet, kind="layout", layout_id=first["id"]) == [
            rows[0],
            rows[2],
        ]
        assert resolve(project, sheet, kind="layout", layout_id=second["id"]) == [
            rows[1]
        ]
        layouts.remember_success(project, first["id"], [rows[0]], commit=True)
        assert resolve(project, sheet, kind="layout", layout_id=first["id"]) == [
            rows[0],
            rows[2],
        ]
        layouts.remember_success(project, second["id"], rows, commit=True)
        assert resolve(project, sheet, kind="layout", layout_id=first["id"]) == []
        assert layouts.get_layout(project, first["id"])["has_applied"] is True
        # Successful zero-record inputs are remembered by source identity;
        # no output sheet/record is required to express their assignment.
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1


def test_membership_is_part_of_callers_publication_transaction(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        first = save(project, sheet, template)
        project.db.execute("BEGIN IMMEDIATE")
        layouts.remember_success(project, first["id"], [rows[0]])
        project.db.rollback()
        assert layouts.get_layout(project, first["id"])["has_applied"] is False
        assert resolve(project, sheet, kind="layout", layout_id=first["id"]) == []
        with pytest.raises(ValueError):
            layouts.remember_success(project, first["id"], [99999])


def test_scopes_count_live_documents_and_obey_sheet_filter(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Scopes", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, template = seed(project, count=3)
    label = project.add_column(sheet, "format", "text")
    project.apply_edits(
        [
            {"row_id": row_id, "column_id": label, "value": value}
            for row_id, value in zip(rows, ["A", "B", "A"], strict=True)
        ]
    )
    project.add_rows(sheet, [{"document": None}], {"document": column})
    first = save(project, sheet, template)
    layouts.remember_success(project, first["id"], [rows[1]], commit=True)
    filter_ = {"format": {"eq": "A"}}
    assert resolve(project, sheet, kind="filter", filter=filter_) == [rows[0], rows[2]]
    assert resolve(
        project, sheet, kind="filter", filter=filter_, scope_row_ids=[rows[2]]
    ) == [rows[2]]
    assert resolve(project, sheet, kind="filter", scope_row_ids=[]) == []
    assert resolve(project, sheet, kind="this", row_id=rows[1]) == [rows[1]]
    counts = DocumentExtractionService(workspace).counts(
        "p",
        ExtractionScopeCountsRequest(
            sheet_id=sheet,
            source="document",
            layout_id=first["id"],
            row_id=rows[1],
            filter=filter_,
        ),
    )
    assert counts.model_dump() == {"all": 3, "filter": 2, "layout": 1, "this": 1}
    project.apply_edits([{"row_id": rows[0], "column_id": label, "value": "B"}])
    assert resolve(project, sheet, kind="filter", filter=filter_) == [rows[2]]


def test_layouts_are_scoped_by_column_identity(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        other = project.add_column(sheet, "other", "file")
        first = save(project, sheet, template)
        with pytest.raises(ValueError):
            layouts.validate_layout_scope(project, first["id"], sheet, "other")
        project.db.execute("UPDATE columns SET name='renamed' WHERE id=?", (column,))
        project.db.commit()
        assert (
            layouts.validate_layout_scope(project, first["id"], sheet, "renamed")["id"]
            == first["id"]
        )
        assert layouts.list_layouts(project, sheet, other) == []


def test_saved_workspace_templates_are_copied_once_without_deleting_originals(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Recipes", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, template = seed(project)
    original = workspace.save_recipe(
        "My old form",
        {
            "action_kind": "media.extract_document",
            "project_id": "p",
            "sheet_id": sheet,
            "reference_row_id": rows[0],
            "params": {"source": "document", "template": template.model_dump()},
        },
    )
    service = DocumentExtractionService(workspace)
    first = service.templates("p", sheet, "document")
    second = service.templates("p", sheet, "document")
    assert first == second
    assert len(first.templates) == 1
    assert (
        first.templates[0].draft.model_dump()["fields"]
        == template.model_dump()["fields"]
    )
    assert workspace.saved_recipe_by_id(original["id"]) == original


def test_prior_bundle_upgrade_preserves_source_cells_and_op_history(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        path = project.path
        values = project.get_values(sheet, column)
        history = [
            tuple(row) for row in project.db.execute("SELECT * FROM ops ORDER BY id")
        ]
    with sqlite3.connect(path / "project.db") as db:
        for table in (
            "extraction_layout_documents",
            "extraction_layout_selection",
            "extraction_layouts",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (EXTRACTION_LAYOUTS_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
    with closing(Project(path)) as upgraded:
        assert (
            upgraded.get_meta(SCHEMA_DIGEST_META_KEY)
            == EXTRACTION_LAYOUTS_TO_DIGEST
            == SCHEMA_DIGEST
        )
        assert upgraded.get_values(sheet, column) == values
        assert [
            tuple(row) for row in upgraded.db.execute("SELECT * FROM ops ORDER BY id")
        ] == history
        assert layouts.list_layouts(upgraded, sheet, column) == []
        assert save(upgraded, sheet, template)["name"] == "Layout 1"

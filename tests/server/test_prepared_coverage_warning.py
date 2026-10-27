from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.server.services.project_qa_sources import read_source_text
from frisket.server.services.project_qa_tools import ProjectQATools
from frisket.server.services.sheet_grid import SheetGridService


@pytest.mark.parametrize("value_mode", ["exact", "bounded"])
def test_grid_and_ask_warn_only_for_partial_document(tmp_path, value_mode):
    project = Project.create(tmp_path / "coverage.frisket")
    try:
        sheet = project.add_sheet("Documents")
        text_col = project.add_column(sheet, "Text", type="text")
        rows = project.add_rows(sheet, [{}, {}, {}], {"Text": text_col})
        project.db.execute("BEGIN IMMEDIATE")
        op = project.append_op("media.read_pdf_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            page_count=3,
        )
        store = PreparedContentStore(project)
        partial = store.stage_reference(
            source_artifact_id=artifact["id"],
            producing_op_id=op,
            pages=[PreparedPageDraft(1, "one"), PreparedPageDraft(3, "")],
            allow_partial=True,
        )
        full_blank = store.stage_reference(
            source_artifact_id=artifact["id"],
            producing_op_id=op,
            pages=[PreparedPageDraft(i, "") for i in range(1, 4)],
        )
        page = store.stage_selector(
            set_id=store.resolve(partial.ref_id).set_id, page_number=3
        )
        project.db.commit()
        project.apply_edits(
            [
                {"row_id": row, "column_id": text_col, "value": ref}
                for row, ref in zip(rows, [partial, full_blank, page])
            ]
        )
        service = SheetGridService(SimpleNamespace(get=lambda _: project))
        payload = service.sheet_data("project", sheet, value_mode=value_mode)
        by_id = {row["id"]: row for row in payload["rows"]}
        warning = "Prepared text is incomplete: 2 of 3 pages are available."
        assert by_id[rows[0]]["meta"][str(text_col)]["warnings"] == [warning]
        assert by_id[rows[0]]["cells"][str(text_col)] == "one\n\n"
        assert not by_id[rows[1]]["meta"][str(text_col)].get("warnings")
        assert not by_id[rows[2]]["meta"][str(text_col)].get("warnings")
        result = read_source_text(project, (sheet, rows[0], text_col), limit=2)
        assert result["warnings"] == [warning]
        assert result["text"] == "on"
        assert result["next_cursor"] is not None
        assert not read_source_text(project, (sheet, rows[1], text_col)).get("warnings")
        assert not read_source_text(project, (sheet, rows[2], text_col)).get("warnings")
        qa_store = ProjectQAStore(project)
        thread = qa_store.create_thread(title="Coverage")
        turn = qa_store.submit_turn(thread["id"], request_id="one", question="Read")
        tools = ProjectQATools(project, turn, qa_store)
        [cell] = tools.read_rows(sheet, [rows[0]], [text_col])["rows"][0]["cells"]
        assert cell["warnings"] == [warning]
        assert cell["value"] == "one\n\n"
        opened = tools.open_source(cell["citation_id"])
        assert opened["warnings"] == [warning]
        assert opened["passages"][0]["text"] == "one\n\n"
        found = tools.find_in_source(cell["citation_id"], "one")
        assert found["warnings"] == [warning]
        project.db.execute("BEGIN IMMEDIATE")
        complete = store.stage_replacement(
            base_ref_id=partial.ref_id,
            producing_op_id=op,
            replacements=[PreparedPageDraft(2, "two")],
            allow_partial=True,
        )
        project.db.commit()
        project.apply_edits(
            [{"row_id": rows[0], "column_id": text_col, "value": complete}]
        )
        refreshed = service.sheet_data("project", sheet, value_mode=value_mode)
        updated = next(row for row in refreshed["rows"] if row["id"] == rows[0])
        assert not updated["meta"][str(text_col)].get("warnings")
        project.undo()
        reverted = service.sheet_data("project", sheet, value_mode=value_mode)
        restored = next(row for row in reverted["rows"] if row["id"] == rows[0])
        assert restored["meta"][str(text_col)]["warnings"] == [warning]
    finally:
        project.close()

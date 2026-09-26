from __future__ import annotations

import frisket.server.services.project_qa_tools as tools_mod
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_tools import ProjectQATools


def test_keyword_search_does_not_pair_stale_excerpt_with_new_value_ref(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "race.frisket", name="Race")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "Text")
    [row] = project.add_rows(sheet, [{"Text": "oldneedle"}], {"Text": column})
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(thread["id"], request_id="one", question="Find it")
    real_search = tools_mod.search_cells_scoped

    def search_then_edit(*args, **kwargs):
        hits = real_search(*args, **kwargs)
        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "replacement"}]
        )
        return hits

    monkeypatch.setattr(tools_mod, "search_cells_scoped", search_then_edit)
    try:
        result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
        assert result["hits"] == []
        assert result["coverage"]["reason"] == "source_changed"
    finally:
        project.close()

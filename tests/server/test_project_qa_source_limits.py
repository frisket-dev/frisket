"""Regression coverage for bounded Ask row observations."""

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_tools import ProjectQATools


def test_read_rows_clips_each_value_without_stopping_later_rows(tmp_path):
    project = Project.create(tmp_path / "source-limits.frisket", name="Source limits")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "Text")
        rows = project.add_rows(
            sheet,
            [{"Text": "a" * 4_000}, {"Text": "b" * 4_000}],
            {"Text": column},
        )
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(thread["id"], request_id="rows", question="Read")

        result = ProjectQATools(project, turn, store).read_rows(sheet, rows, [column])

        assert [row["row_id"] for row in result["rows"]] == rows
        assert all(row["cells"][0]["truncated"] for row in result["rows"])
        assert result["observation_truncated"] is False
    finally:
        project.close()

from types import SimpleNamespace

import pytest

from frisket.engine.runner.row_inputs import read_source_page, row_values
from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot


@pytest.mark.parametrize("template", ["", "Video: {{video}}"])
def test_source_snapshot_is_captured_with_the_values_in_one_read(template):
    calls = []
    source = {"blob_hash": "source-blob"}
    ref = {"kind": "raw", "id": 31}

    def read_with_refs(sheet_id, column_id, *, row_ids):
        calls.append((sheet_id, column_id, row_ids))
        return {7: source}, {7: ref}

    project = SimpleNamespace(get_values_with_refs=read_with_refs)
    recipe = SimpleNamespace(
        capture_source_cells=True, source_columns=lambda spec: ["video"]
    )
    values = row_values(
        project,
        recipe,
        {"sheet_id": 2, "input_template": template},
        {"video": 4},
        7,
        for_model=False,
        column_types={"video": "video"},
    )
    assert calls == [(2, 4, [7])]
    assert values.source_cells == {
        "video": {
            "column_id": 4,
            "column_type": "video",
            "value": source,
            "value_ref": ref,
        }
    }
    if not template:
        assert values["video"] == source


def test_unrelated_row_reader_does_not_request_source_references():
    project = SimpleNamespace(get_values=lambda *args, **kwargs: {7: "plain"})
    recipe = SimpleNamespace(source_columns=lambda spec: ["text"])
    values = row_values(
        project,
        recipe,
        {"sheet_id": 2},
        {"text": 4},
        7,
        for_model=False,
        column_types={"text": "text"},
    )
    assert values == {"text": "plain"}
    assert values.source_cells is None


def test_local_page_retains_exact_refs_after_source_edit_and_closes_snapshot(tmp_path):
    project = Project.create(tmp_path / "source.frisket")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "text", type="text")
        row_id = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
        page = read_source_page(project, sheet, [column], [row_id])
        original_ref = page.cell(sheet, column, row_id)[1]
        project.apply_edits(
            [{"row_id": row_id, "column_id": column, "value": "edited"}]
        )
        recipe = SimpleNamespace(source_columns=lambda spec: ["text"])
        values = row_values(
            project,
            recipe,
            {"sheet_id": sheet},
            {"text": column},
            row_id,
            for_model=False,
            column_types={"text": "text"},
            source_page=page,
        )
        assert values == {"text": "original"}
        assert values.source_cells["text"]["value_ref"] == original_ref
        newer = read_source_page(project, sheet, [column], [row_id])
        assert newer.cell(sheet, column, row_id)[0] == "edited"
        assert newer.cell(sheet, column, row_id)[1] != original_ref
        for kwargs in (
            {"for_model": True},
            {"for_model": False, "admitted_work_scope": {}},
        ):
            with pytest.raises(ValueError, match="require live source reads"):
                row_values(
                    project,
                    recipe,
                    {"sheet_id": sheet},
                    {"text": column},
                    row_id,
                    column_types={"text": "text"},
                    source_page=page,
                    **kwargs,
                )
        # No reader remains pinned after loading a page.
        assert project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        project.close()


def test_local_page_reads_columns_from_one_snapshot(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "coherent.frisket")
    try:
        sheet = project.add_sheet("Source")
        columns = {name: project.add_column(sheet, name) for name in ("a", "b")}
        row_id = project.add_rows(sheet, [{"a": "old a", "b": "old b"}], columns)[0]
        read = ProjectReadSnapshot.get_values_with_refs

        def edit_between_columns(snapshot, sheet_id, column_id, row_ids=None, **kwargs):
            result = read(snapshot, sheet_id, column_id, row_ids=row_ids, **kwargs)
            if column_id == columns["a"]:
                project.apply_edits(
                    [
                        {"row_id": row_id, "column_id": column, "value": f"new {name}"}
                        for name, column in columns.items()
                    ]
                )
            return result

        monkeypatch.setattr(
            ProjectReadSnapshot, "get_values_with_refs", edit_between_columns
        )
        page = read_source_page(project, sheet, list(columns.values()), [row_id])
        assert page.cell(sheet, columns["a"], row_id)[0] == "old a"
        assert page.cell(sheet, columns["b"], row_id)[0] == "old b"
        assert project.get_values(sheet, columns["b"])[row_id] == "new b"
    finally:
        project.close()


def test_local_page_mutable_inputs_do_not_mutate_capture_or_other_rows(tmp_path):
    project = Project.create(tmp_path / "mutable.frisket")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "data", type="json")
        rows = project.add_rows(sheet, [{"data": {"items": [1]}}] * 2, {"data": column})
        page = read_source_page(project, sheet, [column], rows)
        recipe = SimpleNamespace(source_columns=lambda spec: ["data"])

        def read(row_id):
            return row_values(
                project,
                recipe,
                {"sheet_id": sheet},
                {"data": column},
                row_id,
                for_model=False,
                column_types={"data": "json"},
                source_page=page,
            )

        first = read(rows[0])
        first["data"]["items"].append(2)
        assert first.source_cells["data"]["value"] == {"items": [1]}
        assert read(rows[1])["data"] == {"items": [1]}
        assert page.cell(sheet, column, rows[0])[0] == {"items": [1]}
    finally:
        project.close()

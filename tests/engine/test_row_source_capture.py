from types import SimpleNamespace

import pytest

from frisket.engine.runner.row_inputs import read_source_page, row_values
from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot


@pytest.mark.parametrize("template", ["", "Video: {{video}}"])
def test_source_snapshot_is_captured_with_the_values_in_one_read(
    make_project, monkeypatch, template
):
    project = make_project("source")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "video", type="text")
    [row_id] = project.add_rows(sheet, [{"video": "source text"}], {"video": column})
    _, refs = project.get_values_with_refs(sheet, column, row_ids=[row_id])
    calls = []
    original = ProjectReadSnapshot.get_values_with_refs

    def read_with_refs(snapshot, sheet_id, column_id, **kwargs):
        calls.append((sheet_id, column_id, kwargs["row_ids"]))
        return original(snapshot, sheet_id, column_id, **kwargs)

    monkeypatch.setattr(ProjectReadSnapshot, "get_values_with_refs", read_with_refs)
    recipe = SimpleNamespace(
        capture_source_cells=True, source_columns=lambda spec: ["video"]
    )
    values = row_values(
        project,
        recipe,
        {"sheet_id": sheet, "input_template": template},
        {"video": column},
        row_id,
        for_model=False,
        column_types={"video": "text"},
    )
    assert calls == [(sheet, column, [row_id])]
    assert values.source_cells == {
        "video": {
            "column_id": column,
            "column_type": "text",
            "value": "source text",
            "value_ref": refs[row_id],
        }
    }
    if not template:
        assert values["video"] == "source text"


def test_row_reader_keeps_current_source_reference(make_project):
    project = make_project("plain")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "text")
    [row_id] = project.add_rows(sheet, [{"text": "plain"}], {"text": column})
    recipe = SimpleNamespace(source_columns=lambda spec: ["text"])
    values = row_values(
        project,
        recipe,
        {"sheet_id": sheet},
        {"text": column},
        row_id,
        for_model=False,
        column_types={"text": "text"},
    )
    assert values == {"text": "plain"}
    assert values.source_cells["text"]["value"] == "plain"
    assert values.source_cells["text"]["value_ref"] is not None


def test_local_page_retains_exact_refs_after_source_edit_and_closes_snapshot(
    make_project,
):
    project = make_project("source")
    sheet = project.add_sheet("Source")
    column = project.add_column(sheet, "text", type="text")
    row_id = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
    page = read_source_page(project, sheet, [column], [row_id])
    original_ref = page.cell(sheet, column, row_id)[1]
    project.apply_edits([{"row_id": row_id, "column_id": column, "value": "edited"}])
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
    model_values = row_values(
        project,
        recipe,
        {"sheet_id": sheet},
        {"text": column},
        row_id,
        for_model=True,
        column_types={"text": "text"},
        source_page=newer,
        admitted_work_scope={"row_ids": frozenset({row_id})},
    )
    assert model_values["text"] == "edited"
    with pytest.raises(ValueError, match="outside the admitted selection"):
        row_values(
            project,
            recipe,
            {"sheet_id": sheet},
            {"text": column},
            row_id,
            column_types={"text": "text"},
            source_page=newer,
            admitted_work_scope={"row_ids": []},
        )
    # No reader remains pinned after loading a page.
    assert project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


def test_local_page_reads_columns_from_one_snapshot(make_project, monkeypatch):
    project = make_project("coherent")
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


def test_local_page_mutable_inputs_do_not_mutate_capture_or_other_rows(make_project):
    project = make_project("mutable")
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


@pytest.mark.parametrize("paged", [False, True])
def test_invalid_source_values_require_an_explicit_repair_policy(tmp_path, paged):
    project = Project.create(tmp_path / f"invalid-source-{paged}.frisket")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "date", type="date")
        [row_id] = project.add_rows(sheet, [{"date": "12/31/24"}], {"date": column})
        ordinary = SimpleNamespace(source_columns=lambda spec: ["date"])
        repair = SimpleNamespace(
            source_columns=lambda spec: ["date"],
            preserve_invalid_source_columns=frozenset({"date"}),
        )

        def read(recipe, *, preserve_invalid=False):
            page = (
                read_source_page(
                    project,
                    sheet,
                    [column],
                    [row_id],
                    preserve_invalid_column_ids=({column} if preserve_invalid else ()),
                )
                if paged
                else None
            )
            return row_values(
                project,
                recipe,
                {"sheet_id": sheet},
                {"date": column},
                row_id,
                for_model=False,
                column_types={"date": "date"},
                source_page=page,
            )

        assert read(ordinary)["date"] is None
        assert read(repair, preserve_invalid=True)["date"] == "12/31/24"
    finally:
        project.close()

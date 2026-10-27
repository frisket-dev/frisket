from __future__ import annotations

import json
from types import SimpleNamespace

from frisket.engine.store import Project
from frisket.server.services import sheet_grid
from frisket.server.services.sheet_grid import SheetGridService


def test_grid_read_keeps_membership_and_values_in_one_snapshot(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "grid.frisket")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body", type="text")
    [old] = project.add_rows(sheet, [{"body": "old"}], {"body": column})
    writer = Project(project.path)
    service = SheetGridService(SimpleNamespace(get=lambda _: project))
    original = sheet_grid.execute_sheet_row_page
    changed = False
    new = None

    def replace_after_membership(*args, **kwargs):
        nonlocal changed, new
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            writer.db.execute("BEGIN IMMEDIATE")
            writer.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (old,))
            [new] = writer.add_rows(
                sheet, [{"body": "new"}], {"body": column}, commit=False
            )
            writer.db.commit()
        return result

    monkeypatch.setattr(sheet_grid, "execute_sheet_row_page", replace_after_membership)
    first = service.sheet_data("p", sheet, value_mode="bounded")
    assert [(r["id"], r["cells"][str(column)]) for r in first["rows"]] == [(old, "old")]
    second = service.sheet_data("p", sheet, value_mode="bounded")
    assert [(r["id"], r["cells"][str(column)]) for r in second["rows"]] == [
        (new, "new")
    ]
    writer.close()
    project.close()


def test_grid_read_preserves_caller_transaction_and_filtered_scope(tmp_path):
    project = Project.create(tmp_path / "grid.frisket")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body", type="text")
    [old] = project.add_rows(sheet, [{"body": "old"}], {"body": column})
    service = SheetGridService(SimpleNamespace(get=lambda _: project))
    project.db.execute("BEGIN")
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (old,))
    [new] = project.add_rows(sheet, [{"body": "new"}], {"body": column}, commit=False)
    result = service.sheet_data(
        "p",
        sheet,
        value_mode="bounded",
        filter_=json.dumps({"body": {"contains": "new"}}),
    )
    assert [row["id"] for row in result["rows"]] == [new]
    assert project.db.in_transaction
    project.db.rollback()
    result = service.sheet_data("p", sheet, value_mode="bounded")
    assert [row["id"] for row in result["rows"]] == [old]
    project.close()

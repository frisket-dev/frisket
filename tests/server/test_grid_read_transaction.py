from __future__ import annotations

import json
from types import SimpleNamespace

from frisket.engine.store import Project
from frisket.server.services import sheet_grid
from frisket.server.services.sheet_grid import SheetGridService


def test_grid_read_keeps_membership_and_values_in_one_snapshot(
    make_project, monkeypatch
):
    project = make_project("grid")
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


def test_grid_read_preserves_caller_transaction_and_filtered_scope(make_project):
    project = make_project("grid")
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


def test_grid_metadata_excludes_unpublished_paired_output(make_project):

    from frisket.engine.store.result_generations import ResultGenerationStore
    from frisket.engine.store.runs import RunResultStore
    from helpers import run_writer_authority_fixture

    project = make_project("paired")
    sheet = project.add_sheet("Rows")
    columns = [
        project.add_column(sheet, name, ai_generated=True)
        for name in ("answer", "explanation")
    ]
    [row] = project.add_rows(sheet, [{}], {})
    op = project.append_change("map.regex_extract", {})
    runs = RunResultStore(project)
    run = runs.start_run(op, sheet, "map.regex_extract", row_ids=[row], total_rows=1)
    authority = run_writer_authority_fixture(
        project, run, output_column_ids=set(columns)
    )
    generations = ResultGenerationStore(project)
    for column, role in zip(columns, ("answer", "explanation"), strict=True):
        generations.declare(
            run,
            column,
            output_role=role,
            compatibility_key="text-v1",
            write_mode="replace_scope",
            claim_token=authority.claim_token,
        )
    runs.write_results(
        run,
        [
            {
                "row_id": row,
                "column_id": columns[0],
                "value": "unpublished",
                "publication_effect": "publish_value",
                "confidence": 0.9,
                "justification": "private checkpoint",
            }
        ],
        **authority.kwargs(),
    )
    generations.seal(
        run,
        columns,
        claim_token=authority.claim_token,
        terminal_disposition="cancelled",
    )
    runs.finish_run(run, "cancelled", **authority.terminal_kwargs())
    assert (
        project.db.execute(
            "SELECT published FROM results WHERE run_id=?", (run,)
        ).fetchone()[0]
        == 0
    )
    assert project.get_values(sheet, columns[0], [row])[row] is None
    assert sheet_grid._result_metadata(project, columns[0], [row]) == {}

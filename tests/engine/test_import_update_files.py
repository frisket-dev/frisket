"""Direct CSV/XLSX adapters use the same staged publisher as reviewed tables."""

from __future__ import annotations

import csv

import pytest
from openpyxl import Workbook

from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits
from frisket.engine.executor.actions import run_action_spec


def _source(tmp_path, kind, rows):
    path = tmp_path / f"source.{kind}"
    if kind == "xlsx":
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        workbook.save(path)
        workbook.close()
    else:
        with path.open("w", newline="") as output:
            csv.writer(output).writerows(
                [*row, *([None] * (len(rows[0]) - len(row)))] for row in rows
            )
    return path


def _seed(project):
    sheet = project.add_sheet("Stock")
    columns = {
        name: project.add_column(sheet, name, kind)
        for name, kind in (("sku", "text"), ("qty", "integer"), ("note", "text"))
    }
    rows = project.add_rows(
        sheet,
        [
            {"sku": "A1", "qty": 3, "note": "keep"},
            {"sku": "B2", "qty": 8, "note": "other"},
        ],
        columns,
    )
    return sheet, columns, rows


def _request(kind, source, sheet, *, keep=False):
    return {
        "action_id": f"import.update_{kind}",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            **(
                {
                    "sources": [
                        {
                            "path": str(source),
                            "headers": next(
                                csv.reader([source.read_text().splitlines()[0]])
                            ),
                        }
                    ]
                }
                if kind == "csv"
                else {"source": {"kind": "file", "path": str(source)}}
            ),
            "columns": [
                {"source_name": "code", "name": "sku", "type": "text"},
                {"source_name": "amount", "name": "qty", "type": "integer"},
            ],
            "key_columns": ["sku"],
            "update_policy": "incoming_wins",
            "keep_existing_on_blank": keep,
        },
        "idempotency_key": "update-file",
    }


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_file_mapping_unmapped_fields_and_undo(tmp_path, project, kind):
    sheet, columns, rows = _seed(project)
    source = _source(
        tmp_path,
        kind,
        [
            ["code", "amount", "extra"],
            ["A1", 11, "ignored"],
            ["missing", 20, "ignored"],
        ],
    )
    request = _request(kind, source, sheet)
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.errors
    assert result.action.kind == f"import.update_{kind}"
    assert result.outputs[0].ref["edit_count"] == 1
    assert project.get_values(sheet, columns["qty"]) == {rows[0]: 11, rows[1]: 8}
    assert project.get_values(sheet, columns["note"])[rows[0]] == "keep"
    assert project.undo() == result.change_ids[0]
    assert project.get_values(sheet, columns["qty"])[rows[0]] == 3
    source.unlink()
    replay = run_action_spec(project, request, project_id="p")
    assert replay.receipt_id == result.receipt_id


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
@pytest.mark.parametrize("keep", [False, True])
def test_blank_policy_on_missing_trailing_field(tmp_path, project, kind, keep):
    sheet, columns, rows = _seed(project)
    source = _source(tmp_path, kind, [["code", "amount"], ["A1"]])
    result = run_action_spec(
        project, _request(kind, source, sheet, keep=keep), project_id="p"
    )
    assert result.status == "completed", result.errors
    assert project.get_values(sheet, columns["qty"])[rows[0]] == (3 if keep else None)
    assert result.outputs[0].ref["cleared_cells"] == (0 if keep else 1)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_deployment_row_limit_rejects_before_any_publication(tmp_path, project, kind):
    sheet, columns, rows = _seed(project)
    source = _source(tmp_path, kind, [["code", "amount"], ["A1", 11], ["B2", 20]])
    result = run_action_spec(
        project,
        _request(kind, source, sheet),
        project_id="p",
        deps=ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=1)),
    )
    assert result.status == "failed"
    assert result.errors[0].code == "import_workload_limit_exceeded"
    assert project.get_values(sheet, columns["qty"])[rows[0]] == 3
    assert project.db.execute("SELECT count(*) FROM import_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_invalid_numeric_input_is_refused_before_publication(tmp_path, project, kind):
    sheet, columns, rows = _seed(project)
    source = _source(tmp_path, kind, [["code", "amount"], ["A1", "not a number"]])
    result = run_action_spec(project, _request(kind, source, sheet), project_id="p")
    assert result.status == "failed"
    assert project.get_values(sheet, columns["qty"])[rows[0]] == 3


@pytest.mark.parametrize("formula_column", ["A", "B", "C"])
def test_only_mapped_xlsx_formulas_require_cached_values(
    tmp_path, project, formula_column
):
    sheet, columns, rows = _seed(project)
    source = _source(
        tmp_path, "xlsx", [["code", "amount", "unused"], ["A1", 11, "ignored"]]
    )
    from openpyxl import load_workbook

    workbook = load_workbook(source)
    workbook.active[f"{formula_column}2"] = "=1+1"
    workbook.save(source)
    workbook.close()
    result = run_action_spec(project, _request("xlsx", source, sheet), project_id="p")
    if formula_column == "C":
        assert result.status == "completed", result.errors
        assert project.get_values(sheet, columns["qty"])[rows[0]] == 11
    else:
        assert result.status == "failed"
        assert result.errors[0].code == "unresolved_xlsx_formula"
        assert project.get_values(sheet, columns["qty"])[rows[0]] == 3

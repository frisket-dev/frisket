"""`failed` grid-filter operator (failure-triage filter-to-failures).

`{ai_col: {"failed": "any" | <failure outcome>}}` filters to rows whose
CURRENT-run result for that column failed, through the shared sheet-filter
evaluator (`frisket.querysets`) — so /data, saved views, CSV export, and the
map all honor it. The value names a bucket from the result-outcome taxonomy
(`frisket.store.runs`: model_error / invalid_output / empty_output) or "any"
for every failure bucket; it is validated against those module constants,
never interpolated from user input. `withheld_unverified` is a terminal
NON-failure and must not match.
"""

from __future__ import annotations

import json

import pytest

from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "failed.frisket", name="failed")
    yield p
    p.close()


def _failed_filter(column_name: str, value: str) -> str:
    return json.dumps({column_name: {"failed": value}})


def _seed_run_backed_column(project: Project):
    """A 5-row sheet whose ai column's current run produced one row per
    outcome: ok, model_error, invalid_output, empty_output, withheld."""
    sheet = project.add_sheet("data")
    text_col = project.add_column(sheet, "text", type="text")
    ai_col = project.add_column(sheet, "result", type="text", ai_generated=True)
    rows = project.add_rows(
        sheet,
        [{"text": f"row {i}"} for i in range(5)],
        {"text": text_col},
    )
    op = project.append_op("map", {"action_kind": "map.classify"})
    store = RunResultStore(project)
    run = store.start_run(op, sheet, "map.classify", total_rows=len(rows))
    write_claimed_test_results(
        project,
        run,
        [
            {"row_id": rows[0], "column_id": ai_col, "value": "fine", "outcome": "ok"},
            {
                "row_id": rows[1],
                "column_id": ai_col,
                "value": None,
                "error": "provider rate limited",
                "error_code": "provider_rate_limited",
                "outcome": "model_error",
            },
            {
                "row_id": rows[2],
                "column_id": ai_col,
                "value": None,
                "error": "output failed validation",
                "error_code": "invalid_output",
                "outcome": "invalid_output",
            },
            {
                "row_id": rows[3],
                "column_id": ai_col,
                "value": None,
                "error": "model returned no output for a non-empty source",
                "error_code": "empty_output",
                "outcome": "empty_output",
            },
            {
                "row_id": rows[4],
                "column_id": ai_col,
                "value": None,
                "outcome": "withheld_unverified",
            },
        ],
    )
    store.finish_run(run)
    store.point_column_at_run(op, ai_col, run)
    return sheet, rows


def test_failed_any_selects_every_failure_bucket(project):
    sheet, rows = _seed_run_backed_column(project)
    rowset = resolve_sheet_filter_rows(
        project, sheet, filter_=_failed_filter("result", "any")
    )
    assert set(rowset.row_ids) == {rows[1], rows[2], rows[3]}
    assert rowset.total == 3


@pytest.mark.parametrize(
    ("outcome", "row_index"),
    [("model_error", 1), ("invalid_output", 2), ("empty_output", 3)],
)
def test_failed_single_bucket_selects_only_that_outcome(project, outcome, row_index):
    sheet, rows = _seed_run_backed_column(project)
    rowset = resolve_sheet_filter_rows(
        project, sheet, filter_=_failed_filter("result", outcome)
    )
    assert rowset.row_ids == [rows[row_index]]
    assert rowset.total == 1


def test_failed_never_matches_withheld_or_ok(project):
    sheet, rows = _seed_run_backed_column(project)
    rowset = resolve_sheet_filter_rows(
        project, sheet, filter_=_failed_filter("result", "any")
    )
    assert rows[0] not in rowset.row_ids
    assert rows[4] not in rowset.row_ids


def test_failed_on_column_without_current_run_matches_nothing(project):
    sheet, _rows = _seed_run_backed_column(project)
    rowset = resolve_sheet_filter_rows(
        project, sheet, filter_=_failed_filter("text", "any")
    )
    assert rowset.row_ids == []
    assert rowset.total == 0


def test_failed_rejects_values_outside_the_taxonomy(project):
    sheet, _rows = _seed_run_backed_column(project)
    with pytest.raises(SheetRowSetError, match="failed filter"):
        resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=_failed_filter("result", "ok"),
        )
    with pytest.raises(SheetRowSetError, match="failed filter"):
        resolve_sheet_filter_rows(
            project,
            sheet,
            filter_=_failed_filter("result", "someone's WHERE 1=1 --"),
        )


def test_failed_composes_with_other_column_filters(project):
    sheet, rows = _seed_run_backed_column(project)
    spec = json.dumps({"result": {"failed": "any"}, "text": {"contains": "row 1"}})
    rowset = resolve_sheet_filter_rows(project, sheet, filter_=spec)
    assert rowset.row_ids == [rows[1]]

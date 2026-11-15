"""Actual bounded source pages share the existing freshness verifier."""

import pytest

from frisket.engine.store.consumed_source_pages import iter_consumed_source_pages
from frisket.engine.store.consumed_sources import bundle_state, store_capture
from frisket.engine.store.op_log import step_operation
from test_consumed_source_freshness import _seed


@pytest.mark.parametrize("selected", [False, True])
def test_pages_bound_values_and_refs_and_preserve_whole_vs_selected_intent(
    tmp_path, selected
):
    project, sheet, column, _, rows, owner = _seed(tmp_path, count=513)
    try:
        captures, seen = [], []
        with project.read_snapshot() as snapshot:
            for ids, values, refs in iter_consumed_source_pages(
                snapshot,
                sheet,
                [column],
                row_ids=rows[:2] if selected else None,
                batch_size=256,
                captures=captures,
            ):
                assert len(ids) <= 256
                assert set(values[column]) == set(refs[column]) == set(ids)
                seen.extend(ids)
        assert seen == (rows[:2] if selected else rows)
        [capture] = captures
        assert capture["scopes"][0]["cohort"] == (rows[:2] if selected else None)
        with project.transaction_scope():
            bundle = store_capture(
                project, capture, owner_op_id=owner, output_sheet_id=sheet
            )
        assert bundle_state(project, bundle) == "fresh"
        project.add_rows(sheet, [{"input": "appended"}], {"input": column})
        assert bundle_state(project, bundle) == ("fresh" if selected else "stale")
        edit = project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": "v0"}]
        )
        assert bundle_state(project, bundle) == "stale"
        with project.transaction_scope():
            step_operation(project, "revert", op_id=edit)
        assert bundle_state(project, bundle) == ("fresh" if selected else "stale")
    finally:
        project.close()


def test_empty_whole_scope_captures_descriptor_and_membership(tmp_path):
    project, sheet, column, _, _, owner = _seed(tmp_path, count=0)
    try:
        captures = []
        with project.read_snapshot() as snapshot:
            assert (
                list(
                    iter_consumed_source_pages(
                        snapshot, sheet, [column], captures=captures
                    )
                )
                == []
            )
        with project.transaction_scope():
            bundle = store_capture(
                project, captures[0], owner_op_id=owner, output_sheet_id=sheet
            )
        assert bundle_state(project, bundle) == "fresh"
        project.add_rows(sheet, [{"input": "new"}], {"input": column})
        assert bundle_state(project, bundle) == "stale"
    finally:
        project.close()


def test_early_close_keeps_no_complete_source_capture(tmp_path):
    project, sheet, column, _, _, _ = _seed(tmp_path, count=513)
    try:
        captures = []
        with project.read_snapshot() as snapshot:
            pages = iter_consumed_source_pages(
                snapshot, sheet, [column], captures=captures
            )
            assert len(next(pages)[0]) == 256
            pages.close()
        assert captures == []
        with pytest.raises(RuntimeError, match="actual read snapshot"):
            list(iter_consumed_source_pages(project, sheet, [column], captures=[]))
    finally:
        project.close()


@pytest.mark.parametrize("intent", ["whole", "selected"])
def test_frozen_execution_cohort_retains_independent_source_scope_intent(
    tmp_path, intent
):
    project, sheet, column, _, rows, owner = _seed(tmp_path, count=2)
    try:
        frozen = list(rows)
        project.add_rows(
            sheet, [{"input": "arrived after admission"}], {"input": column}
        )
        captures, consumed = [], []
        with project.read_snapshot() as snapshot:
            for ids, _values, _refs in iter_consumed_source_pages(
                snapshot,
                sheet,
                [column],
                row_ids=frozen,
                scope_intent=intent,
                captures=captures,
            ):
                consumed.extend(ids)
        assert consumed == frozen
        with project.transaction_scope():
            bundle = store_capture(
                project, captures[0], owner_op_id=owner, output_sheet_id=sheet
            )
        assert bundle_state(project, bundle) == (
            "stale" if intent == "whole" else "fresh"
        )
    finally:
        project.close()

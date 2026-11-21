"""Inferred output contracts retain the inputs of the actual staging pass."""

import json
from contextlib import contextmanager
import pytest

from frisket.engine.store.consumed_sources import row_input_state, verify_bundle
from frisket.engine.store.op_log import step_operation
from tests.ops.test_formula_host import _run


def _revert(project, op):
    with project.transaction_scope():
        step_operation(project, "revert", change_id=op)


@pytest.mark.parametrize("count", [3, 1001])
def test_inferred_batch_classifies_unavailable_before_computing_and_keeps_one_capture(
    make_project, count
):
    project = make_project("mixed-availability")
    sheet = project.add_sheet("source")
    source = project.add_column(sheet, "amount", type="integer")
    rows = project.add_rows(
        sheet,
        [{"amount": "bad"}] + [{"amount": 2}] * (count - 2) + [{"amount": "bad"}],
        {"amount": source},
    )
    result = _run(project, sheet, "={{amount}} + 1")
    assert result.status == "partial", result.errors
    column = result.outputs[0].column_id
    values, refs = project.get_values_with_refs(sheet, column, row_ids=rows)
    assert values[rows[1]] == 3
    assert refs[rows[0]]["outcome"] == refs[rows[-1]]["outcome"] == "not_completed"
    errors = project.db.execute(
        "SELECT error_code FROM results WHERE run_id=? AND outcome='not_completed' ORDER BY row_id",
        (result.run_id,),
    ).fetchall()
    assert [row[0] for row in errors] == ["input_invalid", "input_invalid"]
    captures = project.db.execute(
        "SELECT capture_json FROM consumed_source_bundles WHERE run_id=?",
        (result.run_id,),
    ).fetchall()
    assert len(captures) == 1
    assert json.loads(captures[0][0])["shared_run"] is True
    assert row_input_state(project, result.run_id, rows[-1]) == "fresh"


def test_all_unavailable_inferred_rows_publish_actual_not_completed_reasons(
    make_project,
):
    project = make_project("all-unavailable")
    sheet = project.add_sheet("source")
    source = project.add_column(sheet, "amount", type="integer")
    rows = project.add_rows(sheet, [{"amount": "bad"}] * 2, {"amount": source})
    result = _run(project, sheet, "={{amount}} + 1")
    assert result.run_id is not None, result.errors
    column = next(
        column["id"]
        for column in project.columns(sheet, include_hidden=True)
        if column["name"] == "computed"
    )
    _, refs = project.get_values_with_refs(sheet, column, row_ids=rows)
    assert {ref["outcome"] for ref in refs.values()} == {"not_completed"}
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT outcome,error_code,published FROM results WHERE run_id=?",
            (result.run_id,),
        )
    ] == [("not_completed", "input_invalid", 1)] * 2


def test_edit_after_staging_never_captures_newer_source_for_old_result(
    make_project, monkeypatch
):
    from frisket.engine.executor import inferred_batch_action

    project = make_project("between-stage-and-publication")
    sheet = project.add_sheet("source")
    source = project.add_column(sheet, "amount", type="number")
    [row] = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
    stage = inferred_batch_action.stage_plan
    edits = []

    def edit_after_stage(*args, **kwargs):
        staged = stage(*args, **kwargs)
        edits.append(
            project.apply_edits([{"row_id": row, "column_id": source, "value": 99}])
        )
        return staged

    monkeypatch.setattr(inferred_batch_action, "stage_plan", edit_after_stage)
    result = _run(project, sheet, "={{amount}} + 1")
    assert result.status == "completed", result.errors
    assert project.get_values(sheet, result.outputs[0].column_id)[row] == 3
    assert row_input_state(project, result.run_id, row) == "stale"
    _revert(project, edits[0])
    assert row_input_state(project, result.run_id, row) == "fresh"


def test_inferred_multi_batch_capture_retains_actual_mixed_versions(
    make_project, monkeypatch
):
    project = make_project("mixed-stage")
    sheet = project.add_sheet("source")
    source = project.add_column(sheet, "amount", type="number")
    rows = project.add_rows(sheet, [{"amount": 2}] * 1001, {"amount": source})
    read = project.read_snapshot
    reads = 0
    edits = []

    @contextmanager
    def interleave():
        nonlocal reads
        with read() as snapshot:
            yield snapshot
        reads += 1
        if reads == 2:
            edits.append(
                project.apply_edits(
                    [{"row_id": rows[0], "column_id": source, "value": 99}]
                )
            )
            edits.append(
                project.apply_edits(
                    [{"row_id": rows[-1], "column_id": source, "value": 100}]
                )
            )

    monkeypatch.setattr(project, "read_snapshot", interleave)
    result = _run(project, sheet, "={{amount}} + 1")
    monkeypatch.setattr(project, "read_snapshot", read)
    assert result.status == "completed", result.errors
    values = project.get_values(sheet, result.outputs[0].column_id)
    assert values[rows[0]] == 3 and values[rows[-1]] == 101
    bundle = project.db.execute(
        "SELECT id FROM consumed_source_bundles WHERE run_id=?", (result.run_id,)
    ).fetchone()[0]
    assert row_input_state(project, result.run_id, rows[-1]) == "unknown"
    assert verify_bundle(project, bundle) == "stale"
    assert row_input_state(project, result.run_id, rows[-1]) == "stale"
    _revert(project, edits[0])
    assert verify_bundle(project, bundle) == "fresh"
    assert row_input_state(project, result.run_id, rows[-1]) == "fresh"
    _revert(project, edits[1])
    assert verify_bundle(project, bundle) == "stale"
    assert row_input_state(project, result.run_id, rows[-1]) == "stale"


def test_later_append_does_not_expand_a_compatible_replacement_cohort(
    make_project, monkeypatch
):
    from frisket.engine.executor import inferred_batch_action

    project = make_project("append-compatible")
    sheet = project.add_sheet("source")
    source = project.add_column(sheet, "amount", type="number")
    [row] = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
    initial = _run(project, sheet, "={{amount}} + 1")
    column = initial.outputs[0].column_id
    stage = inferred_batch_action.stage_plan
    appended = []

    def append_after_stage(*args, **kwargs):
        staged = stage(*args, **kwargs)
        appended.extend(project.add_rows(sheet, [{"amount": 3}], {"amount": source}))
        project.apply_edits([{"row_id": appended[0], "column_id": column, "value": 99}])
        return staged

    monkeypatch.setattr(inferred_batch_action, "stage_plan", append_after_stage)
    result = _run(
        project,
        sheet,
        "={{amount}} * 2",
        key="compatible-replacement",
        replace_existing=True,
    )
    assert result.status == "completed", result.errors
    assert project.get_values(sheet, column) == {row: 4, appended[0]: 99}
    assert row_input_state(project, result.run_id, row) == "stale"


def test_cancel_after_inference_before_publication_closes_retained_run(
    make_project, monkeypatch
):
    from frisket.engine.executor import inferred_batch_action
    from frisket.engine.store.runs import RunResultStore

    project = make_project("cancel-before-publish")
    sheet = project.add_sheet("source")
    source = project.add_column(sheet, "amount", type="integer")
    project.add_rows(sheet, [{"amount": 2}], {"amount": source})
    publish = inferred_batch_action._publish

    def stop_before_publish(project, *args, **kwargs):
        assert RunResultStore(project).request_cancel(kwargs["run_id"])
        return publish(project, *args, **kwargs)

    monkeypatch.setattr(inferred_batch_action, "_publish", stop_before_publish)
    result = _run(project, sheet, "={{amount}} + 1")
    assert result.status == "cancelled", result.errors
    run = RunResultStore(project).get_run(result.run_id)
    assert run["status"] == "cancelled"
    assert run["current_attempt_id"] is None
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (result.run_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cell_result_heads WHERE run_id=?",
            (result.run_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM generated_column_claims WHERE run_id=? AND status='active'",
            (result.run_id,),
        ).fetchone()[0]
        == 0
    )

"""Unavailable input is uncompleted work, distinct from a valid null result."""

import pytest

from frisket.engine.store.input_availability import (
    input_unavailability,
    not_completed_result,
)


@pytest.mark.parametrize(
    "ref,code",
    [
        ({"outcome": "pending", "validity": "missing"}, "input_pending"),
        ({"outcome": "failed", "validity": "missing"}, "input_failed"),
        ({"outcome": "not_completed", "validity": "missing"}, "input_not_completed"),
        ({"outcome": "completed", "validity": "invalid"}, "input_invalid"),
        ({"outcome": "completed", "validity": "missing"}, "input_missing"),
    ],
)
def test_required_source_reason_is_retained_and_optional_source_remains_available(
    ref, code
):
    reason = input_unavailability("source", ref)
    assert reason["code"] == code
    assert not_completed_result(reason)["outcome"] == "not_completed"
    assert input_unavailability("source", ref, required=False) is None


def test_missing_capable_transform_keeps_null_but_does_not_coerce_invalid_to_missing():
    assert (
        input_unavailability(
            "source",
            {"outcome": "completed", "validity": "missing"},
            allow_missing=True,
        )
        is None
    )
    assert (
        input_unavailability(
            "source",
            {"outcome": "completed", "validity": "invalid"},
            allow_missing=True,
        )["code"]
        == "input_invalid"
    )
    assert (
        input_unavailability(
            "source",
            {"outcome": "completed", "validity": "invalid"},
            preserve_invalid=True,
        )
        is None
    )


def test_canonical_current_outcome_retains_uncompleted_reason(tmp_path):
    from frisket.engine.store.result_generations import ResultGenerationStore
    from test_result_generation_store import (
        _seed_project,
        _start_claimed_run,
        _declare,
        _write,
        _seal,
    )

    project, sheet, column, rows = _seed_project(tmp_path, row_count=1)
    try:
        run = _start_claimed_run(
            project,
            sheet_id=sheet,
            output_column_id=column,
            row_ids=rows,
            label="unavailable input",
        )
        _declare(ResultGenerationStore(project), run, column, write_mode="create")
        reason = input_unavailability(
            "source", {"outcome": "pending", "validity": "missing"}
        )
        _write(
            project,
            run,
            [{"row_id": rows[0], "column_id": column, **not_completed_result(reason)}],
        )
        _seal(ResultGenerationStore(project), run, column)
        values, refs = project.get_values_with_refs(sheet, column, row_ids=rows)
        assert values[rows[0]] is None
        assert refs[rows[0]]["outcome"] == "not_completed"
        result = project.db.execute(
            "SELECT error_code,outcome FROM results WHERE run_id=?", (run.run_id,)
        ).fetchone()
        assert tuple(result) == ("input_pending", "not_completed")
    finally:
        project.close()

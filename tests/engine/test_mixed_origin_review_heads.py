from __future__ import annotations

from typing import Any

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.runner.review import (
    queue_count,
    review_bundle_count,
    review_bundle_page,
    review_bundles,
    review_queue,
)
from test_mixed_origin_readers import (  # noqa: F401
    _MixedReaderFixture,
    mixed_reader,
)


def _decision(*, run_id: int, row_id: int, column_id: int, key: str) -> dict[str, Any]:
    return {
        "action_id": "review.decision",
        "scope": {"kind": "project"},
        "params": {
            "run_id": run_id,
            "row_id": row_id,
            "column_id": column_id,
            "decision": "accept",
        },
        "idempotency_key": key,
    }


def _review_state(fixture: _MixedReaderFixture, *, run_id: int, row_id: int) -> str:
    row = fixture.project.db.execute(
        "SELECT review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, fixture.output_column_id),
    ).fetchone()
    assert row is not None
    return str(row["review_state"])


def test_review_queue_bundles_counts_and_summary_follow_exact_mixed_heads(
    request: pytest.FixtureRequest,
) -> None:
    fixture: _MixedReaderFixture = request.getfixturevalue("mixed_reader")
    project = fixture.project

    queue = review_queue(project, sheet_id=fixture.sheet_id)
    assert [(item["run_id"], item["row_id"], item["value"]) for item in queue] == [
        (fixture.first_run_id, fixture.row_ids[3], "first-untargeted"),
        (fixture.second_run_id, fixture.row_ids[0], "second-value"),
        (fixture.second_run_id, fixture.row_ids[1], None),
    ]
    assert queue_count(project) == 3
    assert review_bundle_count(project, sheet_id=fixture.sheet_id) == 3

    bundles = review_bundles(project, sheet_id=fixture.sheet_id)
    assert [(bundle["run_id"], bundle["row_id"]) for bundle in bundles] == [
        (fixture.first_run_id, fixture.row_ids[3]),
        (fixture.second_run_id, fixture.row_ids[0]),
        (fixture.second_run_id, fixture.row_ids[1]),
    ]
    retained = bundles[0]
    assert retained["fields"][0]["value"] == "first-untargeted"
    assert retained["fields"][0]["run_id"] == fixture.first_run_id

    page = review_bundle_page(project, sheet_id=fixture.sheet_id, offset=0, limit=2)
    assert page["total"] == 3
    assert len(page["bundles"]) == 2
    assert project.refresh_pending_review_summary() == 3


def test_review_decision_requires_the_active_exact_head(
    request: pytest.FixtureRequest,
) -> None:
    fixture: _MixedReaderFixture = request.getfixturevalue("mixed_reader")
    project = fixture.project
    column_id = fixture.output_column_id

    retained_row = fixture.row_ids[3]
    retained = run_action_spec(
        project,
        _decision(
            run_id=fixture.first_run_id,
            row_id=retained_row,
            column_id=column_id,
            key="mixed-review-retained@sha256:v1",
        ),
        project_id="mixed-review",
    )
    assert retained.status == "completed", retained.errors
    assert (
        _review_state(fixture, run_id=fixture.first_run_id, row_id=retained_row)
        == "verified"
    )
    assert review_bundle_count(project) == 2

    stale_direct = run_action_spec(
        project,
        _decision(
            run_id=fixture.first_run_id,
            row_id=fixture.row_ids[0],
            column_id=column_id,
            key="mixed-review-stale-direct@sha256:v1",
        ),
        project_id="mixed-review",
    )
    assert stale_direct.status == "failed"
    assert stale_direct.errors[0].code == "review_target_not_found"
    assert (
        _review_state(fixture, run_id=fixture.first_run_id, row_id=fixture.row_ids[0])
        == "unreviewed"
    )

    error_row = fixture.row_ids[2]
    error_review = run_action_spec(
        project,
        _decision(
            run_id=fixture.second_run_id,
            row_id=error_row,
            column_id=column_id,
            key="mixed-review-error@sha256:v1",
        ),
        project_id="mixed-review",
    )
    assert error_review.status == "completed", error_review.errors
    assert (
        _review_state(fixture, run_id=fixture.second_run_id, row_id=error_row)
        == "verified"
    )

    null_row = fixture.row_ids[1]
    accepted = run_action_spec(
        project,
        _decision(
            run_id=fixture.second_run_id,
            row_id=null_row,
            column_id=column_id,
            key="mixed-review-null@sha256:v1",
        ),
        project_id="mixed-review",
    )
    assert accepted.status == "completed", accepted.errors
    assert (
        _review_state(fixture, run_id=fixture.second_run_id, row_id=null_row)
        == "verified"
    )
    assert review_bundle_count(project) == 1
    assert project.refresh_pending_review_summary() == 1

    stale = run_action_spec(
        project,
        _decision(
            run_id=fixture.first_run_id,
            row_id=fixture.row_ids[0],
            column_id=column_id,
            key="mixed-review-stale@sha256:v1",
        ),
        project_id="mixed-review",
    )
    assert stale.status == "failed"
    assert stale.errors[0].code == "review_target_not_found"
    assert (
        _review_state(fixture, run_id=fixture.first_run_id, row_id=fixture.row_ids[0])
        == "unreviewed"
    )

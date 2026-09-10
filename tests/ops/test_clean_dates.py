"""Behavioral coverage for the typed ``map.clean_dates`` action."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest, Row, SheetRows
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.text import CleanDatesParams, clean_date, parse_date
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.runner import MapRunner
from frisket.engine.runner.review import review_queue
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12/06/2026", dt.date(2026, 12, 6)),
        ("14/03/2024", dt.date(2024, 3, 14)),
        ("June 12, 2026", dt.date(2026, 6, 12)),
        ("3 de mayo de 2021", dt.date(2021, 5, 3)),
        (
            "2026-06-12 14:30",
            dt.datetime(2026, 6, 12, 14, 30, tzinfo=dt.UTC),
        ),
        (
            "2026-06-12T14:30:00+02:00",
            dt.datetime(2026, 6, 12, 12, 30, tzinfo=dt.UTC),
        ),
        (
            "2026-06-12T00:00:00+02:00",
            dt.datetime(2026, 6, 11, 22, 0, tzinfo=dt.UTC),
        ),
        (
            "2026-06-12T00:00:00Z",
            dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.UTC),
        ),
    ],
)
def test_auto_date_parsing(raw: str, expected: dt.date | dt.datetime) -> None:
    assert parse_date(raw) == expected


def test_partial_and_unparseable_dates_are_not_silently_completed() -> None:
    assert parse_date("March 2020") is None
    result = clean_date(
        CleanDatesParams(source="raw"),
        Row({"raw": "the third quarter, probably"}),
    ).output.cleaned
    assert result.value is None
    assert result.confidence == 0.0
    assert "the third quarter, probably" in str(result.justification)
    assert "source column" in str(result.justification)


def test_exact_format_selects_field_order_and_rejects_mismatch() -> None:
    assert parse_date("03/04/2025", format="%d/%m/%Y") == dt.date(2025, 4, 3)
    assert parse_date("2025-01-15", format="%d/%m/%Y") is None


def _runner(project: Project, router: Any | None) -> MapRunner:
    return MapRunner(
        project,
        router or ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def test_unparseable_rows_reach_review_through_typed_lifecycle(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "clean-dates.frisket", name="dates")
    try:
        sheet_id = project.add_sheet("filings")
        source_id = project.add_column(sheet_id, "filed", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"filed": "12/06/2026"}, {"filed": "Sept the first-ish"}],
            {"filed": source_id},
        )
        request = ActionRequest(
            action_id="map.clean_dates",
            scope=SheetRows(sheet_id=sheet_id, row_ids=tuple(row_ids)),
            params={"source": "filed"},
            output_names={"cleaned": "filed_iso"},
            idempotency_key="clean-dates@behavior",
        )

        result = run_typed_map_rows_action(
            project,
            "project-1",
            BoundTypedActionRequest.bind(
                ACTION_REGISTRY.get(request.action_id), request
            ),
            None,
            _runner,
        )

        assert result.status == "completed"
        output_id = result.outputs[0].column_id
        assert output_id is not None
        assert project.get_values(sheet_id, output_id, row_ids=row_ids) == {
            row_ids[0]: "2026-12-06",
            row_ids[1]: None,
        }
        queue = review_queue(project, sheet_id=sheet_id)
        assert queue[0]["confidence"] == 0.0
        assert "Sept the first-ish" in queue[0]["justification"]
        assert "Sept the first-ish" in project.get_values(sheet_id, source_id).values()
    finally:
        project.close()

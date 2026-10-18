from __future__ import annotations

from pathlib import Path

import pytest

from frisket.authoring import column_types
from frisket.contracts.actions.schemas.imports import normalize_import_value
from frisket.engine.store import Project
from frisket.iso_datetimes import parse_iso_datetime


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-12T14:30:00",
        "2026-06-12T14:30:00.123456",
        "2026-06-12T14:30:00Z",
        "2026-06-12T14:30:00+02:00",
        "2026-06-12T14:30:00-05:30",
    ],
)
def test_datetime_accepts_complete_local_and_aware_iso_values(value: str) -> None:
    assert parse_iso_datetime(value) is not None
    assert column_types.validate_value("datetime", value)
    assert normalize_import_value("datetime", value) == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-12",
        "2026-06-12T14:30",
        "2026-06-12 14:30:00",
        "2026-06-12T14:30:00z",
        "2026-06-12T14:30:00+0200",
        "2026-06-12T14:30:00+02:60",
        "2026-06-12T14:30:00+24:00",
        "2026-13-12T14:30:00",
        "2026-06-12T25:30:00",
        " 2026-06-12T14:30:00 ",
        "",
        20260612,
    ],
)
def test_datetime_rejects_dates_incomplete_or_noncanonical_values(
    value: object,
) -> None:
    assert parse_iso_datetime(value) is None
    assert not column_types.validate_value("datetime", value)


def test_datetime_has_a_distinct_renderer_without_a_date_facet() -> None:
    spec = column_types.get_column_type("datetime")
    assert spec is not None
    assert spec.core
    assert spec.presentation == {"renderer": "datetime"}
    assert column_types.range_facet_value_kind("datetime") is None


def test_datetime_round_trips_naive_and_aware_values_as_exact_text(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "datetimes.frisket", name="datetimes")
    try:
        sheet_id = project.add_sheet("Dates")
        column_id = project.add_column(sheet_id, "When", type="datetime")
        values = [
            "2026-06-12T14:30:00",
            "2026-06-12T14:30:00-05:30",
        ]
        row_ids = project.add_rows(
            sheet_id,
            [{"When": value} for value in values],
            {"When": column_id},
        )

        assert project.get_values(sheet_id, column_id, row_ids) == dict(
            zip(row_ids, values, strict=True)
        )
        assert {
            str(row["value_kind"])
            for row in project.db.execute(
                "SELECT value_kind FROM cells WHERE column_id=?", (column_id,)
            )
        } == {"text"}
    finally:
        project.close()

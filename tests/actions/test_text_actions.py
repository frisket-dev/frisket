from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from frisket.actions.types import Outcome, Row, Template
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.text import (
    CLEAN_DATES,
    TEMPLATE,
    CleanDatesParams,
    clean_date,
    render_template,
)


def test_template_renders_columns_and_literal_text() -> None:
    row = Row({"name": "Ada"})

    rendered = render_template(
        TEMPLATE.run.params_model(template=Template(text="Hello, {{name}}!")),
        row,
    )
    literal = render_template(
        TEMPLATE.run.params_model(template=Template(text="No columns here")),
        row,
    )

    assert rendered.output.rendered == "Hello, Ada!"
    assert literal.output.rendered == "No columns here"
    assert TEMPLATE.run.output_fields[0].key == "rendered"


def test_text_action_catalogs_derive_source_requirements_from_params() -> None:
    entries = {
        action.action_id: action.catalog_entry() for action in ACTION_REGISTRY.actions
    }

    assert entries["map.clean_dates"]["ui_hints"]["source_requirements"] == [
        {
            "id": "source",
            "param": "source",
            "label": "Source",
            "mode": "column",
            "min": 1,
            "accepted_column_types": [
                "text",
                "category",
                "date",
                "number",
                "integer",
            ],
        }
    ]
    assert entries["map.clean_dates"]["ui_hints"]["logical_outputs"] == [
        {"key": "cleaned", "column_type": "date"}
    ]
    assert entries["map.template"]["ui_hints"]["source_requirements"] == [
        {
            "id": "template",
            "param": "template",
            "label": "Template",
            "mode": "template",
            "min": 0,
            "template_columns": "union",
        }
    ]
    assert entries["map.regex_extract"]["ui_hints"]["category"] == "extract"
    assert entries["map.columns_from_json"]["ui_hints"]["category"] == "extract"


@pytest.mark.parametrize(
    ("raw", "format", "expected"),
    [
        (None, None, Outcome.ok(None)),
        ("", None, Outcome.ok(None)),
        ("2026-09-04", None, Outcome.ok(dt.date(2026, 9, 4), confidence=1.0)),
        (
            "09/04/2026",
            "%m/%d/%Y",
            Outcome.ok(dt.date(2026, 9, 4), confidence=1.0),
        ),
        (
            "2026-09-04T12:30:00-04:00",
            None,
            Outcome.ok(
                dt.datetime(2026, 9, 4, 16, 30, tzinfo=dt.UTC),
                confidence=1.0,
            ),
        ),
    ],
)
def test_clean_dates_successes(
    raw: str | None,
    format: str | None,
    expected: Outcome[dt.date | dt.datetime | None],
) -> None:
    params = CleanDatesParams(source="published", format=format)

    result = clean_date(params, Row({"published": raw}))

    assert result.output.cleaned == expected


def test_clean_dates_marks_unparseable_values_for_review() -> None:
    result = clean_date(
        CleanDatesParams(source="published"),
        Row({"published": "definitely not a date"}),
    )

    assert result.output.cleaned == Outcome.ok(
        None,
        confidence=0.0,
        justification=(
            "could not parse 'definitely not a date' as a date "
            "(original kept in the source column)"
        ),
    )
    assert CLEAN_DATES.run.output_fields[0].key == "cleaned"
    assert CLEAN_DATES.run.output_fields[0].column_type == "date"


def test_clean_dates_format_is_trimmed_and_validated() -> None:
    assert (
        CleanDatesParams(source="published", format="  %Y-%m-%d  ").format == "%Y-%m-%d"
    )
    assert CleanDatesParams(source="published", format="   ").format is None

    with pytest.raises(ValidationError, match="invalid strftime format"):
        CleanDatesParams(source="published", format="%Q")

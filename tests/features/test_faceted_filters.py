from __future__ import annotations

import json
import math

import pytest

from frisket.authoring import column_types
from frisket.engine.store import Project
from frisket.preview.column_values import resolve_column_values_preview
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows


def _seed(tmp_path):
    project = Project.create(tmp_path / "facets.frisket", name="facets")
    sheet = project.add_sheet("Cases")
    columns = {
        "status": project.add_column(sheet, "status", type="category"),
        "amount": project.add_column(sheet, "amount", type="number"),
        "filed": project.add_column(sheet, "filed", type="date"),
    }
    rows = project.add_rows(
        sheet,
        [
            {"status": "open", "amount": 10, "filed": "2026-01-01"},
            {"status": "closed", "amount": 25, "filed": "2026-01-02"},
            {"status": "open", "amount": 50, "filed": "2026-02-01"},
            {"status": "pending", "amount": 75, "filed": "2026-03-01"},
            {"status": "closed", "amount": 100, "filed": "2026-04-01"},
        ],
        columns,
    )
    return project, sheet, rows


def _row_ids(project: Project, sheet: int, spec: dict) -> list[int]:
    return resolve_sheet_filter_rows(
        project,
        sheet,
        filter_=json.dumps(spec),
    ).row_ids


def test_facets_or_values_within_a_column_and_and_across_columns(tmp_path):
    project, sheet, rows = _seed(tmp_path)
    try:
        assert _row_ids(
            project,
            sheet,
            {
                "status": {"in": ["open", "pending"]},
                "amount": {"between": {"start": "40", "end": "80"}},
            },
        ) == [rows[2], rows[3]]
    finally:
        project.close()


def test_numeric_and_date_range_bounds_are_typed(tmp_path):
    project, sheet, rows = _seed(tmp_path)
    try:
        assert _row_ids(project, sheet, {"amount": {"gte": "75"}}) == [
            rows[3],
            rows[4],
        ]
        assert _row_ids(
            project,
            sheet,
            {"filed": {"between": {"start": "2026-01-02", "end": "2026-03-01"}}},
        ) == [rows[1], rows[2], rows[3]]
    finally:
        project.close()


def test_numeric_ranges_do_not_coerce_malformed_cells_to_zero(tmp_path):
    project, sheet, _rows = _seed(tmp_path)
    try:
        amount_column = next(
            column for column in project.columns(sheet) if column["name"] == "amount"
        )
        malformed_row = project.add_rows(
            sheet,
            [{"amount": "n/a"}],
            {"amount": int(amount_column["id"])},
        )[0]

        matched = _row_ids(
            project,
            sheet,
            {"amount": {"between": {"start": "-0.5", "end": "0.5"}}},
        )
        assert malformed_row not in matched
        assert matched == []
    finally:
        project.close()


def test_numeric_histograms_and_ranges_share_native_json_number_validity(tmp_path):
    project = Project.create(tmp_path / "facet-number-validity.frisket", name="facets")
    sheet = project.add_sheet("Numbers")
    amount = project.add_column(sheet, "amount", type="number")
    rows = project.add_rows(
        sheet,
        [{"amount": -1}, {"amount": 1.5}, {"amount": "0"}, {"amount": True}],
        {"amount": amount},
    )
    try:
        preview = resolve_column_values_preview(
            project, sheet_id=sheet, input_column="amount"
        )
        assert sum(bin_["count"] for bin_ in preview["distribution"]["bins"]) == 2
        assert (
            _row_ids(
                project,
                sheet,
                {"amount": {"between": {"start": "-2", "end": "2"}}},
            )
            == rows[:2]
        )
    finally:
        project.close()


def test_integer_ranges_keep_signed_64_bit_endpoints_exact(tmp_path):
    project = Project.create(tmp_path / "facet-integer-exact.frisket", name="facets")
    sheet = project.add_sheet("Identifiers")
    identifier = project.add_column(sheet, "identifier", type="integer")
    rows = project.add_rows(
        sheet,
        [
            {"identifier": -(2**63)},
            {"identifier": 2**53 + 1},
            {"identifier": 2**63 - 1},
        ],
        {"identifier": identifier},
    )
    try:
        invalid_row = project.db.execute(
            "INSERT INTO rows (sheet_id, position) VALUES (?, ?)", (sheet, 4)
        ).lastrowid
        project.db.execute(
            "INSERT INTO cells (row_id, column_id, value) VALUES (?, ?, ?)",
            (invalid_row, identifier, json.dumps(2**63)),
        )
        project.db.commit()
        preview = resolve_column_values_preview(
            project, sheet_id=sheet, input_column="identifier"
        )
        distribution = preview["distribution"]
        assert distribution["kind"] == "integer"
        assert distribution["min"] == "-9223372036854775808"
        assert distribution["max"] == "9223372036854775807"
        assert sum(bin_["count"] for bin_ in distribution["bins"]) == 3
        assert (
            _row_ids(
                project,
                sheet,
                {"identifier": {"gte": "9007199254740993"}},
            )
            == rows[1:]
        )
        with pytest.raises(SheetRowSetError, match="canonical decimal string"):
            _row_ids(project, sheet, {"identifier": {"gte": "9007199254740993.0"}})
        with pytest.raises(SheetRowSetError, match="signed 64-bit"):
            _row_ids(project, sheet, {"identifier": {"gte": str(2**63)}})
    finally:
        project.close()


def test_registered_plugin_number_facet_previews_and_filters_end_to_end(tmp_path):
    type_name = "plugin_number_facet"
    column_types.register_column_type(
        type_name,
        validate=lambda value: (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
        presentation={
            "renderer": "number",
            "facet": {"kind": "range", "valueKind": "number"},
        },
        plugin="test.facets",
    )
    project = Project.create(tmp_path / "plugin-number.frisket", name="facets")
    try:
        sheet = project.add_sheet("Plugin numbers")
        score = project.add_column(sheet, "score", type=type_name)
        rows = project.add_rows(
            sheet,
            [{"score": 1}, {"score": 2.5}, {"score": 10}],
            {"score": score},
        )
        preview = resolve_column_values_preview(
            project, sheet_id=sheet, input_column="score"
        )
        assert preview["distribution"]["kind"] == "number"
        assert preview["distribution"]["min"] == 1
        assert preview["distribution"]["max"] == 10
        assert _row_ids(
            project,
            sheet,
            {"score": {"between": {"start": "2", "end": "3"}}},
        ) == [rows[1]]
    finally:
        project.close()
        column_types.unregister_column_type(type_name)


def test_boolean_in_rejects_unknown_spellings(tmp_path):
    project = Project.create(tmp_path / "facet-boolean.frisket", name="facets")
    sheet = project.add_sheet("Flags")
    flag = project.add_column(sheet, "flag", type="boolean")
    rows = project.add_rows(
        sheet,
        [{"flag": True}, {"flag": False}],
        {"flag": flag},
    )
    try:
        assert _row_ids(project, sheet, {"flag": {"in": ["yes", "off"]}}) == rows
        with pytest.raises(SheetRowSetError, match="invalid boolean filter"):
            _row_ids(project, sheet, {"flag": {"in": ["banana"]}})
    finally:
        project.close()


@pytest.mark.parametrize(
    ("condition", "match"),
    [
        ({"in": []}, "non-empty array"),
        ({"in": [1]}, "string values only"),
        ({"in": [str(index) for index in range(101)]}, "at most 100 values"),
        ({"between": {"start": "nope", "end": "4"}}, "numeric start and end"),
        ({"gte": True}, "requires a numeric value"),
        ({"gte": "NaN"}, "requires a finite value"),
        ({"lte": "Infinity"}, "requires a finite value"),
    ],
)
def test_faceted_filter_payloads_fail_closed(tmp_path, condition, match):
    project, sheet, _rows = _seed(tmp_path)
    try:
        with pytest.raises(SheetRowSetError, match=match):
            _row_ids(project, sheet, {"amount": condition})
    finally:
        project.close()


def test_column_preview_includes_exact_numeric_and_date_histograms(tmp_path):
    project, sheet, _rows = _seed(tmp_path)
    try:
        amount = resolve_column_values_preview(
            project,
            sheet_id=sheet,
            input_column="amount",
            limit=2,
        )
        assert amount["truncated"] is True
        assert amount["distribution"]["kind"] == "number"
        assert amount["distribution"]["min"] == 10
        assert amount["distribution"]["max"] == 100
        assert sum(bin_["count"] for bin_ in amount["distribution"]["bins"]) == 5

        filed = resolve_column_values_preview(
            project,
            sheet_id=sheet,
            input_column="filed",
            limit=2,
        )
        assert filed["distribution"]["kind"] == "date"
        assert filed["distribution"]["min"] == "2026-01-01"
        assert filed["distribution"]["max"] == "2026-04-01"
        assert sum(bin_["count"] for bin_ in filed["distribution"]["bins"]) == 5
    finally:
        project.close()


def test_histograms_include_timestamps_and_extreme_finite_numbers(tmp_path):
    project = Project.create(tmp_path / "facet-extremes.frisket", name="facets")
    sheet = project.add_sheet("Events")
    columns = {
        "amount": project.add_column(sheet, "amount", type="number"),
        "filed": project.add_column(sheet, "filed", type="date"),
    }
    project.add_rows(
        sheet,
        [
            {"amount": -1e308, "filed": "2026-01-01T23:59:00Z"},
            {"amount": 1e308, "filed": "2026-03-31T23:30:00-02:00"},
        ],
        columns,
    )
    try:
        amount = resolve_column_values_preview(
            project, sheet_id=sheet, input_column="amount"
        )
        assert sum(bin_["count"] for bin_ in amount["distribution"]["bins"]) == 2
        assert all(
            math.isfinite(float(bin_[bound]))
            for bin_ in amount["distribution"]["bins"]
            for bound in ("start", "end")
        )

        filed = resolve_column_values_preview(
            project, sheet_id=sheet, input_column="filed"
        )
        assert filed["distribution"]["min"] == "2026-01-01"
        assert filed["distribution"]["max"] == "2026-04-01"
        assert sum(bin_["count"] for bin_ in filed["distribution"]["bins"]) == 2
    finally:
        project.close()


def test_histogram_handles_subnormal_spans_without_dropping_counts(tmp_path):
    project = Project.create(tmp_path / "facet-subnormal.frisket", name="facets")
    sheet = project.add_sheet("Tiny")
    amount = project.add_column(sheet, "amount", type="number")
    project.add_rows(
        sheet,
        [{"amount": 5e-324}, {"amount": 1e-323}, {"amount": 5e-324}],
        {"amount": amount},
    )
    try:
        preview = resolve_column_values_preview(
            project, sheet_id=sheet, input_column="amount"
        )
        distribution = preview["distribution"]
        assert distribution["min"] == 5e-324
        assert distribution["max"] == 1e-323
        assert sum(bin_["count"] for bin_ in distribution["bins"]) == 3
    finally:
        project.close()

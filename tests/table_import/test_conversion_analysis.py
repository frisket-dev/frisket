from __future__ import annotations

from dataclasses import asdict

from frisket.table_import import (
    ColumnStatsAccumulator,
    PreviewAccumulator,
    RawCell,
    RawRow,
    RowIssue,
    SourceColumn,
    TableRow,
    convert_cell,
)
from frisket.table_import.analysis import PREVIEW_TEXT_CHARS


def _row(ordinal: int, *values: str, role: str = "data") -> TableRow:
    cells = tuple(RawCell(value) for value in values)
    source = RawRow(ordinal, ordinal, ordinal, cells)
    return TableRow(role=role, source=source, cells=cells)  # type: ignore[arg-type]


def test_conversion_preserves_text_and_reports_changes_and_failures() -> None:
    text = convert_cell(RawCell(" 00418823 "), "text")
    integer = convert_cell(RawCell("00418823"), "integer")
    number = convert_cell(
        RawCell("1.234,50"),
        "number",
        decimal_separator=",",
        thousands_separator=".",
    )
    invalid = convert_cell(RawCell("N/A"), "number")
    moment = convert_cell(RawCell("2026-10-18 12:30"), "datetime")

    assert (text.value, text.display, text.outcome) == (
        " 00418823 ",
        " 00418823 ",
        "unchanged",
    )
    assert (integer.value, integer.display, integer.outcome) == (
        418_823,
        "418823",
        "changed",
    )
    assert (number.value, number.display, number.outcome) == (
        1234.5,
        "1,234.50",
        "unchanged",
    )
    assert invalid.value is None
    assert invalid.outcome == "invalid"
    assert invalid.issue is not None
    assert (moment.value, moment.display, moment.outcome) == (
        "2026-10-18T12:30:00",
        "2026-10-18 12:30:00",
        "unchanged",
    )


def test_conversion_marks_only_semantic_loss_or_introduced_time_as_changed() -> None:
    unchanged = (
        convert_cell(RawCell("Y"), "boolean"),
        convert_cell(RawCell("Mar 4, 2024"), "date"),
        convert_cell(
            RawCell("1.250,00"),
            "number",
            decimal_separator=",",
            thousands_separator=".",
        ),
        convert_cell(RawCell("03/04/2026 12:30"), "datetime"),
    )
    changed = (
        convert_cell(RawCell("00104"), "integer"),
        convert_cell(RawCell("001.25"), "number"),
        convert_cell(RawCell("4471820993345529871"), "integer"),
        convert_cell(RawCell("4471820993345529871"), "number"),
        convert_cell(RawCell("0.1234567890123456789"), "number"),
        convert_cell(RawCell("03/04/2026 12:30"), "date"),
        convert_cell(RawCell("03/04/2026"), "datetime"),
    )

    assert [result.outcome for result in unchanged] == ["unchanged"] * 4
    assert [result.outcome for result in changed] == ["changed"] * 7
    assert changed[-2].value == "2026-03-04"
    assert changed[-1].value == "2026-03-04T00:00:00"


def test_stats_use_semantic_changes_instead_of_display_normalization() -> None:
    columns = (
        SourceColumn(0, "amount", "amount", "original"),
        SourceColumn(1, "active", "active", "original"),
        SourceColumn(2, "day", "day", "original"),
        SourceColumn(3, "moment", "moment", "original"),
    )
    stats = ColumnStatsAccumulator(
        columns,
        decimal_separator=",",
        thousands_separator=".",
    )
    stats.accept(_row(1, "1.250,00", "Y", "03/04/2026", "03/04/2026 12:30"))

    by_column = stats.snapshot(complete=True).columns
    assert by_column[0].counts_by_type["number"].changed == 0
    assert by_column[1].counts_by_type["boolean"].changed == 0
    assert by_column[2].counts_by_type["date"].changed == 0
    assert by_column[2].counts_by_type["datetime"].changed == 1
    assert by_column[3].counts_by_type["date"].changed == 1
    assert by_column[3].counts_by_type["datetime"].changed == 0


def test_conversion_preserves_datetime_precision_and_rejects_storage_overflow() -> None:
    moment = convert_cell(RawCell("2026-10-18T12:30:45.123456+02:00"), "datetime")

    assert moment.value == "2026-10-18T12:30:45.123456+02:00"
    assert convert_cell(RawCell("1e999"), "number").outcome == "invalid"
    assert convert_cell(RawCell(str(2**63)), "integer").outcome == "invalid"
    assert convert_cell(RawCell(str(-(2**63) - 1)), "integer").outcome == "invalid"
    assert convert_cell(RawCell("1,2,3"), "number").outcome == "invalid"
    assert convert_cell(RawCell("1,234,567.89"), "number").value == 1_234_567.89


def test_preview_and_stats_stay_bounded_and_mark_provisional_counts() -> None:
    columns = (
        SourceColumn(0, "id", "id", "original"),
        SourceColumn(1, "amount", "amount", "original"),
    )
    preview = PreviewAccumulator(head_rows=2, tail_rows=2, column_limit=1)
    stats = ColumnStatsAccumulator(columns, inference_limit=2, preview_values=2)
    rows = [
        _row(1, "001", "1.2"),
        _row(2, "002", "2.3"),
        _row(3, "003", "bad"),
        _row(4, "004", "4.5"),
        _row(5, "005", "5.6"),
    ]
    for row in rows:
        preview.accept(row)
        stats.accept(row)

    provisional = preview.snapshot(complete=False)
    assert provisional.tail == ()
    assert provisional.tail_pending is True
    assert provisional.omitted_rows is None

    finished = preview.snapshot(complete=True)
    assert [row.record_ordinal for row in finished.head] == [1, 2]
    assert [row.record_ordinal for row in finished.tail] == [4, 5]
    assert finished.omitted_rows == 1
    assert finished.head[0].cells == ("001",)
    assert finished.head[0].hidden_cell_count == 1

    snapshot = stats.snapshot(complete=False)
    assert snapshot.rows_checked == 5
    assert snapshot.inference_rows_checked == 2
    assert snapshot.complete is False
    assert snapshot.columns[0].suggested_type == "text"
    assert "leading_zero" in snapshot.columns[0].needs_review
    assert snapshot.columns[1].suggested_type == "number"
    assert snapshot.columns[1].counts_by_type["number"].invalid == 1
    assert asdict(snapshot)["rows_checked"] == 5


def test_stats_count_every_row_but_bound_inference_to_first_100k() -> None:
    columns = (SourceColumn(0, "value", "value", "original"),)
    stats = ColumnStatsAccumulator(columns, inference_limit=100_000, preview_values=5)

    for ordinal in range(1, 100_006):
        stats.accept(_row(ordinal, str(ordinal)))

    snapshot = stats.snapshot(complete=True)
    column = snapshot.columns[0]
    assert snapshot.rows_checked == 100_005
    assert snapshot.inference_rows_checked == 100_000
    assert tuple(sample.original for sample in column.sample_values) == (
        "1",
        "2",
        "3",
        "4",
        "5",
    )
    assert column.counts_by_type["integer"].rows_checked == 100_005
    assert column.suggested_type == "integer"


def test_large_samples_are_bounded_after_exact_outcomes_are_computed() -> None:
    columns = (SourceColumn(0, "value", "value", "original"),)
    stats = ColumnStatsAccumulator(columns)
    value = "yes" + " " * (PREVIEW_TEXT_CHARS - 3) + "x"

    stats.accept(_row(1, value))

    column = stats.snapshot(complete=True).columns[0]
    sample = column.sample_values[0]
    assert sample.original == value[:PREVIEW_TEXT_CHARS]
    assert sample.original_length == len(value)
    assert sample.truncated is True
    assert sample.outcomes_by_type["boolean"] == "invalid"
    assert column.counts_by_type["boolean"].invalid == 1
    assert convert_cell(RawCell(sample.original), "boolean").outcome == "unchanged"


def test_row_preview_bounds_cells_and_issue_originals() -> None:
    value = "x" * (PREVIEW_TEXT_CHARS + 17)
    cells = (RawCell(value), RawCell(value))
    source = RawRow(1, 1, 1, cells)
    row = TableRow(
        role="data",
        source=source,
        cells=cells,
        issues=(RowIssue("extra_values", "extra value", (value,)),),
    )
    preview = PreviewAccumulator(head_rows=1, column_limit=2)

    preview.accept(row)

    result = preview.snapshot(complete=True).head[0]
    excerpt = "x" * (PREVIEW_TEXT_CHARS - 1) + "…"
    assert result.cells == (excerpt,) * 2
    assert result.issues[0].original_values == (excerpt,)


def test_mixed_naive_and_aware_datetimes_stay_text_for_review() -> None:
    columns = (SourceColumn(0, "when", "when", "original"),)
    stats = ColumnStatsAccumulator(columns)
    stats.accept(_row(1, "2026-10-18T12:30:45.123456"))
    stats.accept(_row(2, "2026-10-18T12:30:45+02:00"))

    column = stats.snapshot(complete=True).columns[0]
    assert column.suggested_type == "text"
    assert "mixed_datetime_awareness" in column.needs_review


def test_mixed_slash_naive_and_iso_aware_datetimes_stay_text_for_review() -> None:
    columns = (SourceColumn(0, "when", "when", "original"),)
    stats = ColumnStatsAccumulator(columns)
    stats.accept(_row(1, "03/04/2026 12:30"))
    stats.accept(_row(2, "2026-03-04T12:30:00+02:00"))

    column = stats.snapshot(complete=True).columns[0]
    assert column.suggested_type == "text"
    assert "mixed_datetime_awareness" in column.needs_review


def test_date_only_values_infer_date_instead_of_datetime() -> None:
    columns = (SourceColumn(0, "when", "when", "original"),)
    stats = ColumnStatsAccumulator(columns)
    stats.accept(_row(1, "2026-10-18"))
    stats.accept(_row(2, "2026-10-19"))

    column = stats.snapshot(complete=True).columns[0]
    assert column.suggested_type == "date"


def test_clear_day_first_values_infer_order_and_use_its_counts() -> None:
    columns = (SourceColumn(0, "when", "when", "original"),)
    stats = ColumnStatsAccumulator(columns)
    stats.accept(_row(1, "18/10/2026"))
    stats.accept(_row(2, "19/10/2026"))

    column = stats.snapshot(complete=True).columns[0]
    assert column.suggested_type == "date"
    assert column.suggested_date_order == "day_first"
    assert column.ambiguous_date_order is False
    assert column.counts_by_type["date"].invalid == 0
    assert column.counts_by_date_order["month_first"]["date"].invalid == 2
    assert column.counts_by_date_order["day_first"]["date"].valid == 2


def test_ambiguous_slash_datetime_parses_under_both_orders() -> None:
    columns = (SourceColumn(0, "when", "when", "original"),)
    stats = ColumnStatsAccumulator(columns)
    stats.accept(_row(1, "03/04/2026 12:30:45.123456"))

    column = stats.snapshot(complete=True).columns[0]
    assert column.suggested_type == "datetime"
    assert column.suggested_date_order == "month_first"
    assert column.ambiguous_date_order is True
    assert column.counts_by_date_order["month_first"]["datetime"].valid == 1
    assert column.counts_by_date_order["day_first"]["datetime"].valid == 1
    converted = convert_cell(
        RawCell("18/10/2026 12:30:45.123456"),
        "datetime",
        date_order="day_first",
    )
    assert converted.value == "2026-10-18T12:30:45.123456"

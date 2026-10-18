from __future__ import annotations

from dataclasses import asdict

from frisket.table_import import (
    ColumnStatsAccumulator,
    PreviewAccumulator,
    RawCell,
    RawRow,
    SourceColumn,
    TableRow,
    convert_cell,
)


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
        "changed",
    )
    assert invalid.value is None
    assert invalid.outcome == "invalid"
    assert invalid.issue is not None
    assert (moment.value, moment.display) == (
        "2026-10-18T12:30",
        "2026-10-18 12:30",
    )


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

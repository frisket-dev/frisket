from __future__ import annotations

from pathlib import Path

import pytest

from frisket.table_import import (
    TableReadSettings,
    TableRecordCountRequired,
    count_records,
    inspect_source,
    open_table,
    read_prefix,
)


def _settings(**changes: object) -> TableReadSettings:
    values: dict[str, object] = {
        "kind": "delimited",
        "delimiter": ",",
        "encoding": "utf-8",
    }
    values.update(changes)
    return TableReadSettings(**values)  # type: ignore[arg-type]


def test_delimited_reader_keeps_multiline_text_and_repairs_row_widths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rows.csv"
    source.write_text(
        'name,,name\n"Ada\nLovelace",first,extra,tail\nGrace,second\n\n',
        encoding="utf-8",
    )

    assert count_records(source, _settings()) == 4
    with open_table(source, _settings(), total_record_count=4) as reader:
        assert [
            (column.index, column.name, column.name_status) for column in reader.columns
        ] == [
            (0, "name", "original"),
            (1, "column 2", "missing"),
            (2, "name (2)", "duplicate"),
        ]
        rows = list(reader)

    assert rows[0].role == "header"
    assert rows[1].source.physical_line_start == 2
    assert rows[1].source.physical_line_end == 3
    assert rows[1].cells[0].text == "Ada\nLovelace"
    assert [cell.text for cell in rows[1].extra_values] == ["tail"]
    assert rows[1].issues[0].code == "extra_values"
    assert [cell.text for cell in rows[2].cells] == ["Grace", "second", ""]
    assert rows[2].issues[0].code == "missing_values"
    assert rows[3].role == "blank"


def test_skip_footer_requires_a_count_and_uses_record_ordinals(tmp_path: Path) -> None:
    source = tmp_path / "rows.csv"
    source.write_text("title\nname,value\nAda,1\nTotal,1\nnote,end\n", encoding="utf-8")
    settings = _settings(skip_rows_start=1, skip_rows_end=2)

    with pytest.raises(TableRecordCountRequired):
        open_table(source, settings)

    with open_table(source, settings, total_record_count=5) as reader:
        rows = list(reader)

    assert [row.role for row in rows] == [
        "skipped_start",
        "header",
        "data",
        "skipped_end",
        "skipped_end",
    ]

    prefix = read_prefix(source, settings, record_limit=3)
    assert prefix.tail_pending is True
    assert [row.role for row in prefix.rows] == [
        "skipped_start",
        "header",
        "data",
    ]


def test_content_inspection_detects_pipe_and_utf16(tmp_path: Path) -> None:
    source = tmp_path / "misleading.csv"
    source.write_bytes("name|amount\nAda|1,25\n".encode("utf-16"))

    info = inspect_source(source)

    assert info.kind == "delimited"
    assert info.detected_encoding == "utf-16"
    assert info.detected_delimiter == "|"
    assert info.detected_decimal_separator == ","


def test_content_inspection_recognizes_utf32_bom_before_utf16(tmp_path: Path) -> None:
    source = tmp_path / "utf32.csv"
    source.write_bytes("name,value\nAda,1\n".encode("utf-32"))

    info = inspect_source(source)

    assert info.kind == "delimited"
    assert info.detected_encoding == "utf-32"
    assert info.detected_delimiter == ","


def test_unclosed_quoted_record_fails_closed_without_losing_valid_multiline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformed.csv"
    source.write_text('name,value\n"Ada\nLovelace,1\n', encoding="utf-8")

    with pytest.raises(ValueError, match="could not be parsed"):
        count_records(source, _settings())


def test_logical_record_count_is_exact_beyond_inference_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.csv"
    with source.open("w", encoding="utf-8", newline="") as output:
        output.write("value\n")
        for value in range(100_005):
            output.write(f"{value}\n")

    assert count_records(source, _settings()) == 100_006


def test_wrong_utf8_keeps_rows_and_counts_each_replacement(tmp_path: Path) -> None:
    source = tmp_path / "wrong-encoding.csv"
    source.write_bytes(b"name,value\nAda,Jos\xe9\xff\n")
    settings = _settings(encoding="utf-8")

    assert count_records(source, settings) == 2
    with open_table(source, settings, total_record_count=2) as reader:
        rows = list(reader)

    assert rows[1].cells[1].text == "Jos\ufffd\ufffd"
    assert len(rows[1].issues) == 1
    assert rows[1].issues[0].code == "unreadable_cell"
    assert rows[1].issues[0].count == 2


def test_literal_replacement_character_is_not_reported_as_decode_damage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "literal-replacement.csv"
    source.write_text("name\nJos\ufffd\n", encoding="utf-8")

    with open_table(source, _settings(), total_record_count=2) as reader:
        rows = list(reader)

    assert rows[1].cells[0].text == "Jos\ufffd"
    assert rows[1].issues == ()


def test_auto_utf8_reports_invalid_bytes_beyond_inspection_prefix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "late-invalid.csv"
    source.write_bytes(b"name\n" + (b"ASCII\n" * 12_000) + b"Jos\xe9\n")

    info = inspect_source(source)
    assert info.detected_encoding == "utf-8"
    settings = _settings(encoding=info.detected_encoding)
    with open_table(
        source,
        settings,
        total_record_count=count_records(source, settings),
    ) as reader:
        issue_counts = [
            issue.count
            for row in reader
            for issue in row.issues
            if issue.code == "unreadable_cell"
        ]

    assert issue_counts == [1]


def test_large_skip_retains_only_bounded_boundary_rows(tmp_path: Path) -> None:
    source = tmp_path / "large-skip.csv"
    with source.open("w", encoding="utf-8", newline="") as output:
        for value in range(100_000):
            output.write(f"ignored,{value}\n")
        output.write("name,value\nAda,1\n")
    settings = _settings(skip_rows_start=100_000)

    with open_table(source, settings, total_record_count=100_002) as reader:
        rows = list(reader)

    assert len(rows) == 18
    assert rows[0].role == "skipped_start"
    assert rows[0].source.record_ordinal == 99_985
    assert rows[-2].role == "header"
    assert rows[-1].cells[0].text == "Ada"

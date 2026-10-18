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

from __future__ import annotations

from dataclasses import asdict

import pytest

from frisket.table_import import RawCell, TableReadSettings


def test_public_value_objects_are_plain_dataclasses() -> None:
    settings = TableReadSettings(
        kind="delimited",
        delimiter=",",
        encoding="utf-8",
    )

    assert asdict(settings)["first_row_is_header"] is True
    assert asdict(RawCell("00418823")) == {
        "text": "00418823",
        "kind": "text",
    }


def test_settings_keep_text_and_workbook_controls_disjoint() -> None:
    with pytest.raises(ValueError, match="worksheet"):
        TableReadSettings(
            kind="delimited",
            delimiter=",",
            encoding="utf-8",
            worksheet="Sheet1",
        )
    with pytest.raises(ValueError, match="delimiter"):
        TableReadSettings(kind="xlsx", delimiter=",")

from typing import Any

import pytest
from pydantic import ValidationError

from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    SheetColumnRef,
    SheetRef,
    discover_references,
)


@pytest.mark.parametrize("sheet_id", [0, -1, True, "1", 1.5])
def test_sheet_reference_requires_positive_integer_identity(sheet_id):
    with pytest.raises(ValidationError):
        SheetRef(sheet_id=sheet_id)


def test_sheet_reference_does_not_hide_another_row_scope():
    with pytest.raises(ValidationError):
        SheetRef(sheet_id=2, row_ids=[3])
    assert SheetRef(sheet_id=2).model_dump() == {"sheet_id": 2}


@pytest.mark.parametrize("column", ["", " ", " name", "name ", 3, None])
def test_qualified_column_requires_an_exact_name(column):
    with pytest.raises(ValidationError):
        SheetColumnRef(sheet_id=2, column=column)


def test_other_sheet_column_is_not_mistaken_for_primary_row_input():
    class Params(ActionParams):
        source: ColumnRef[Any]
        target: SheetColumnRef

    params = Params(source="name", target={"sheet_id": 2, "column": "official"})
    assert [ref.column for ref in discover_references(params)] == ["name"]
    assert params.target.model_dump() == {"sheet_id": 2, "column": "official"}

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.extract import ColumnsFromJsonParams
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import SheetRows


_VALID_PARAMS: dict[str, dict[str, object]] = {
    "map.clean_column": {"source": "source"},
    "map.clean_dates": {"source": "source"},
    "map.columns_from_json": {
        "source_column": "source",
        "routes": [{"name": "title", "path": "record.title"}],
    },
    "map.regex_extract": {
        "input_columns": ["source"],
        "pattern": r"\w+",
    },
    "map.template": {"template": {"text": "{{source}}"}},
    "map.to_geo_point": {
        "latitude_column": "latitude",
        "longitude_column": "longitude",
    },
}


@pytest.mark.parametrize("action_id", sorted(_VALID_PARAMS))
def test_registered_action_params_reject_unknown_fields(action_id: str) -> None:
    params_model = ACTION_REGISTRY.get(action_id).definition.run.params_model

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        params_model.model_validate({**_VALID_PARAMS[action_id], "unexpected": True})

    assert params_model.model_json_schema()["additionalProperties"] is False


def test_nested_action_params_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ColumnsFromJsonParams.model_validate(
            {
                "source_column": "source",
                "routes": [
                    {
                        "name": "title",
                        "path": "record.title",
                        "unexpected": True,
                    }
                ],
            }
        )


def test_sheet_rows_rejects_an_explicit_empty_selection() -> None:
    with pytest.raises(ValidationError, match="row ids must not be empty"):
        SheetRows(sheet_id=1, row_ids=())


@pytest.mark.parametrize("row_ids", [(1, 1), (0, 1), (-1, 1)])
def test_sheet_rows_rejects_nonpositive_or_duplicate_rows(
    row_ids: tuple[int, ...],
) -> None:
    with pytest.raises(ValidationError, match="row ids must be positive and unique"):
        SheetRows(sheet_id=1, row_ids=row_ids)


def test_sheet_rows_canonicalizes_selected_rows() -> None:
    assert SheetRows(sheet_id=1, row_ids=(9, 2, 5)).row_ids == (2, 5, 9)


def test_generated_action_params_cover_the_registry() -> None:
    from scripts.ci.gen_http_contracts import render_action_types

    rendered = render_action_types()
    for registered in ACTION_REGISTRY.actions:
        assert f"{registered.action_id!r}:" in rendered
        assert registered.definition.run.params_model.__name__ in rendered


@pytest.mark.parametrize(
    "action_id", ["source.create", "source.update", "source.delete"]
)
def test_source_catalog_output_schema_uses_emitted_source_kind(action_id: str) -> None:
    properties = ACTION_REGISTRY.get(action_id).catalog_entry()["output_schema"][
        "properties"
    ]

    assert "source_kind" in properties
    assert "kind" not in properties

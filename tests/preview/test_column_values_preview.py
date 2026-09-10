"""Read-only column distinct-values preview contract.

Pins the enumeration surface for resolve.substitute / resolve.combine:
frequency-sorted distinct values with counts, case-insensitive substring
search, limit/offset paging with a clamped ceiling, unfiltered column facts
(total_rows / distinct / missing), and a full-column ``value_hash`` identical
to cluster_preview.source_value_hash — the staleness anchor the commits
validate as ``expected_value_hash``.
"""

from __future__ import annotations

import pytest

from frisket.preview.cluster import source_value_hash
from frisket.preview.column_values import (
    COLUMN_VALUES_DEFAULT_LIMIT,
    COLUMN_VALUES_MAX_LIMIT,
    COLUMN_VALUES_PREVIEW_SCHEMA_VERSION,
    ColumnValuesPreviewError,
    resolve_column_values_preview,
)
from frisket.engine.store import Project


def _seed(tmp_path, values, name="org"):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {name: p.add_column(sheet, name)}
    p.add_rows(sheet, [{name: v} for v in values], cols)
    return p, sheet


ROWS = [
    "ACME Corp",
    "ACME Corp",
    "acme corp",
    "Banana Farms",
    "Banana Farms",
    "Banana Farms",
    "Cactus Ltd",
    None,
    "   ",
]


def test_values_sorted_count_desc_then_value_asc(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    payload = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    assert payload["schema_version"] == COLUMN_VALUES_PREVIEW_SCHEMA_VERSION
    assert payload["values"] == [
        {"value": "Banana Farms", "count": 3},
        {"value": "ACME Corp", "count": 2},
        {"value": "Cactus Ltd", "count": 1},
        {"value": "acme corp", "count": 1},
    ]
    assert payload["total_rows"] == 9
    assert payload["distinct"] == 4
    assert payload["missing"] == 2
    assert payload["truncated"] is False
    assert payload["search"] is None
    p.close()


def test_blank_and_null_cells_excluded_from_values_counted_in_missing(tmp_path):
    p, sid = _seed(tmp_path, ["x", None, "", "   ", "\t", "x"])
    payload = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    assert payload["values"] == [{"value": "x", "count": 2}]
    assert payload["distinct"] == 1
    assert payload["missing"] == 4
    assert payload["total_rows"] == 6
    p.close()


def test_search_is_case_insensitive_substring_and_facts_stay_unfiltered(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    payload = resolve_column_values_preview(
        p, sheet_id=sid, input_column="org", search="ACME"
    )
    assert payload["values"] == [
        {"value": "ACME Corp", "count": 2},
        {"value": "acme corp", "count": 1},
    ]
    assert payload["search"] == "ACME"
    # unfiltered column facts survive the filter
    assert payload["total_rows"] == 9
    assert payload["distinct"] == 4
    assert payload["missing"] == 2
    assert payload["truncated"] is False
    p.close()


def test_search_filters_value_hash_still_full_column(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    unfiltered = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    filtered = resolve_column_values_preview(
        p, sheet_id=sid, input_column="org", search="banana"
    )
    assert filtered["values"] == [{"value": "Banana Farms", "count": 3}]
    assert filtered["value_hash"] == unfiltered["value_hash"]
    p.close()


def test_limit_offset_paging_and_truncated_flag(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    page1 = resolve_column_values_preview(
        p, sheet_id=sid, input_column="org", limit=2, offset=0
    )
    assert [v["value"] for v in page1["values"]] == ["Banana Farms", "ACME Corp"]
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert page1["truncated"] is True

    page2 = resolve_column_values_preview(
        p, sheet_id=sid, input_column="org", limit=2, offset=2
    )
    assert [v["value"] for v in page2["values"]] == ["Cactus Ltd", "acme corp"]
    assert page2["offset"] == 2
    assert page2["truncated"] is False

    beyond = resolve_column_values_preview(
        p, sheet_id=sid, input_column="org", limit=2, offset=10
    )
    assert beyond["values"] == []
    assert beyond["truncated"] is False
    p.close()


def test_limit_defaults_to_500_and_clamps_to_2000(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    default = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    assert default["limit"] == COLUMN_VALUES_DEFAULT_LIMIT == 500
    clamped = resolve_column_values_preview(
        p, sheet_id=sid, input_column="org", limit=999_999
    )
    assert clamped["limit"] == COLUMN_VALUES_MAX_LIMIT == 2000
    # values still returned; the clamp is a ceiling, not an error
    assert clamped["values"]
    p.close()


def test_value_hash_matches_source_value_hash_and_changes_on_edit(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    col_id = next(int(c["id"]) for c in p.columns(sid) if c["name"] == "org")
    payload = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    row_ids = [int(r) for r in p.visible_row_ids(sid)]
    assert payload["value_hash"] == source_value_hash(p, sid, col_id, row_ids)
    assert payload["value_hash"].startswith("sha256:")

    again = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    assert again["value_hash"] == payload["value_hash"]

    p.add_rows(sid, [{"org": "Zebra Co"}], {"org": col_id})
    edited = resolve_column_values_preview(p, sheet_id=sid, input_column="org")
    assert edited["value_hash"] != payload["value_hash"]
    p.close()


def test_missing_sheet_and_column_are_invalid_input_ref(tmp_path):
    p, sid = _seed(tmp_path, ROWS)
    with pytest.raises(ColumnValuesPreviewError) as exc:
        resolve_column_values_preview(p, sheet_id=sid + 99, input_column="org")
    assert exc.value.code == "invalid_input_ref"
    assert exc.value.field == "sheet_id"

    with pytest.raises(ColumnValuesPreviewError) as exc:
        resolve_column_values_preview(p, sheet_id=sid, input_column="nope")
    assert exc.value.code == "invalid_input_ref"
    assert exc.value.field == "input_column"

    with pytest.raises(ColumnValuesPreviewError) as exc:
        resolve_column_values_preview(p, sheet_id="1", input_column="org")
    assert exc.value.code == "invalid_input_ref"
    assert exc.value.field == "sheet_id"

    with pytest.raises(ColumnValuesPreviewError) as exc:
        resolve_column_values_preview(p, sheet_id=sid, input_column="   ")
    assert exc.value.code == "invalid_input_ref"
    assert exc.value.field == "input_column"
    p.close()


@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"limit": "10"}, "limit"),
        ({"limit": True}, "limit"),
        ({"limit": 0}, "limit"),
        ({"limit": -5}, "limit"),
        ({"offset": "0"}, "offset"),
        ({"offset": True}, "offset"),
        ({"offset": -1}, "offset"),
        ({"search": 5}, "search"),
    ],
)
def test_bad_paging_and_search_params_are_invalid_params(tmp_path, kwargs, field):
    p, sid = _seed(tmp_path, ROWS)
    with pytest.raises(ColumnValuesPreviewError) as exc:
        resolve_column_values_preview(p, sheet_id=sid, input_column="org", **kwargs)
    assert exc.value.code == "invalid_params"
    assert exc.value.field == field
    p.close()

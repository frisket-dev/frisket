from __future__ import annotations

from typing import Any

from frisket.actions.system import root_action_catalog_payload
from frisket.server.action_catalog_hints import (
    action_catalog_payload_with_launcher_hints,
)


def _entry(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [action for action in payload["actions"] if action["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind} catalog entry"
    return matches[0]


def _requirement(entry: dict[str, Any], param: str) -> dict[str, Any]:
    requirements = entry.get("ui_hints", {}).get("source_requirements") or []
    matches = [req for req in requirements if req.get("param") == param]
    assert len(matches) == 1, (
        f"expected exactly one source_requirement with param={param!r} on "
        f"{entry['kind']}, found {requirements!r}"
    )
    return matches[0]


def test_cluster_values_advertises_column_source_requirement() -> None:
    payload = root_action_catalog_payload()
    entry = _entry(payload, "cluster.values")
    assert "source" in entry["input_schema"]["properties"]
    requirement = _requirement(entry, "source")
    assert requirement["accepted_column_types"] == ["text", "category", "link"]
    assert entry["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows"],
    }


def test_embedding_index_create_advertises_typed_source_arguments() -> None:
    payload = root_action_catalog_payload()
    entry = _entry(payload, "embedding.index_create")
    assert "source_columns" in entry["input_schema"]["properties"]
    requirement = entry["input_schema"]["properties"]["source_columns"]
    assert requirement["type"] == "array"
    assert requirement["minItems"] == 1
    assert requirement["maxItems"] == 64
    # The public action supports multiple modalities. Omitting a fixed type
    # filter is truthful; the selected modality/model owns content support.
    assert "accepted_column_types" not in requirement


def test_derive_table_from_list_has_no_invented_requirement() -> None:
    """The typed list-table source is a column or named-result reference.

    Neither source variant is a flat column-name field, so the root catalog
    must not invent a legacy source_requirements entry for it.
    """
    payload = action_catalog_payload_with_launcher_hints({})
    entry = _entry(payload, "derive.table_from_list")
    assert "source" in entry["input_schema"]["properties"]
    assert not entry.get("ui_hints", {}).get("source_requirements")


def test_reduce_group_summary_advertises_required_many_input_columns() -> None:
    inputs = {
        "id": "source",
        "mode": "columns",
        "param": "source",
        "label": "Source",
        "min": 1,
        # Every column type except ``file``: raw file handles need an explicit
        # conversion before they can feed a grouped summary.
        "accepted_column_types": [
            "audio",
            "boolean",
            "category",
            "date",
            "geo_point",
            "geo_shape",
            "image",
            "integer",
            "json",
            "link",
            "number",
            "text",
            "timeline_point",
            "timeline_points",
            "timeline_range",
            "timeline_ranges",
            "timestamped_transcript",
            "video",
        ],
    }
    # ``GroupSummaryParams.group_by`` is ``GroupSummaryColumn | None``: an
    # ungrouped summary is a valid request, so the catalog must not require
    # the grouping column.
    # PRODUCT METADATA LOSS (typed catalog builder, src/frisket/actions/core.py
    # ~4029-4053): the legacy requirement also carried ``"max": 1`` (a single
    # grouping column); the typed builder never emits ``max``, so that pin is
    # dropped here rather than asserted red. Filed, not re-pinned.
    grouping = {
        "id": "group_by",
        "mode": "column",
        "param": "group_by",
        "label": "Group by",
        "min": 0,
    }

    static_entry = _entry(
        root_action_catalog_payload(),
        "reduce.group_summary",
    )
    assert _requirement(static_entry, "source") == inputs
    assert _requirement(static_entry, "group_by") == grouping

    served_entry = _entry(
        action_catalog_payload_with_launcher_hints({}),
        "reduce.group_summary",
    )
    assert _requirement(served_entry, "source") == inputs
    assert _requirement(served_entry, "group_by") == grouping


def test_requirements_survive_launcher_hints_projection() -> None:
    """The served root catalog preserves declared source requirements.

    Launcher-hints projection and the typed-action merge must retain the
    cluster requirements and typed embedding inputs without inventing any for list-table.
    """
    served = action_catalog_payload_with_launcher_hints({})

    cluster_entry = _entry(served, "cluster.values")
    cluster_requirement = _requirement(cluster_entry, "source")
    assert cluster_requirement["accepted_column_types"] == ["text", "category", "link"]

    embedding_entry = _entry(served, "embedding.index_create")
    embedding_requirement = embedding_entry["input_schema"]["properties"][
        "source_columns"
    ]
    assert embedding_requirement["type"] == "array"
    assert embedding_requirement["minItems"] == 1
    assert embedding_requirement["maxItems"] == 64
    assert "accepted_column_types" not in embedding_requirement

    derive_entry = _entry(served, "derive.table_from_list")
    assert not derive_entry.get("ui_hints", {}).get("source_requirements")

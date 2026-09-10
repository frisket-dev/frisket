import pytest

from frisket.actions.cluster import CLUSTER_VALUES
from frisket.actions.core import ActionNamespace, ActionRegistry
from frisket.actions.types import ActionRequest


def registered_cluster():
    return ActionRegistry([ActionNamespace("custom", actions=[CLUSTER_VALUES])]).get(
        "custom.values"
    )


def request(**changes):
    values = dict(
        action_id="custom.values",
        scope={"kind": "sheet_rows", "sheet_id": 1},
        params={"source": "name"},
        output_names={"canonical": "Reviewed name"},
        idempotency_key="cluster-declaration",
    )
    values.update(changes)
    return ActionRequest(**values)


def test_cluster_exposes_one_named_output_and_whole_column_scope():
    registered = registered_cluster()
    params, fields = registered.bind_request(request())
    assert params.source.name == "name"
    assert [(field.key, field.column_type) for field in fields] == [
        ("canonical", "text")
    ]
    entry = registered.catalog_entry()
    assert entry["ui_hints"]["form"] == "generated"
    assert entry["execution_mode"] == "batch_deduped"
    assert entry["row_scope_policy"] == {
        "kind": "sheet_rows",
        "selectors": ["all_rows"],
    }


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": [1]}}, "all rows"),
        ({"scope": {"kind": "project"}}, "scope"),
        ({"sheet_name": "Other"}, "sheet_name"),
        ({"output_names": {"wrong": "Other"}}, "unknown"),
    ],
)
def test_cluster_refuses_incompatible_envelopes(changes, message):
    with pytest.raises(ValueError, match=message):
        registered_cluster().bind_request(request(**changes))

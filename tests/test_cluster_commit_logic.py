"""Pure commit-logic: group edits (canonical overrides + member exclusions) and
the canonical-column mapping that guarantees receipt canonicals == the written
{input_column}_canonical column.
"""

from __future__ import annotations

import pytest

from frisket.preview.cluster import (
    ClusterPreviewError,
    apply_group_edits,
    canonical_column_values,
)


def _cluster(key, canonical, values):
    return {
        "key": key,
        "canonical": canonical,
        "size": sum(c for _, c in values),
        "values": [{"value": v, "count": c} for v, c in values],
        "row_ids": [],
    }


CLUSTERS = [
    _cluster(
        "acme",
        "ACME Corp",
        [("ACME Corp", 3), ("Acme Corporation", 1), ("ACME Inc.", 1)],
    ),
    _cluster("banana", "Banana Farms", [("Banana Farms", 2), ("Banana Farms LLC", 1)]),
]


def test_override_replaces_canonical():
    out = apply_group_edits(CLUSTERS, canonical_overrides={"acme": "ACME Corporation"})
    acme = next(c for c in out if c["key"] == "acme")
    assert acme["canonical"] == "ACME Corporation"


def test_exclusion_removes_member_and_recomputes_canonical():
    # exclude the most-frequent surface; canonical recomputes over survivors
    out = apply_group_edits(CLUSTERS, excluded_members={"acme": ["ACME Corp"]})
    acme = next(c for c in out if c["key"] == "acme")
    assert {v["value"] for v in acme["values"]} == {"Acme Corporation", "ACME Inc."}
    # both survivors have count 1 -> shortest wins ("ACME Inc." over
    # "Acme Corporation"), matching cluster_fingerprint.canonical's
    # most-frequent -> shortest tie-break.
    assert acme["canonical"] == "ACME Inc."


def test_exclusion_down_to_singleton_drops_group():
    out = apply_group_edits(CLUSTERS, excluded_members={"banana": ["Banana Farms LLC"]})
    assert all(c["key"] != "banana" for c in out)


def test_min_size_floor_applies_after_exclusion_at_the_threshold():
    # acme has 3 forms; excluding one leaves exactly 2 surviving forms.
    excl = {"acme": ["ACME Corp"]}
    # min_size=2 -> 2 survivors meet the floor -> kept
    kept = apply_group_edits(CLUSTERS, min_size=2, excluded_members=excl)
    assert any(c["key"] == "acme" for c in kept)
    # min_size=3 -> 2 survivors are BELOW the run's floor -> dropped (an
    # exclusion must not smuggle a below-min_size group past the threshold the
    # initial clustering enforced).
    dropped = apply_group_edits(CLUSTERS, min_size=3, excluded_members=excl)
    assert all(c["key"] != "acme" for c in dropped)


def test_unknown_key_or_value_rejected_loudly():
    with pytest.raises(ClusterPreviewError) as bad_key:
        apply_group_edits(CLUSTERS, canonical_overrides={"nope": "X"})
    assert bad_key.value.code == "invalid_params"
    with pytest.raises(ClusterPreviewError) as bad_val:
        apply_group_edits(CLUSTERS, excluded_members={"acme": ["Nonexistent"]})
    assert bad_val.value.field == "excluded_members"


def test_canonical_column_maps_members_and_preserves_others():
    # rows: 1,2,3 = ACME Corp; 4 = Acme Corporation; 5 = ACME Inc.;
    #       6,7 = Banana Farms; 8 = Banana Farms LLC; 9 = Unrelated
    source = {
        1: "ACME Corp",
        2: "ACME Corp",
        3: "ACME Corp",
        4: "Acme Corporation",
        5: "ACME Inc.",
        6: "Banana Farms",
        7: "Banana Farms",
        8: "Banana Farms LLC",
        9: "Unrelated Co",
    }
    row_ids = list(range(1, 10))
    adjusted = apply_group_edits(CLUSTERS)
    column = canonical_column_values(adjusted, source_values=source, row_ids=row_ids)
    # every ACME surface -> "ACME Corp"
    assert column[4] == "ACME Corp" and column[5] == "ACME Corp"
    # banana surfaces -> "Banana Farms"
    assert column[8] == "Banana Farms"
    # un-clustered row keeps its own value
    assert column[9] == "Unrelated Co"
    # full coverage: every visible row has a value
    assert set(column) == set(row_ids)


def test_excluded_member_reverts_to_own_value_in_column():
    source = {1: "ACME Corp", 2: "Acme Corporation", 3: "ACME Inc."}
    row_ids = [1, 2, 3]
    adjusted = apply_group_edits(CLUSTERS, excluded_members={"acme": ["ACME Inc."]})
    column = canonical_column_values(adjusted, source_values=source, row_ids=row_ids)
    # ACME Inc. was excluded -> keeps its own value, not the canonical
    assert column[3] == "ACME Inc."
    assert column[1] == column[2]  # the two survivors share the canonical

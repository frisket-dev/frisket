"""Reviewed cluster computation reuses preview rules over the captured source."""

from frisket.actions.cluster_types import ClusterOptions, ClusterReview
from frisket.engine.executor.value_cluster import compute_reviewed_clusters


def test_reviewed_groups_preserve_exact_member_coverage_and_normalization():
    values = {
        1: "Jon Smith",
        2: "Smith, Jon",
        3: "Jon Smith",
        4: " Acme ",
        5: None,
        6: "",
        7: "   ",
        8: "SMITH JON",
    }
    original = dict(values)
    initial = compute_reviewed_clusters(
        None, sheet_id=1, column="name", source_values=values, options=ClusterOptions()
    )
    [group] = initial.clusters
    reviewed = compute_reviewed_clusters(
        None,
        sheet_id=1,
        column="name",
        source_values=values,
        options=ClusterOptions(
            review=ClusterReview(
                canonical_overrides={group["key"]: "Jonathan"},
                excluded_members={group["key"]: ["Smith, Jon"]},
            )
        ),
    )
    assert reviewed.values == {
        1: "Jonathan",
        2: "Smith, Jon",
        3: "Jonathan",
        4: "Acme",
        5: "",
        6: "",
        7: "",
        8: "Jonathan",
    }
    assert reviewed.clusters[0]["row_ids"] == [1, 3, 8]
    assert reviewed.clusters[0]["values"] == [
        {"value": "Jon Smith", "count": 2},
        {"value": "SMITH JON", "count": 1},
    ]
    assert reviewed.clusters[0]["size"] == 3
    assert values == original


def test_no_groups_still_returns_every_admitted_row():
    result = compute_reviewed_clusters(
        None,
        sheet_id=1,
        column="name",
        source_values={9: "Alpha", 12: "Beta"},
        options=ClusterOptions(),
    )
    assert result.values == {9: "Alpha", 12: "Beta"}
    assert result.clusters == []

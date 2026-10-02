"""Reviewed cluster computation reuses preview rules over the captured source."""

from frisket.actions.cluster_types import ClusterOptions, ClusterReview
from frisket.engine.executor.value_cluster import (
    compute_reviewed_clusters,
    recompute_cluster_coverage,
)


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


def test_cluster_coverage_indexes_each_source_row_once():
    class CountingValues(dict[int, str]):
        reads = 0

        def get(self, key, default=None):
            self.reads += 1
            return super().get(key, default)

    values = CountingValues()
    clusters = []
    row_ids = []
    for group in range(40):
        members = [group * 2 + 1, group * 2 + 2]
        surfaces = [f"Entity {group}", f"ENTITY {group}"]
        row_ids.extend(members)
        values.update(zip(members, surfaces, strict=True))
        clusters.append(
            {
                "key": str(group),
                "canonical": surfaces[0],
                "values": [{"value": value, "count": 1} for value in surfaces],
                "row_ids": list(reversed(members)),
                "size": 2,
            }
        )

    rebuilt = recompute_cluster_coverage(clusters, values, row_ids)

    assert values.reads == len(row_ids)
    assert rebuilt[0]["row_ids"] == [1, 2]
    assert rebuilt[-1]["values"] == [
        {"value": "ENTITY 39", "count": 1},
        {"value": "Entity 39", "count": 1},
    ]

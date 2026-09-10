"""Review-aware canonicalization of one whole column."""

from frisket.actions.cluster_types import (
    ClusterColumn,
    ClusterOptions,
    PreparedClustering,
    ValueClusterer,
)
from frisket.actions.core import ActionCategory, action


class ClusterParams(ClusterOptions):
    source: ClusterColumn


def cluster_values(
    params: ClusterParams, clusterer: ValueClusterer
) -> PreparedClustering:
    options = ClusterOptions.model_validate(params.model_dump(exclude={"source"}))
    return clusterer.prepare(params.source, options=options)


CLUSTER_VALUES = action(
    name="values",
    title="Cluster duplicate values",
    description="Group a column's duplicate values and save their reviewed canonical forms.",
    category=ActionCategory.CLEANUP,
    run=cluster_values,
    examples=(ClusterParams(source="name"),),
)

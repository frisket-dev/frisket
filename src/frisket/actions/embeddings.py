"""Typed embedding index actions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    CreatedEmbeddingIndex,
    EmbeddingModality,
    IndexCreator,
    IndexRefresher,
    RefreshedEmbeddingIndex,
    DeletedEmbeddingIndex,
    EmbeddingIndexPolicy,
    EmbeddingIndexPolicyUpdater,
    EmbeddingIndexReader,
    IndexDeleter,
    IndexExport,
    IndexExporter,
    IndexExportFormat,
    TableResult,
    TableRow,
)


class IndexCreateParams(ActionParams):
    source_columns: list[str] = Field(min_length=1, max_length=64)
    sheet_id: int | None = Field(default=None, strict=True, ge=1)
    modality: EmbeddingModality = "text"
    provider: str | None = None
    model: str | None = None
    source_query: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    maintenance_policy: dict[str, Any] = Field(default_factory=dict)
    provider_policy: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None

    @field_validator("source_columns")
    @classmethod
    def _source_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("source columns must be non-empty strings")
        return value

    @field_validator("provider", "model", "name")
    @classmethod
    def _nonblank_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must be a non-empty string or null")
        return value


class IndexRefreshParams(ActionParams):
    index_id: str = Field(min_length=1)
    mode: Literal["incremental", "full"] = "incremental"
    trigger_ref: dict[str, Any] | None = None
    row_scope: dict[str, Any] | None = None

    @field_validator("index_id")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("index_id must be non-empty")
        return value


def create_index(
    params: IndexCreateParams, indexes: IndexCreator
) -> CreatedEmbeddingIndex:
    return indexes.create(
        source_columns=params.source_columns,
        sheet_id=params.sheet_id,
        modality=params.modality,
        provider=params.provider,
        model=params.model,
        source_query=params.source_query,
        source_policy=params.source_policy,
        maintenance_policy=params.maintenance_policy,
        provider_policy=params.provider_policy,
        name=params.name,
    )


def refresh_index(
    params: IndexRefreshParams, indexes: IndexRefresher
) -> RefreshedEmbeddingIndex:
    return indexes.refresh(
        params.index_id,
        mode=params.mode,
        trigger_ref=params.trigger_ref,
        row_scope=params.row_scope,
    )


INDEX_CREATE = action(
    name="index_create",
    title="Create embedding index",
    description=(
        "Create an embedding index and its exact provider/model space. Custom remote "
        "models may require a paid dimension-discovery probe; remote policy, spend "
        "limits, accounting and checkpoint recovery apply before that call."
    ),
    category=ActionCategory.SOURCES,
    run=create_index,
    form="embedding_index_create",
    examples=(IndexCreateParams(source_columns=["text"], sheet_id=1),),
)
INDEX_REFRESH = action(
    name="index_refresh",
    title="Refresh embedding index",
    description=(
        "Refresh missing or changed vectors, or rebuild the full index. The worker "
        "reads current sources and enforces remote and automatic-refresh policies."
    ),
    category=ActionCategory.SOURCES,
    run=refresh_index,
    form="embedding_index_refresh",
    examples=(IndexRefreshParams(index_id="example-index"),),
)


class IndexDeleteParams(ActionParams):
    index_id: str = Field(min_length=1)

    @field_validator("index_id")
    @classmethod
    def _nonblank_index(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("index_id must be non-empty")
        return value


def delete_index(
    params: IndexDeleteParams, indexes: IndexDeleter
) -> DeletedEmbeddingIndex:
    return indexes.delete(params.index_id)


INDEX_DELETE = action(
    examples=(IndexDeleteParams(index_id="example-index"),),
    name="index_delete",
    title="Delete embedding index",
    description=(
        "Delete the index definition, its cached vectors, its unshared embedding "
        "space, and export artifacts. Source data and other indexes are preserved. "
        "Vector deletion and export cleanup "
        "cannot be rolled back with the project metadata."
    ),
    category=ActionCategory.SOURCES,
    run=delete_index,
    form="embedding_index_delete",
)


class IndexExportDestination(ActionParams):
    kind: Literal["local_dir"]
    path: str = Field(min_length=1)


class IndexExportParams(ActionParams):
    index_id: str = Field(min_length=1)
    destination: IndexExportDestination
    formats: list[IndexExportFormat] = Field(default_factory=lambda: ["jsonl"])
    include_vectors: bool = Field(default=True, strict=True)

    @field_validator("index_id")
    @classmethod
    def _nonblank_index(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("index_id must be non-empty")
        return value

    @field_validator("formats")
    @classmethod
    def _ordered_formats(cls, formats: list[str]) -> list[str]:
        if not formats:
            raise ValueError("at least one format is required")
        return list(dict.fromkeys(formats))


def export_index(params: IndexExportParams, exporter: IndexExporter) -> IndexExport:
    return exporter.export(
        params.index_id,
        path=params.destination.path,
        formats=params.formats,
        include_vectors=params.include_vectors,
    )


INDEX_EXPORT = action(
    examples=(
        IndexExportParams(
            index_id="example-index",
            destination=IndexExportDestination(
                kind="local_dir", path="exports/embeddings"
            ),
            formats=["jsonl"],
        ),
    ),
    name="index_export",
    title="Export embedding index",
    description=(
        "Export a fresh index's source originals and optional raw vectors as "
        "JSONL, Parquet, or Arrow files, with recorded index provenance."
    ),
    category=ActionCategory.SOURCES,
    run=export_index,
    form="embedding_index_export",
)


class IndexUpdatePolicyParams(ActionParams):
    index_id: str = Field(min_length=1)
    maintenance_policy: dict[str, Any] | None = Field(
        default=None,
        description="Replace the whole maintenance policy; omit to keep it.",
    )
    provider_policy: dict[str, Any] | None = Field(
        default=None, description="Replace the whole provider policy; omit to keep it."
    )

    @field_validator("index_id")
    @classmethod
    def _index_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("index_id must be non-empty")
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> IndexUpdatePolicyParams:
        if self.maintenance_policy is None and self.provider_policy is None:
            raise ValueError("at least one replacement policy is required")
        return self


def update_index_policy(
    params: IndexUpdatePolicyParams, indexes: EmbeddingIndexPolicyUpdater
) -> EmbeddingIndexPolicy:
    return indexes.update_policy(
        params.index_id,
        maintenance_policy=params.maintenance_policy,
        provider_policy=params.provider_policy,
    )


INDEX_UPDATE_POLICY = action(
    examples=(
        IndexUpdatePolicyParams(
            index_id="example-index", maintenance_policy={"mode": "manual"}
        ),
    ),
    name="index_update_policy",
    title="Update embedding index policy",
    description=(
        "Replace an index's maintenance and/or provider policy. Metadata only: "
        "no provider call or vector work. Remote egress and cost gates still "
        "apply at refresh time."
    ),
    category=ActionCategory.SOURCES,
    run=update_index_policy,
    form="embedding_index_update_policy",
)


class IndexProjectParams(ActionParams):
    index_id: str = Field(min_length=1)
    method: Literal["pca"] = "pca"
    dimensions: Literal[2] = 2


class ProjectionRow(BaseModel):
    source_key: str
    dim_0: float
    dim_1: float


def project_index(
    params: IndexProjectParams, indexes: EmbeddingIndexReader
) -> TableResult[ProjectionRow]:
    rows = indexes.read(params.index_id, minimum_rows=params.dimensions)
    coordinates = _pca_2d([list(row.vector) for row in rows])
    return TableResult(
        rows=(
            TableRow(
                output=ProjectionRow(source_key=row.source_key, dim_0=x, dim_1=y),
                sources=(row.source,),
                parent=row.source,
            )
            for row, (x, y) in zip(rows, coordinates, strict=True)
        )
    )


class IndexClusterParams(ActionParams):
    index_id: str = Field(min_length=1)
    method: Literal["kmeans"] = "kmeans"
    k: int = Field(ge=2, strict=True)
    seed: int = 0


class ClusterRow(BaseModel):
    source_key: str
    cluster_id: int


def cluster_index(
    params: IndexClusterParams, indexes: EmbeddingIndexReader
) -> TableResult[ClusterRow]:
    rows = indexes.read(params.index_id, minimum_rows=params.k)
    labels = _kmeans([list(row.vector) for row in rows], params.k, seed=params.seed)
    return TableResult(
        rows=(
            TableRow(
                output=ClusterRow(source_key=row.source_key, cluster_id=label),
                sources=(row.source,),
                parent=row.source,
            )
            for row, label in zip(rows, labels, strict=True)
        )
    )


INDEX_PROJECT = action(
    examples=(IndexProjectParams(index_id="example-index"),),
    name="index_project",
    title="Project embedding index to 2D",
    description="Create a deterministic PCA projection of a fresh embedding index.",
    category=ActionCategory.SOURCES,
    run=create_sheet(project_index),
)
INDEX_CLUSTER = action(
    examples=(IndexClusterParams(index_id="example-index", k=3),),
    name="index_cluster",
    title="Cluster embedding index (k-means)",
    description="Create deterministic k-means assignments from a fresh embedding index.",
    category=ActionCategory.SOURCES,
    run=create_sheet(cluster_index),
)


def _pca_2d(vectors: list[list[float]]) -> list[tuple[float, float]]:
    """Deterministic numpy-only PCA to 2D: mean-center, SVD, take the top-2
    principal axes. Component signs are pinned by the index of each axis's
    max-abs loading so the same input always yields the same coordinates (so
    idempotency/replay hashes are stable). numpy-only by necessity — sklearn/scipy
    are not dependencies."""
    import numpy as np

    x = np.asarray(vectors, dtype=np.float64)
    centered = x - x.mean(axis=0)
    # Xc = U S Vt ; principal axes are the rows of Vt.
    _u, _s, vt = np.linalg.svd(centered, full_matrices=False)
    comps = vt[:2].copy()
    for i in range(comps.shape[0]):
        j = int(np.argmax(np.abs(comps[i])))
        if comps[i, j] < 0:
            comps[i] = -comps[i]
    coords = centered @ comps.T
    return [(float(row[0]), float(row[1])) for row in coords]


def _kmeans(
    vectors: list[list[float]], k: int, *, seed: int, max_iter: int = 50
) -> list[int]:
    """Deterministic numpy-only k-means.

    Init is k-means++ driven entirely by ``numpy.random.default_rng(seed)``;
    Lloyd iterations run to convergence or ``max_iter``. Caller guarantees
    ``len(vectors) >= k``. Empty clusters are reseeded deterministically to the
    point that is farthest (by squared distance) from its current centroid,
    breaking ties by the lowest source index — so the same vectors + k + seed
    always yield the same labels (vectors arrive ORDER BY source_key).
    numpy-only by necessity — sklearn/scipy are not dependencies."""
    import numpy as np

    x = np.asarray(vectors, dtype=np.float64)
    n = x.shape[0]
    rng = np.random.default_rng(seed)

    # k-means++ seeding.
    first = int(rng.integers(n))
    centers = [first]
    closest_sq = ((x - x[first]) ** 2).sum(axis=1)
    for _ in range(1, k):
        total = float(closest_sq.sum())
        if total <= 0.0:
            # All remaining points coincide with chosen centers; pick the first
            # index not already a center (deterministic).
            candidates = [i for i in range(n) if i not in centers]
            nxt = candidates[0] if candidates else first
        else:
            probs = closest_sq / total
            nxt = int(rng.choice(n, p=probs))
        centers.append(nxt)
        new_sq = ((x - x[nxt]) ** 2).sum(axis=1)
        closest_sq = np.minimum(closest_sq, new_sq)
    centroids = x[centers].copy()

    labels = np.zeros(n, dtype=np.int64)
    for _ in range(max_iter):
        # assignment: nearest centroid, ties to the lowest centroid index.
        dists = ((x[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        new_labels = np.argmin(dists, axis=1).astype(np.int64)
        # update: recompute centroids; reseed empty clusters deterministically.
        for c in range(k):
            members = x[new_labels == c]
            if members.shape[0] == 0:
                point_dists = ((x - centroids[c]) ** 2).sum(axis=1)
                far = int(np.argmax(point_dists))
                centroids[c] = x[far]
                new_labels[far] = c
            else:
                centroids[c] = members.mean(axis=0)
        if np.array_equal(new_labels, labels):
            labels = new_labels
            break
        labels = new_labels
    return [int(v) for v in labels]

"""Cluster computation over one captured source; publication belongs to the host."""

from dataclasses import dataclass
from contextlib import nullcontext
from typing import Any
import sqlite3

from frisket.actions.cluster_types import ClusterColumn, ClusterOptions
from frisket.preview.cluster import (
    apply_group_edits,
    compute_method_clusters,
)
from frisket.ops.cluster_values import (
    canonical_column_values,
    cluster_snapshot,
    hash_column_values,
)


@dataclass(frozen=True)
class ClusterComputation:
    values: dict[int, str]
    clusters: list[dict[str, Any]]
    envelope: dict[str, Any]


class ClusterSourceChanged(ValueError):
    """The exact reviewed or admitted source is no longer current."""


def cluster_embedding_inputs(values, options):
    from frisket.authoring.templates import render_value_key
    from frisket.ops.cluster import semantic_embedding_inputs

    return semantic_embedding_inputs(
        cluster_snapshot(values),
        derive_key=(lambda value: render_value_key(options.key_template, value))
        if options.key_template
        else None,
    )


def missing_cluster_vectors(project, texts, model):
    """Quote cache misses without creating or warming the rebuildable sidecar."""
    from frisket.semantic import _vec_key

    keys = [_vec_key(model, text) for text in texts]
    sidecar = project.path / "project.search.db"
    if not keys or not sidecar.exists():
        return len(keys)
    cached = set()
    try:
        db = sqlite3.connect(f"{sidecar.resolve().as_uri()}?mode=ro", uri=True)
        try:
            for offset in range(0, len(keys), 500):
                chunk = keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                cached.update(
                    row[0]
                    for row in db.execute(
                        f"SELECT key FROM cell_vec WHERE key IN ({placeholders})", chunk
                    )
                )
        finally:
            db.close()
    except sqlite3.Error:
        return len(keys)
    return sum(key not in cached for key in keys)


def cluster_source_snapshot(project, sheet_id: int, source: ClusterColumn):
    """Read source identity, roster and values from the same database snapshot."""
    context = (
        nullcontext(project) if project.db.in_transaction else project.read_snapshot()
    )
    with context as snapshot:
        sheet = snapshot.db.execute(
            "SELECT name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        column = snapshot.db.execute(
            "SELECT id,name,type FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
            (sheet_id, source.name),
        ).fetchone()
        if (
            sheet is None
            or column is None
            or column["type"] not in ClusterColumn.accepted_column_types
        ):
            raise ValueError(
                "Clustering requires a visible text, category, or link source column"
            )
        row_ids = list(snapshot.visible_row_ids(sheet_id))
        current = snapshot.get_values(sheet_id, column["id"])
        values = {row_id: current.get(row_id) for row_id in row_ids}
        return {
            "sheet_id": sheet_id,
            "sheet_name": sheet["name"],
            "column_id": column["id"],
            "name": column["name"],
            "type": column["type"],
            "row_ids": row_ids,
            "value_hash": hash_column_values(row_ids, values),
        }, values


def validate_cluster_source(project, expected):
    try:
        current, values = cluster_source_snapshot(
            project, expected["sheet_id"], ClusterColumn(expected["name"])
        )
    except ValueError as exc:
        raise ClusterSourceChanged(
            "The admitted cluster source is no longer available"
        ) from exc
    if current != expected:
        raise ClusterSourceChanged(
            "The cluster source changed; review its groups again"
        )
    return values


def recompute_cluster_coverage(
    clusters: list[dict[str, Any]],
    values_by_row: dict[int, Any],
    row_ids: list[int],
) -> list[dict[str, Any]]:
    """Bind surviving variants to their exact rows after review exclusions."""
    rebuilt: list[dict[str, Any]] = []
    for cluster in clusters:
        surfaces = [value["value"] for value in cluster["values"]]
        surface_set = set(surfaces)
        per_surface: dict[str, list[int]] = {surface: [] for surface in surfaces}
        for row_id in row_ids:
            raw = values_by_row.get(row_id)
            surface = "" if raw is None else str(raw).strip()
            if surface in surface_set:
                per_surface[surface].append(row_id)
        member_row_ids = sorted(
            row_id for rows in per_surface.values() for row_id in rows
        )
        ordered_values = [
            {"value": surface, "count": len(per_surface[surface])}
            for surface in sorted(surfaces, key=lambda s: (-len(per_surface[s]), s))
        ]
        rebuilt.append(
            {
                **cluster,
                "values": ordered_values,
                "row_ids": member_row_ids,
                "size": len(member_row_ids),
            }
        )
    return rebuilt


def compute_reviewed_clusters(
    project,
    *,
    sheet_id: int,
    column: str,
    source_values: dict[int, Any],
    options: ClusterOptions,
    embed=None,
    embed_id: str | None = None,
) -> ClusterComputation:
    if options.review and options.review.source_hash is not None:
        actual_hash = hash_column_values(list(source_values), source_values)
        if options.review.source_hash != actual_hash:
            raise ClusterSourceChanged(
                "The reviewed cluster source changed; review its groups again"
            )
    groups, envelope = compute_method_clusters(
        project,
        sheet_id,
        column,
        method=options.method,
        min_size=options.min_size,
        threshold=options.threshold,
        ngram_size=options.ngram_size,
        key_template=options.key_template,
        embed=embed,
        embed_id=embed_id,
        values=cluster_snapshot(source_values),
    )
    adjusted = apply_group_edits(
        groups,
        min_size=options.min_size,
        canonical_overrides=(
            options.review.canonical_overrides if options.review else {}
        ),
        excluded_members=(options.review.excluded_members if options.review else {}),
    )
    row_ids = list(source_values)
    adjusted = recompute_cluster_coverage(adjusted, source_values, row_ids)
    return ClusterComputation(
        values=canonical_column_values(
            adjusted, source_values=source_values, row_ids=row_ids
        ),
        clusters=adjusted,
        envelope=envelope,
    )

"""Stable value transforms shared by cluster preview and execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def hash_column_values(row_ids: list[int], raw_values: dict[int, Any]) -> str:
    """Deterministically hash visible values from an already-read snapshot."""
    ordered = [
        {"row_id": int(row_id), "value": raw_values.get(row_id)} for row_id in row_ids
    ]
    encoded = json.dumps(
        ordered, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def cluster_snapshot(raw_values: dict[int, Any]) -> dict[int, str]:
    """Return non-empty, stripped surfaces from a caller-owned value snapshot."""
    snapshot: dict[int, str] = {}
    for row_id, value in raw_values.items():
        if value is None:
            continue
        surface = str(value).strip()
        if surface:
            snapshot[int(row_id)] = surface
    return snapshot


def canonical_column_values(
    adjusted_clusters: list[dict[str, Any]],
    *,
    source_values: dict[int, Any],
    row_ids: list[int],
) -> dict[int, str]:
    """Map clustered surfaces to canonicals and retain every other source value."""
    surface_to_canonical: dict[str, str] = {}
    for cluster in adjusted_clusters:
        canonical = cluster["canonical"]
        for value in cluster["values"]:
            surface_to_canonical[value["value"]] = canonical

    out: dict[int, str] = {}
    for row_id in row_ids:
        raw = source_values.get(row_id)
        surface = "" if raw is None else str(raw).strip()
        out[row_id] = surface_to_canonical.get(surface, surface)
    return out

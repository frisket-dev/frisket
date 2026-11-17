"""Cluster and resolve ops plus the Entities sheet.

The OpenRefine-style cluster panel needs something to power it: a way to group
near-duplicate values in a column into clusters of likely-the-same thing, and a
way to *resolve* a cluster to a single canonical value materialised into an
``Entities`` sheet (so watchlists / joins can hang off stable entity ids).

The clustering itself is deterministic and network-free string fingerprinting
(OpenRefine's "key collision" + n-gram methods); those pure helpers live in
``frisket.ops.cluster_fingerprint`` since non-engine layers (preview, other
ops) need them too. This module owns the Entities-sheet write machinery: the
``resolve`` op materialises a cluster's canonical value, keyed by fingerprint,
into the sheet. The model is reserved for an optional later ``resolve``
polish step; the canonical pick here is the most-frequent / longest surface
form, which is good enough to be honestly useful.
"""

from __future__ import annotations

from typing import Any

from frisket.engine.store import Project

_ENTITIES_SHEET = "Entities"


def list_entities(project: Project) -> list[dict[str, Any]]:
    """Rows of the Entities sheet (empty list if it doesn't exist yet)."""
    sid = None
    for s in project.sheets():
        if s["name"] == _ENTITIES_SHEET:
            sid = s["id"]
            break
    if sid is None:
        return []
    cols = {c["name"]: c["id"] for c in project.columns(sid)}
    by_col = {name: project.get_values(sid, cid) for name, cid in cols.items()}
    row_ids = sorted({rid for vals in by_col.values() for rid in vals})
    out: list[dict[str, Any]] = []
    for rid in row_ids:
        out.append(
            {
                "id": rid,
                "entity": by_col.get("entity", {}).get(rid),
                "key": by_col.get("key", {}).get(rid),
                "variants": by_col.get("variants", {}).get(rid),
                "mentions": by_col.get("mentions", {}).get(rid),
            }
        )
    return out

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

from collections import Counter
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.cell_writes import create_base_cell_producer
from frisket.ops.cluster_fingerprint import compute_clusters, fingerprint
from frisket.ops.cluster_fingerprint import canonical as _pick_canonical

_ENTITIES_SHEET = "Entities"


def run_cluster(
    project: Project, sheet_id: int, column: str, min_size: int = 2
) -> dict[str, Any]:
    """Cluster a column and log the op. Read-only over the data; returns the
    clusters for the OpenRefine-style panel to render."""
    clusters = compute_clusters(project, sheet_id, column, min_size=min_size)
    project.append_op(
        "cluster",
        {
            "sheet_id": sheet_id,
            "column": column,
            "min_size": min_size,
            "clusters": len(clusters),
        },
        label=f"cluster {column}",
    )
    return {
        "sheet_id": sheet_id,
        "column": column,
        "clusters": clusters,
        "count": len(clusters),
    }


def _entities_sheet_id(project: Project) -> int:
    for s in project.sheets():
        if s["name"] == _ENTITIES_SHEET:
            return s["id"]
    sid = project.add_sheet(_ENTITIES_SHEET)
    project.add_column(sid, "entity", type="text")
    project.add_column(sid, "key", type="text")
    project.add_column(sid, "variants", type="text")
    project.add_column(sid, "mentions", type="number")
    return sid


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


def run_resolve(
    project: Project,
    sheet_id: int,
    column: str,
    canonical: str | None = None,
    key: str | None = None,
    members: list[str] | None = None,
    min_size: int = 2,
) -> dict[str, Any]:
    """Resolve one (or all) cluster(s) to canonical entities in the Entities
    sheet. If ``key``/``canonical``/``members`` are given, resolve just that
    cluster; otherwise materialise every auto-detected cluster.

    Returns the entities created. Each entity row records its canonical name,
    the cluster fingerprint key, the variant surface forms, and the mention
    count, giving watchlists/joins a stable thing to hang off."""
    clusters = compute_clusters(project, sheet_id, column, min_size=min_size)
    by_key = {c["key"]: c for c in clusters}

    chosen: list[dict[str, Any]]
    if key is not None or members is not None:
        if members is not None:
            variants = list(dict.fromkeys(members))
            counts = Counter({v: 1 for v in variants})
            ck = key or fingerprint(canonical or (variants[0] if variants else ""))
            chosen = [
                {
                    "key": ck,
                    "canonical": canonical or _pick_canonical(counts),
                    "values": [{"value": v, "count": 1} for v in variants],
                    "size": len(variants),
                }
            ]
        else:
            cl = by_key.get(key)
            if cl is None:
                raise KeyError(key)
            if canonical:
                cl = {**cl, "canonical": canonical}
            chosen = [cl]
    else:
        chosen = clusters

    ent_sid = _entities_sheet_id(project)
    records = []
    for cl in chosen:
        variants = [v["value"] for v in cl["values"]]
        records.append(
            {
                "entity": cl["canonical"],
                "key": cl["key"],
                "variants": ", ".join(variants),
                "mentions": cl["size"],
            }
        )
    col_ids = {c["name"]: c["id"] for c in project.columns(ent_sid)}
    op_spec = {
        "sheet_id": sheet_id,
        "column": column,
        "entities": len(records),
        "entities_sheet_id": ent_sid,
    }
    try:
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op(
            "resolve",
            op_spec,
            label=f"resolve {column}",
            commit=False,
        )
        if records:
            producer_id = create_base_cell_producer(
                project.db, stage_id=f"op:{op_id}", op_id=op_id
            )
            row_ids = project.add_rows(
                ent_sid,
                records,
                col_ids,
                producer_id=producer_id,
                commit=False,
            )
            project.set_undo_info(op_id, {"created_rows": row_ids}, commit=False)
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    return {
        "entities_sheet_id": ent_sid,
        "created": len(records),
        "entities": records,
    }

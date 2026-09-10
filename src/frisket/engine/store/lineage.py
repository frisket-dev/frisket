"""Store-layer projections for derived-sheet lineage (Workbench IA inc 7).

The HTTP service stays projection-thin (a purity guard forbids direct SQL in
``server/services/projects.py``); this module owns the reads that enrich the
sheets list and assemble the Monitor Lineage DAG. Staleness is layered on from
``frisket.store.staleness`` (lazy, pull-computed) so nothing here pushes flags.
"""

from __future__ import annotations

from typing import Any

from frisket.authoring.action_metadata import run_row_action_kind
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.staleness import compute_sync_states


def build_lineage_dag(project: Any) -> dict[str, Any]:
    """The Monitor Lineage DAG: sources -> sheets -> AI columns.

    Generalizes ProjectDebugService.debug's lineage shape (sheets + columns +
    current-run provenance) with a sources tier (``sources.sheet_id``) and
    multi-parent edges (``materialized_row_sources``). Stale sheet nodes and the
    edges into them are flagged from the lazy staleness resolver.
    """
    sync_states = compute_sync_states(project)
    sheets = project.sheets()
    parent_op_ids = [
        int(s["parent_op_id"]) for s in sheets if s["parent_op_id"] is not None
    ]
    ops_by_id = project.ops_meta(parent_op_ids)
    run_rows = {
        int(r["id"]): r
        for r in project.db.execute(
            "SELECT id, action_kind, model FROM runs"
        ).fetchall()
    }

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    present_sheet_ids = {int(s["id"]) for s in sheets}
    generations = ResultGenerationStore(project)

    def _stale(sheet_id: int) -> bool:
        state = sync_states.get(sheet_id)
        return state is not None and state["sync_state"] == "stale"

    # Sources tier.
    for src in project.db.execute(
        "SELECT id, name, kind, sheet_id, last_status FROM sources"
    ).fetchall():
        src_node = f"source:{int(src['id'])}"
        nodes.append(
            {
                "id": src_node,
                "kind": "source",
                "name": src["name"],
                "source_kind": src["kind"],
                "last_status": src["last_status"],
                "sheet_id": src["sheet_id"],
            }
        )
        if src["sheet_id"] is not None and int(src["sheet_id"]) in present_sheet_ids:
            edges.append(
                {
                    "from": src_node,
                    "to": f"sheet:{int(src['sheet_id'])}",
                    "kind": "source",
                    "stale": False,
                }
            )

    # Sheets tier + AI-column tier.
    for sheet in sheets:
        sheet_id = int(sheet["id"])
        state = sync_states.get(sheet_id)
        node: dict[str, Any] = {
            "id": f"sheet:{sheet_id}",
            "kind": "sheet",
            "name": sheet["name"],
            "sheet_id": sheet_id,
            "parent_sheet_id": sheet["parent_sheet_id"],
            "parent_op_id": sheet["parent_op_id"],
            "derived": sheet["parent_sheet_id"] is not None,
            "syncState": state["sync_state"] if state is not None else None,
            "stale_reason": state["stale_reason"] if state is not None else None,
        }
        parent_op = ops_by_id.get(
            int(sheet["parent_op_id"]) if sheet["parent_op_id"] is not None else -1
        )
        if parent_op is not None:
            node["op_kind"] = parent_op["kind"]
            node["op_label"] = parent_op["label"]
        nodes.append(node)

        if (
            sheet["parent_sheet_id"] is not None
            and int(sheet["parent_sheet_id"]) in present_sheet_ids
        ):
            edges.append(
                {
                    "from": f"sheet:{int(sheet['parent_sheet_id'])}",
                    "to": f"sheet:{sheet_id}",
                    "kind": "derive",
                    "stale": _stale(sheet_id),
                }
            )

        for column in project.columns(sheet_id, include_hidden=True):
            if not column["ai_generated"]:
                continue
            run_id = generations.latest_applied_run_id(int(column["id"]))
            if run_id is None:
                continue
            run = run_rows.get(run_id)
            col_node = f"column:{int(column['id'])}"
            nodes.append(
                {
                    "id": col_node,
                    "kind": "ai_column",
                    "name": column["name"],
                    "sheet_id": sheet_id,
                    "column_id": int(column["id"]),
                    "current_run_id": run_id,
                    "action_kind": (
                        run_row_action_kind(run) if run is not None else None
                    ),
                    "model": run["model"] if run is not None else None,
                }
            )
            edges.append(
                {
                    "from": f"sheet:{sheet_id}",
                    "to": col_node,
                    "kind": "ai_column",
                    "stale": _stale(sheet_id),
                }
            )

    # Multi-parent edges (join/resolve/reduce membership).
    seen_edges = {(e["from"], e["to"]) for e in edges}
    for row in project.db.execute(
        "SELECT DISTINCT r.sheet_id AS child_sheet_id, mrs.source_sheet_id "
        "FROM materialized_row_sources mrs "
        "JOIN rows r ON mrs.materialized_row_id = r.id"
    ).fetchall():
        child = int(row["child_sheet_id"])
        source = int(row["source_sheet_id"])
        if child == source:
            continue
        if child not in present_sheet_ids or source not in present_sheet_ids:
            continue
        key = (f"sheet:{source}", f"sheet:{child}")
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(
            {
                "from": f"sheet:{source}",
                "to": f"sheet:{child}",
                "kind": "derive",
                "stale": _stale(child),
            }
        )

    return {
        "op_cursor": project.op_cursor,
        "nodes": nodes,
        "edges": edges,
    }

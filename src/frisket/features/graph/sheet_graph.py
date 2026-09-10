from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.materialization import active_materialized_row_sources


GRAPH_SCHEMA_VERSION = "frisket.sheet_graph.v1"

# Membership roles that make a sheet an edge/join table.
LINK_ROLES = ("edge_source", "edge_target")
JOIN_ROLES = ("join_left", "join_right")
EDGE_ROLES = (*LINK_ROLES, *JOIN_ROLES)

# ops.kind values whose output sheets carry edge/join membership.
EDGE_OP_KINDS = {"derive.link_table"}
JOIN_OP_KINDS = {"derive.join"}

_DEFAULT_LIMIT_NODES = 200
_DEFAULT_LIMIT_EDGES = 500
_MAX_LIMIT_NODES = 2000
_MAX_LIMIT_EDGES = 5000

# Node display-name heuristic: first column whose lowercased name is
# one of these, else the first text column.
_LABEL_NAME_HINTS = ("name", "title", "label")


def sheet_materialized_kind(
    project: Project,
    sheet_id: int,
    *,
    op_kind: str | None,
) -> str | None:
    """The sheet-shape availability signal: 'edge' | 'join' | None.

    Honest gate = producing ``ops.kind`` in the edge/join families AND the
    presence of the matching membership roles for that sheet's rows. Op kind
    alone is not enough (a producer could in principle emit a non-edge output);
    membership alone is the substrate ground truth.
    """
    if op_kind in EDGE_OP_KINDS:
        candidate, roles = "edge", LINK_ROLES
    elif op_kind in JOIN_OP_KINDS:
        candidate, roles = "join", JOIN_ROLES
    else:
        return None
    placeholders = ",".join("?" for _ in roles)
    row = project.db.execute(
        "SELECT 1 FROM materialized_row_sources AS m "
        "JOIN rows AS r ON r.id = m.materialized_row_id "
        f"WHERE r.sheet_id=? AND r.hidden=0 AND m.role IN ({placeholders}) LIMIT 1",
        (sheet_id, *roles),
    ).fetchone()
    return candidate if row is not None else None


class SheetGraphError(Exception):
    """Typed error for the sheet-graph read path (route maps to HTTP)."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class GraphViewConfig:
    direction: str | None = None  # 'directed' | 'undirected' | None (auto per family)
    node_label_column_id: int | None = None
    node_color_column_id: int | None = None
    node_size_column_id: int | None = None
    edge_label_column_id: int | None = None
    limit_nodes: int = _DEFAULT_LIMIT_NODES
    limit_edges: int = _DEFAULT_LIMIT_EDGES


@dataclass
class _NodeAccum:
    sheet_id: int
    row_id: int
    degree: int = 0
    label: Any = None
    color_value: Any = None
    size_value: Any = None


def _node_id(sheet_id: int, row_id: int) -> str:
    return f"{sheet_id}:{row_id}"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def build_sheet_graph(
    project: Project,
    *,
    sheet_id: int,
    config: GraphViewConfig | None = None,
) -> dict[str, Any]:
    config = config or GraphViewConfig()
    limit_nodes = _clamp(int(config.limit_nodes), 1, _MAX_LIMIT_NODES)
    limit_edges = _clamp(int(config.limit_edges), 1, _MAX_LIMIT_EDGES)

    sheet = project.db.execute(
        "SELECT id, name, parent_op_id FROM sheets WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    if sheet is None:
        raise SheetGraphError(404, "sheet_not_found", f"no sheet {sheet_id}")

    materialized_kind = _materialized_kind(project, sheet["parent_op_id"])
    diagnostics: list[dict[str, Any]] = []

    all_edge_row_ids = project.visible_row_ids(sheet_id)
    truncated = False
    if len(all_edge_row_ids) > limit_edges:
        truncated = True
        edge_row_ids = all_edge_row_ids[:limit_edges]
    else:
        edge_row_ids = list(all_edge_row_ids)

    cur = project.db.cursor()
    membership = active_materialized_row_sources(
        cur,
        materialized_row_ids=edge_row_ids,
        roles=EDGE_ROLES,
    )

    if not membership:
        # Honest empty: the gate should keep the view from
        # mounting here, so this is defense-in-depth, not the primary UX.
        diagnostics.append(
            {
                "code": "not_an_edge_sheet",
                "message": (
                    "sheet has no edge/join membership "
                    "(edge_source/edge_target/join_left/join_right)"
                ),
            }
        )
        return _envelope(
            sheet_id=sheet_id,
            materialized_kind=None,
            direction=config.direction or "directed",
            nodes=[],
            edges=[],
            truncated=truncated,
            limit_nodes=limit_nodes,
            limit_edges=limit_edges,
            diagnostics=diagnostics,
        )

    by_edge: dict[int, dict[str, tuple[int, int]]] = {}
    roles_seen: set[str] = set()
    for row in membership:
        edge_row = int(row["materialized_row_id"])
        role = str(row["role"])
        roles_seen.add(role)
        by_edge.setdefault(edge_row, {})[role] = (
            int(row["source_sheet_id"]),
            int(row["source_row_id"]),
        )

    is_link = bool(set(LINK_ROLES) & roles_seen)
    is_join = bool(set(JOIN_ROLES) & roles_seen)
    if is_link and is_join:
        diagnostics.append(
            {
                "code": "mixed_role_families",
                "message": "sheet carries both link and join membership roles",
            }
        )
    family = "link" if is_link else "join"
    default_direction = "directed" if family == "link" else "undirected"
    direction = config.direction or default_direction
    if direction not in ("directed", "undirected"):
        direction = default_direction

    tail_role = "edge_source" if family == "link" else "join_left"
    head_role = "edge_target" if family == "link" else "join_right"

    nodes: dict[str, _NodeAccum] = {}
    edges: list[dict[str, Any]] = []
    dangling = 0

    def touch(ref: tuple[int, int]) -> _NodeAccum:
        nid = _node_id(*ref)
        acc = nodes.get(nid)
        if acc is None:
            acc = _NodeAccum(sheet_id=ref[0], row_id=ref[1])
            nodes[nid] = acc
        return acc

    for edge_row in edge_row_ids:
        roles = by_edge.get(edge_row)
        if not roles:
            continue
        tail = roles.get(tail_role)
        head = roles.get(head_role)
        if tail is None or head is None:
            # Outer/dangling edge (one endpoint missing): keep the present
            # endpoint as a node but draw no edge.
            for ref in (tail, head):
                if ref is not None:
                    touch(ref)
            dangling += 1
            continue
        tail_acc = touch(tail)
        head_acc = touch(head)
        tail_acc.degree += 1
        head_acc.degree += 1
        edges.append(
            {
                "id": f"edge:{edge_row}",
                "source": _node_id(*tail),
                "target": _node_id(*head),
                "direction": direction,
                "label": None,  # resolved below
                "sheet_id": sheet_id,
                "row_id": edge_row,
                "row_ref": {"sheet_id": sheet_id, "row_id": edge_row},
            }
        )

    if dangling:
        diagnostics.append(
            {
                "code": "dangling_edges",
                "message": f"{dangling} edge row(s) had a missing endpoint",
                "count": dangling,
            }
        )

    _resolve_node_columns(project, nodes, config)
    _resolve_edge_labels(project, sheet_id, sheet["name"], edges, config)

    # Cap nodes (truncation) deterministically by (sheet_id, row_id).
    ordered_nodes = sorted(nodes.values(), key=lambda n: (n.sheet_id, n.row_id))
    if len(ordered_nodes) > limit_nodes:
        truncated = True
        kept = ordered_nodes[:limit_nodes]
        kept_ids = {_node_id(n.sheet_id, n.row_id) for n in kept}
        ordered_nodes = kept
        edges = [
            e for e in edges if e["source"] in kept_ids and e["target"] in kept_ids
        ]

    node_payload = [
        {
            "id": _node_id(n.sheet_id, n.row_id),
            "label": _node_label(n),
            "sheet_id": n.sheet_id,
            "row_id": n.row_id,
            "row_ref": {"sheet_id": n.sheet_id, "row_id": n.row_id},
            "degree": n.degree,
            "color_value": n.color_value,
            "size_value": n.size_value,
        }
        for n in ordered_nodes
    ]

    return _envelope(
        sheet_id=sheet_id,
        materialized_kind=materialized_kind or family,
        direction=direction,
        nodes=node_payload,
        edges=edges,
        truncated=truncated,
        limit_nodes=limit_nodes,
        limit_edges=limit_edges,
        diagnostics=diagnostics,
    )


def _node_label(node: _NodeAccum) -> str:
    if node.label is not None and str(node.label).strip():
        return str(node.label)
    return _node_id(node.sheet_id, node.row_id)


def _materialized_kind(project: Project, parent_op_id: Any) -> str | None:
    if parent_op_id is None:
        return None
    meta = project.ops_meta([int(parent_op_id)])
    op = meta.get(int(parent_op_id))
    if op is None:
        return None
    kind = op.get("kind")
    if kind in EDGE_OP_KINDS:
        return "edge"
    if kind in JOIN_OP_KINDS:
        return "join"
    return None


def _default_label_column_id(project: Project, sheet_id: int) -> int | None:
    columns = project.columns(sheet_id)
    text_cols = [c for c in columns if str(c["type"]) == "text"]
    for hint in _LABEL_NAME_HINTS:
        for col in columns:
            if str(col["name"]).lower() == hint:
                return int(col["id"])
    if text_cols:
        return int(text_cols[0]["id"])
    return None


def _resolve_node_columns(
    project: Project,
    nodes: dict[str, _NodeAccum],
    config: GraphViewConfig,
) -> None:
    if not nodes:
        return
    rows_by_sheet: dict[int, list[int]] = {}
    for node in nodes.values():
        rows_by_sheet.setdefault(node.sheet_id, []).append(node.row_id)

    # Which column id belongs to which sheet (a config column lives on ONE
    # endpoint sheet; other-sheet nodes resolve to None for that binding).
    def column_sheet(column_id: int | None) -> int | None:
        if column_id is None:
            return None
        col = project.get_column(int(column_id))
        return int(col["sheet_id"]) if col is not None else None

    label_sheet = column_sheet(config.node_label_column_id)
    color_sheet = column_sheet(config.node_color_column_id)
    size_sheet = column_sheet(config.node_size_column_id)

    for sheet_id, row_ids in rows_by_sheet.items():
        # Per-sheet default label column when none configured.
        label_col: int | None
        if label_sheet == sheet_id:
            label_col = config.node_label_column_id
        elif config.node_label_column_id is None:
            label_col = _default_label_column_id(project, sheet_id)
        else:
            label_col = None
        label_values = (
            project.get_values(sheet_id, int(label_col), row_ids=row_ids)
            if label_col is not None
            else {}
        )
        color_values = (
            project.get_values(
                sheet_id, int(config.node_color_column_id), row_ids=row_ids
            )
            if color_sheet == sheet_id and config.node_color_column_id is not None
            else {}
        )
        size_values = (
            project.get_values(
                sheet_id, int(config.node_size_column_id), row_ids=row_ids
            )
            if size_sheet == sheet_id and config.node_size_column_id is not None
            else {}
        )
        for row_id in row_ids:
            acc = nodes[_node_id(sheet_id, row_id)]
            if row_id in label_values:
                acc.label = label_values[row_id]
            if row_id in color_values:
                acc.color_value = color_values[row_id]
            if row_id in size_values:
                acc.size_value = size_values[row_id]


def _resolve_edge_labels(
    project: Project,
    sheet_id: int,
    sheet_name: str,
    edges: list[dict[str, Any]],
    config: GraphViewConfig,
) -> None:
    if not edges:
        return
    label_col = config.edge_label_column_id
    if label_col is None:
        # Default edge label = first text column, else the edge sheet name.
        columns = project.columns(sheet_id)
        text = next((c for c in columns if str(c["type"]) == "text"), None)
        label_col = int(text["id"]) if text is not None else None
    if label_col is None:
        for edge in edges:
            edge["label"] = sheet_name
        return
    row_ids = [int(e["row_id"]) for e in edges]
    values = project.get_values(sheet_id, int(label_col), row_ids=row_ids)
    for edge in edges:
        val = values.get(int(edge["row_id"]))
        edge["label"] = str(val) if val is not None and str(val).strip() else sheet_name


def _envelope(
    *,
    sheet_id: int,
    materialized_kind: str | None,
    direction: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    truncated: bool,
    limit_nodes: int,
    limit_edges: int,
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "materialized_kind": materialized_kind,
        "direction": direction,
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        "limits": {
            "nodes": limit_nodes,
            "edges": limit_edges,
            "returned_nodes": len(nodes),
            "returned_edges": len(edges),
        },
        "diagnostics": diagnostics,
    }

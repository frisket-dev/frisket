"""Read-only FollowTheMoney graph neighborhood projection."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from frisket.features.followthemoney import entities_available
from frisket.engine.store import Project

GRAPH_NEIGHBORHOOD_SCHEMA_VERSION = "frisket.graph_neighborhood.v1"
DEFAULT_REVIEW_STATES = frozenset({"verified", "imported"})

_FTM_REQUIRED_COLUMNS = ("_ftm_id", "_ftm_schema")
_FTM_CAPTION_COLUMN = "_ftm_caption"
_FTM_PROPERTIES_COLUMN = "_ftm_properties_json"
_FTM_DATASET_COLUMN = "_ftm_dataset"
_REVIEW_STATE_COLUMNS = ("review_state", "reviewState")
_CONFIDENCE_COLUMNS = ("confidence",)
_LABEL_COLUMNS = ("name", "title", "full", "summary")


class GraphNeighborhoodError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class _FtmNode:
    id: str
    schema: str | None
    label: str
    sheet_id: int | None
    sheet_name: str | None
    row_id: int | None
    dataset: Any = None
    missing: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "ftm_entity" if not self.missing else "ftm_unresolved_entity",
            "ftm_id": self.id,
            "schema": self.schema,
            "label": self.label,
            "sheet_id": self.sheet_id,
            "row_id": self.row_id,
            "sheet_ref": None,
            "row_ref": None,
        }
        if self.sheet_id is not None:
            payload["sheet_ref"] = {
                "sheet_id": self.sheet_id,
                "sheet_name": self.sheet_name,
                "schema": self.schema,
            }
        if self.sheet_id is not None and self.row_id is not None:
            payload["row_ref"] = {"sheet_id": self.sheet_id, "row_id": self.row_id}
        if self.dataset is not None:
            payload["dataset"] = self.dataset
        if self.missing:
            payload["missing"] = True
        return payload


@dataclass(frozen=True)
class _FtmEdge:
    id: str
    source: str
    target: str
    schema: str
    label: str
    sheet_id: int
    sheet_name: str
    row_id: int
    source_property: str
    target_property: str
    review_state: str
    caption: str | None = None
    confidence: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "ftm_relationship",
            "source": self.source,
            "target": self.target,
            "schema": self.schema,
            "predicate": self.schema,
            "label": self.label,
            "direction": "directed",
            "review_state": self.review_state,
            "confidence": self.confidence,
            "sheet_id": self.sheet_id,
            "row_id": self.row_id,
            "sheet_ref": {
                "sheet_id": self.sheet_id,
                "sheet_name": self.sheet_name,
                "schema": self.schema,
            },
            "row_ref": {"sheet_id": self.sheet_id, "row_id": self.row_id},
            "endpoint_refs": {
                "source": {
                    "property": self.source_property,
                    "ftm_id": self.source,
                    "node_id": self.source,
                },
                "target": {
                    "property": self.target_property,
                    "ftm_id": self.target,
                    "node_id": self.target,
                },
            },
            "properties": self.properties,
        }
        if self.caption is not None:
            payload["caption"] = self.caption
        return payload


def build_graph_neighborhood(
    project: Project,
    *,
    anchor_id: str,
    depth: int = 1,
    review_states: set[str] | frozenset[str] | None = None,
    limit_nodes: int = 200,
    limit_edges: int = 500,
) -> dict[str, Any]:
    """Project a bounded graph neighborhood over imported FtM-shaped sheets."""

    ok, err = entities_available()
    if not ok:
        raise GraphNeighborhoodError("entities_extra_missing", err, status_code=503)

    clean_anchor_id = str(anchor_id or "").strip()
    if not clean_anchor_id:
        raise GraphNeighborhoodError(
            "missing_anchor_id",
            "anchor_id is required",
            status_code=400,
        )
    if depth < 1 or depth > 2:
        raise GraphNeighborhoodError(
            "invalid_depth",
            "graph neighborhood depth must be 1 or 2",
            status_code=400,
        )
    if limit_nodes < 1 or limit_edges < 1:
        raise GraphNeighborhoodError(
            "invalid_limit",
            "graph neighborhood limits must be positive",
            status_code=400,
        )

    nodes_by_id, edges, scan_diagnostics = _scan_ftm_graph(project)
    anchor_node = nodes_by_id.get(clean_anchor_id)
    if anchor_node is None:
        raise GraphNeighborhoodError(
            "anchor_not_found",
            f"FtM entity anchor not found: {clean_anchor_id}",
            status_code=404,
        )

    review_filter = _normalize_review_filter(review_states)
    candidate_edges = [
        edge for edge in edges if _edge_matches_review_filter(edge, review_filter)
    ]
    selected_nodes: OrderedDict[str, _FtmNode] = OrderedDict()
    selected_edges: OrderedDict[str, _FtmEdge] = OrderedDict()
    selected_nodes[anchor_node.id] = anchor_node
    diagnostics: list[dict[str, Any]] = []

    frontier = {anchor_node.id}
    visited = {anchor_node.id}
    for _hop in range(depth):
        next_frontier: set[str] = set()
        for edge in candidate_edges:
            if edge.id in selected_edges:
                continue
            if edge.source not in frontier and edge.target not in frontier:
                continue
            if len(selected_edges) >= limit_edges:
                _add_limit_diagnostic(
                    diagnostics,
                    "edge_limit_reached",
                    "edge limit reached before adding another matching edge",
                    limit=limit_edges,
                )
                break
            endpoint_ids = (edge.source, edge.target)
            missing_endpoint_ids = [
                ftm_id for ftm_id in endpoint_ids if ftm_id not in selected_nodes
            ]
            if len(selected_nodes) + len(set(missing_endpoint_ids)) > limit_nodes:
                _add_limit_diagnostic(
                    diagnostics,
                    "node_limit_reached",
                    "node limit reached before adding another matching edge",
                    limit=limit_nodes,
                )
                continue
            for ftm_id in endpoint_ids:
                if ftm_id not in selected_nodes:
                    selected_nodes[ftm_id] = nodes_by_id.get(
                        ftm_id, _missing_node(ftm_id)
                    )
                if ftm_id not in visited:
                    next_frontier.add(ftm_id)
            selected_edges[edge.id] = edge
        if not next_frontier:
            break
        visited.update(next_frontier)
        frontier = next_frontier

    all_diagnostics = [*scan_diagnostics, *diagnostics]
    return {
        "schema_version": GRAPH_NEIGHBORHOOD_SCHEMA_VERSION,
        "anchor": {
            "kind": "ftm_entity",
            "id": anchor_node.id,
            "node_id": anchor_node.id,
            "schema": anchor_node.schema,
            "label": anchor_node.label,
            "sheet_id": anchor_node.sheet_id,
            "row_id": anchor_node.row_id,
            "row_ref": (
                {"sheet_id": anchor_node.sheet_id, "row_id": anchor_node.row_id}
                if anchor_node.sheet_id is not None and anchor_node.row_id is not None
                else None
            ),
        },
        "nodes": [node.to_payload() for node in selected_nodes.values()],
        "edges": [edge.to_payload() for edge in selected_edges.values()],
        "truncated": any(
            item["code"] in {"node_limit_reached", "edge_limit_reached"}
            for item in all_diagnostics
        ),
        "limits": {
            "depth": depth,
            "nodes": limit_nodes,
            "edges": limit_edges,
            "returned_nodes": len(selected_nodes),
            "returned_edges": len(selected_edges),
        },
        "diagnostics": all_diagnostics,
    }


def _schema_metadata(schema_name: str) -> dict[str, Any] | None:
    """Lazy accessor: only touches the ``entities`` extra's real submodule
    once the caller has already confirmed ``entities_available()`` (the
    module-top import stays a cheap probe, never a hard dependency)."""
    from frisket.features.followthemoney.schema_catalog import get_schema_metadata

    return get_schema_metadata(schema_name)


def _scan_ftm_graph(
    project: Project,
) -> tuple[dict[str, _FtmNode], list[_FtmEdge], list[dict[str, Any]]]:
    nodes_by_id: dict[str, _FtmNode] = {}
    edges: list[_FtmEdge] = []
    diagnostics: list[dict[str, Any]] = []

    for sheet in project.sheets(include_hidden=False):
        sheet_id = int(sheet["id"])
        columns = project.columns(sheet_id, include_hidden=True)
        columns_by_name = {str(column["name"]): column for column in columns}
        if any(name not in columns_by_name for name in _FTM_REQUIRED_COLUMNS):
            continue
        row_ids = project.visible_row_ids(sheet_id)
        if not row_ids:
            continue
        values_by_name = _sheet_values(
            project,
            sheet_id=sheet_id,
            columns_by_name=columns_by_name,
            row_ids=row_ids,
            names=_base_value_column_names(columns_by_name),
        )
        extra_column_names = _extra_value_column_names(
            columns_by_name=columns_by_name,
            schema_values=values_by_name["_ftm_schema"],
        )
        values_by_name.update(
            _sheet_values(
                project,
                sheet_id=sheet_id,
                columns_by_name=columns_by_name,
                row_ids=row_ids,
                names=extra_column_names - set(values_by_name),
            )
        )
        label_column_names = [name for name in _LABEL_COLUMNS if name in values_by_name]
        for row_id in row_ids:
            ftm_id = _string_value(values_by_name["_ftm_id"].get(row_id))
            schema_name = _string_value(values_by_name["_ftm_schema"].get(row_id))
            if ftm_id is None or schema_name is None:
                diagnostics.append(
                    _diagnostic(
                        "invalid_ftm_metadata",
                        "FtM-shaped row is missing _ftm_id or _ftm_schema",
                        sheet_id=sheet_id,
                        row_id=row_id,
                    )
                )
                continue
            metadata = _schema_metadata(schema_name)
            if _is_relationship_metadata(metadata):
                edge = _edge_from_row(
                    ftm_id=ftm_id,
                    schema_name=schema_name,
                    metadata=metadata or {},
                    sheet_id=sheet_id,
                    sheet_name=str(sheet["name"]),
                    row_id=row_id,
                    values_by_name=values_by_name,
                )
                if edge is None:
                    diagnostics.append(
                        _diagnostic(
                            "relationship_endpoint_missing",
                            "FtM relationship row is missing source or target endpoint",
                            sheet_id=sheet_id,
                            row_id=row_id,
                            schema=schema_name,
                            ftm_id=ftm_id,
                        )
                    )
                    continue
                edges.append(edge)
                continue
            node = _node_from_row(
                ftm_id=ftm_id,
                schema_name=schema_name,
                sheet_id=sheet_id,
                sheet_name=str(sheet["name"]),
                row_id=row_id,
                values_by_name=values_by_name,
                label_column_names=label_column_names,
            )
            nodes_by_id.setdefault(node.id, node)

    return nodes_by_id, edges, diagnostics


def _base_value_column_names(columns_by_name: dict[str, Any]) -> set[str]:
    names = {
        *_FTM_REQUIRED_COLUMNS,
        _FTM_CAPTION_COLUMN,
        _FTM_PROPERTIES_COLUMN,
        _FTM_DATASET_COLUMN,
        *_REVIEW_STATE_COLUMNS,
        *_CONFIDENCE_COLUMNS,
    }
    return {name for name in names if name in columns_by_name}


def _extra_value_column_names(
    *,
    columns_by_name: dict[str, Any],
    schema_values: dict[int, Any],
) -> set[str]:
    names: set[str] = set()
    for raw_schema in schema_values.values():
        schema_name = _string_value(raw_schema)
        if schema_name is None:
            continue
        metadata = _schema_metadata(schema_name)
        if _is_relationship_metadata(metadata):
            for endpoint_name in (
                _string_value((metadata or {}).get("source_property")),
                _string_value((metadata or {}).get("target_property")),
            ):
                if endpoint_name is not None and endpoint_name in columns_by_name:
                    names.add(endpoint_name)
        else:
            names.update(name for name in _LABEL_COLUMNS if name in columns_by_name)
    return names


def _sheet_values(
    project: Project,
    *,
    sheet_id: int,
    columns_by_name: dict[str, Any],
    row_ids: list[int],
    names: set[str],
) -> dict[str, dict[int, Any]]:
    return {
        name: project.get_values(
            sheet_id,
            int(columns_by_name[name]["id"]),
            row_ids=row_ids,
        )
        for name in names
        if name in columns_by_name
    }


def _node_from_row(
    *,
    ftm_id: str,
    schema_name: str,
    sheet_id: int,
    sheet_name: str,
    row_id: int,
    values_by_name: dict[str, dict[int, Any]],
    label_column_names: list[str],
) -> _FtmNode:
    properties = _dict_value(values_by_name.get(_FTM_PROPERTIES_COLUMN, {}).get(row_id))
    caption = _string_value(values_by_name.get(_FTM_CAPTION_COLUMN, {}).get(row_id))
    label = (
        caption
        or _label_from_properties(properties)
        or _label_from_visible_values(row_id, values_by_name, label_column_names)
    )
    dataset = values_by_name.get(_FTM_DATASET_COLUMN, {}).get(row_id)
    return _FtmNode(
        id=ftm_id,
        schema=schema_name,
        label=label or ftm_id,
        sheet_id=sheet_id,
        sheet_name=sheet_name,
        row_id=row_id,
        dataset=dataset,
    )


def _edge_from_row(
    *,
    ftm_id: str,
    schema_name: str,
    metadata: dict[str, Any],
    sheet_id: int,
    sheet_name: str,
    row_id: int,
    values_by_name: dict[str, dict[int, Any]],
) -> _FtmEdge | None:
    source_property = _string_value(metadata.get("source_property"))
    target_property = _string_value(metadata.get("target_property"))
    if source_property is None or target_property is None:
        return None
    properties = _dict_value(values_by_name.get(_FTM_PROPERTIES_COLUMN, {}).get(row_id))
    source_ftm_id = _endpoint_value(
        row_id, source_property, values_by_name=values_by_name, properties=properties
    )
    target_ftm_id = _endpoint_value(
        row_id, target_property, values_by_name=values_by_name, properties=properties
    )
    if source_ftm_id is None or target_ftm_id is None:
        return None
    review_state = _review_state(row_id, values_by_name)
    edge_label = _string_value(metadata.get("edge_label")) or _string_value(
        metadata.get("label")
    )
    caption = _string_value(values_by_name.get(_FTM_CAPTION_COLUMN, {}).get(row_id))
    return _FtmEdge(
        id=ftm_id,
        source=source_ftm_id,
        target=target_ftm_id,
        schema=schema_name,
        label=edge_label or schema_name,
        sheet_id=sheet_id,
        sheet_name=sheet_name,
        row_id=row_id,
        source_property=source_property,
        target_property=target_property,
        review_state=review_state,
        caption=caption,
        confidence=_confidence(row_id, values_by_name),
    )


def _is_relationship_metadata(metadata: dict[str, Any] | None) -> bool:
    return bool(
        metadata
        and metadata.get("edge")
        and metadata.get("source_property")
        and metadata.get("target_property")
    )


def _normalize_review_filter(
    review_states: set[str] | frozenset[str] | None,
) -> frozenset[str] | None:
    if review_states is None:
        return DEFAULT_REVIEW_STATES
    normalized = frozenset(
        state.strip().lower() for state in review_states if state and state.strip()
    )
    if "all" in normalized:
        return None
    return normalized


def _edge_matches_review_filter(
    edge: _FtmEdge, review_filter: frozenset[str] | None
) -> bool:
    if review_filter is None:
        return True
    return edge.review_state.lower() in review_filter


def _review_state(row_id: int, values_by_name: dict[str, dict[int, Any]]) -> str:
    for name in _REVIEW_STATE_COLUMNS:
        state = _string_value(values_by_name.get(name, {}).get(row_id))
        if state is not None:
            return state.strip().lower() or "unreviewed"
    return "imported"


def _confidence(row_id: int, values_by_name: dict[str, dict[int, Any]]) -> float | None:
    for name in _CONFIDENCE_COLUMNS:
        value = values_by_name.get(name, {}).get(row_id)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _endpoint_value(
    row_id: int,
    property_name: str,
    *,
    values_by_name: dict[str, dict[int, Any]],
    properties: dict[str, Any],
) -> str | None:
    value = values_by_name.get(property_name, {}).get(row_id)
    if value is None:
        value = properties.get(property_name)
    return _string_value(_first_value(value))


def _label_from_visible_values(
    row_id: int,
    values_by_name: dict[str, dict[int, Any]],
    label_column_names: list[str],
) -> str | None:
    for name in label_column_names:
        label = _string_value(values_by_name.get(name, {}).get(row_id))
        if label:
            return label
    return None


def _label_from_properties(properties: dict[str, Any]) -> str | None:
    for name in ("name", "title", "full", "summary"):
        label = _string_value(_first_value(properties.get(name)))
        if label:
            return label
    return None


def _first_value(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _dict_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _missing_node(ftm_id: str) -> _FtmNode:
    return _FtmNode(
        id=ftm_id,
        schema=None,
        label=ftm_id,
        sheet_id=None,
        sheet_name=None,
        row_id=None,
        missing=True,
    )


def _diagnostic(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        **{key: value for key, value in details.items() if value is not None},
    }


def _add_limit_diagnostic(
    diagnostics: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    limit: int,
) -> None:
    if any(item["code"] == code for item in diagnostics):
        return
    diagnostics.append(_diagnostic(code, message, limit=limit))

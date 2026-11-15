"""A concrete composable selection, separate from view presentation."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from frisket.features.watchlists.specs import canonical_json
from frisket.querysets import sheet_row_scope_plan


class SelectionUnavailable(ValueError):
    def __init__(self, message: str, *, code: str = "selection_unavailable"):
        super().__init__(message)
        self.code = code


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def normalize_selection(project: Any, value: dict[str, Any]) -> dict[str, Any]:
    """Validate and retain stable column identities; never bind by a new name."""
    from frisket.contracts.view import ViewSelection

    if isinstance(value, ViewSelection):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        raise ValueError("selection must be an object")
    unknown = set(value) - {
        "scope",
        "filters",
        "search",
        "similarity",
        "order",
        "limit",
        "ranking",
        "rank_constant",
    }
    if unknown:
        raise ValueError(f"unknown selection fields: {', '.join(sorted(unknown))}")
    scope = value.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") not in {"sheet", "project"}:
        raise ValueError("selection requires a sheet or project scope")
    scope = deepcopy(scope)
    if scope["kind"] == "sheet":
        sheet = _positive(scope.get("sheet_id"), "sheet_id")
        if (
            project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet,)
            ).fetchone()
            is None
        ):
            raise SelectionUnavailable("The view's sheet is unavailable.")
        if set(scope) - {"kind", "sheet_id", "row_ids", "parent_row_id"}:
            raise ValueError("unknown sheet scope fields")
        if "parent_row_id" in scope:
            _positive(scope["parent_row_id"], "parent_row_id")
        if "row_ids" in scope:
            ids = scope["row_ids"]
            if not isinstance(ids, list):
                raise ValueError("row_ids must be a list")
            scope["row_ids"] = sorted({_positive(row, "row_id") for row in ids})
    elif set(scope) != {"kind"}:
        raise ValueError("project scope has no sheet restrictions")
    filters = value.get("filters", [])
    order = value.get("order", [])
    if not isinstance(filters, list) or not isinstance(order, list):
        raise ValueError("filters and order must be lists")
    for condition in filters:
        if not isinstance(condition, dict) or set(condition) != {
            "column_id",
            "condition",
        }:
            raise ValueError("filters require column_id and condition")
        _positive(condition["column_id"], "column_id")
        if not isinstance(condition["condition"], dict):
            raise ValueError("filter condition must be an object")
    column_order = []
    for item in order:
        if not isinstance(item, dict) or item.get("dir", "asc") not in {"asc", "desc"}:
            raise ValueError("order entries require asc or desc direction")
        if "column_id" in item and set(item) <= {"column_id", "dir"}:
            _positive(item["column_id"], "column_id")
            column_order.append(item)
        elif (
            "score" in item
            and set(item) <= {"score", "dir"}
            and item["score"] in {"search", "similarity"}
        ):
            if not value.get(item["score"]):
                raise ValueError(
                    f"{item['score']} ordering requires that selection criterion"
                )
        else:
            raise ValueError("order requires column_id or a query score")
    if (filters or column_order) and scope["kind"] != "sheet":
        raise ValueError("column filters and ordering require a sheet scope")
    if scope["kind"] == "sheet":
        sheet_row_scope_plan(
            project,
            scope["sheet_id"],
            filter_=canonical_json(filters),
            sort=canonical_json(column_order),
            row_ids=scope.get("row_ids"),
            parent_row_id=scope.get("parent_row_id"),
        )
    out = {
        "scope": scope,
        "filters": deepcopy(filters),
        "order": deepcopy(order),
        "limit": value.get("limit"),
    }
    if out["limit"] is not None:
        _positive(out["limit"], "limit")
    search = value.get("search")
    if search is not None:
        if (
            not isinstance(search, dict)
            or set(search) != {"text"}
            or not isinstance(search["text"], str)
            or not search["text"].strip()
        ):
            raise ValueError("search requires nonempty text")
        out["search"] = {"text": search["text"].strip()}
    if value.get("similarity") is not None:
        from frisket.features.watchlists.specs import normalize_query_spec

        query = normalize_query_spec(value["similarity"], allow_row_cell=True)
        if query["kind"] != "embedding_similarity":
            raise ValueError("similarity requires an embedding similarity query")
        query.pop("limit", None)
        out["similarity"] = query
    if value.get("ranking") is not None:
        if (
            value["ranking"] != "hybrid"
            or "search" not in out
            or "similarity" not in out
        ):
            raise ValueError("hybrid ranking requires search and similarity")
        out["ranking"] = "hybrid"
        out["rank_constant"] = _positive(
            value.get("rank_constant", 60), "rank_constant"
        )
    elif "rank_constant" in value:
        raise ValueError("rank_constant requires hybrid ranking")
    return out


def membership_hash(selection: dict[str, Any]) -> str:
    criteria = deepcopy(selection)
    if criteria.get("limit") is None:
        criteria.pop("order", None)
    return "sha256:" + hashlib.sha256(canonical_json(criteria).encode()).hexdigest()


def legacy_selection(
    project: Any, *, sheet_id: int | None, spec: dict[str, Any]
) -> dict[str, Any]:
    """One-time conversion helper; invalid definitions remain available to repair."""
    query = spec.get("query", spec)
    if not isinstance(query, dict):
        raise ValueError("saved query is not an object")
    scope = query.get("scope") or (
        {"kind": "sheet", "sheet_id": sheet_id}
        if sheet_id is not None
        else {"kind": "project"}
    )
    if query.get("sheet_id") is not None:
        scope = {"kind": "sheet", "sheet_id": query["sheet_id"]}
    filters = query.get("filter", {})
    columns = (
        {
            str(col["name"]): int(col["id"])
            for col in project.columns(scope["sheet_id"], include_hidden=True)
            if col["active"]
        }
        if scope.get("sheet_id")
        else {}
    )
    selection: dict[str, Any] = {
        "scope": scope,
        "filters": [],
        "order": [],
        "limit": query.get("limit"),
    }
    if not isinstance(filters, dict):
        raise ValueError("saved filter is not an object")
    for name, condition in filters.items():
        if name not in columns:
            raise SelectionUnavailable(f"Filter column {name!r} is unavailable.")
        selection["filters"].append(
            {"column_id": columns[name], "condition": condition}
        )
    for item in query.get("sort", []):
        if item.get("column") not in columns:
            raise SelectionUnavailable(
                f"Sort column {item.get('column')!r} is unavailable."
            )
        selection["order"].append(
            {"column_id": columns[item["column"]], "dir": item.get("dir", "asc")}
        )
    if "scope_row_ids" in spec:
        selection["scope"]["row_ids"] = spec["scope_row_ids"]
    kind = query.get("kind", "sheet.filter")
    if kind in {"fts", "search.fts"}:
        selection["search"] = {"text": query["q"]}
        selection["order"] = [{"score": "search", "dir": "desc"}]
    elif kind == "embedding_similarity":
        selection["similarity"] = query
        selection["order"] = [{"score": "similarity", "dir": "desc"}]
    elif kind == "embedding_hybrid":
        selection["search"] = {"text": query["text"]}
        selection["similarity"] = {
            "kind": "embedding_similarity",
            "scope": scope,
            "embedding_index_id": query["embedding_index_id"],
            "anchor": {"kind": "manual_text_query", "text": query["text"]},
        }
        selection["ranking"] = "hybrid"
        selection["rank_constant"] = query.get("k_rrf", 60)
    elif kind not in {"filter", "sheet.filter"}:
        raise SelectionUnavailable(f"Saved selection kind {kind!r} needs conversion.")
    return normalize_selection(project, selection)


def normalize_view_spec(project: Any, spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict) or set(spec) - {"selection", "presentation"}:
        raise ValueError("view requires selection and optional presentation")
    presentation = spec.get("presentation", {})
    if not isinstance(presentation, dict):
        raise ValueError("presentation must be an object")
    if set(presentation) - {"columns", "column_groups"}:
        raise ValueError("unknown view presentation fields")
    for name, value in presentation.items():
        if value is not None and not isinstance(value, list):
            raise ValueError(f"{name} must be a list or null")
    return {
        "selection": normalize_selection(project, spec.get("selection")),
        "presentation": deepcopy(presentation),
    }


def stored_selection(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("unavailable_reason"):
        raise SelectionUnavailable(str(spec["unavailable_reason"]))
    selection = spec.get("selection")
    if not isinstance(selection, dict):
        raise SelectionUnavailable("This view has no usable selection definition.")
    return selection

"""Resolve Ask's saved source handles against current project content."""

from __future__ import annotations

from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQANotFoundError, ProjectQAStore


def resolve_citation(
    project: Project, thread_id: str, citation_id: str
) -> dict[str, Any]:
    store = ProjectQAStore(project)
    citation = store.get_citation(citation_id)
    if store.get_turn(citation["turn_id"])["thread_id"] != thread_id:
        raise ProjectQANotFoundError("Source not found in this conversation.")
    result = {
        "id": citation_id,
        "label": citation["label"],
        "source_kind": citation["source_kind"],
        "excerpt": citation["excerpt"],
        "status": "unavailable",
        "message": "This source is no longer available in the project.",
        "target": None,
    }
    locator = citation["locator"]
    if citation["source_kind"] == "query":
        if not project.db.execute(
            "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (locator.get("sheet_id"),)
        ).fetchone():
            return result
        from frisket.server.services.project_qa_query import evaluate_query

        try:
            current = evaluate_query(
                project, locator["query"], locator["scope"], limit=0
            )
        except (ValueError, KeyError):
            return result
        changed = project.op_cursor != locator.get("source_op_cursor")
        return {
            **result,
            "status": "changed" if changed else "current",
            "message": "The project has changed since this query. These results use current data."
            if changed
            else None,
            "target": {
                "kind": "query",
                "sheet_id": current["sheet_id"],
                "row_ids": current["scope"].get("row_ids"),
                "filter": current["query"].get("filter", {}),
                "sort": current["query"].get("sort"),
                "total": current["total"],
            },
        }
    if citation["source_kind"] not in {"cell", "evidence"}:
        return result
    sheet_id, row_id, column_id = (
        locator.get(key) for key in ("sheet_id", "row_id", "column_id")
    )
    sheet = next(
        (
            s
            for s in project.sheets()
            if s["id"] == sheet_id and not str(s["name"]).startswith("(undone:")
        ),
        None,
    )
    if sheet is None or not project.visible_row_ids(sheet_id, [row_id]):
        return result
    column = next((c for c in project.columns(sheet_id) if c["id"] == column_id), None)
    if column is None:
        return result
    values, refs = project.get_values_with_refs(
        sheet_id, column_id, row_ids=[row_id], include_validity=True
    )
    if row_id not in values:
        return result
    saved_ref = locator.get("value_ref")
    status = (
        "unverified"
        if saved_ref is None
        else "current"
        if saved_ref == refs.get(row_id)
        else "changed"
    )
    target = {
        "kind": "cell",
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
    }
    if citation["source_kind"] == "evidence":
        from frisket.engine.store.evidence import resolve_evidence_viewer

        try:
            viewer = resolve_evidence_viewer(project, locator["evidence_link_id"])
        except KeyError:
            return result
        span_exists = any(
            artifact["stable_id"] == locator.get("artifact_id")
            and any(
                span["stable_id"] == locator.get("span_id")
                for span in artifact["spans"]
            )
            for artifact in viewer["artifacts"]
        )
        if not span_exists:
            return result
        if (
            viewer["link"]["status"] != "active"
            or viewer["link"]["text_layer_hash_mismatch"]
        ):
            status = "changed"
        target = {
            **target,
            "kind": "evidence",
            **{
                key: locator[key]
                for key in ("evidence_link_id", "artifact_id", "span_id")
            },
        }
    return {
        **result,
        "label": f"{sheet['name']} · row {row_id} · {column['name']}",
        "status": status,
        "message": {
            "current": None,
            "changed": "This value has changed since the answer was written.",
            "unverified": "The original value cannot be verified.",
        }[status],
        "target": target,
    }

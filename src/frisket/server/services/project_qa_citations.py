"""Resolve Ask's saved source handles against current project content."""

from __future__ import annotations

from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQANotFoundError, ProjectQAStore
from frisket.server.services.project_qa_sources import (
    read_source_text,
    resolve_prepared_source,
)
from frisket.server.services.project_qa_web import safe_web_text, safe_web_url


def project_qa_safe_citation_projection(citation: dict[str, Any]) -> dict[str, Any]:
    """Return the safe label, URL, and excerpt shared by reports and clients."""

    label = safe_web_text(citation.get("label"), limit=240)
    excerpt = citation.get("excerpt")
    if citation.get("source_kind") == "web":
        locator = citation.get("locator")
        url = safe_web_url(locator.get("url")) if isinstance(locator, dict) else None
        return {
            "label": label or "Web source",
            "url": url,
            "excerpt": safe_web_text(excerpt, limit=8_000) if excerpt else None,
        }
    return {
        "label": label,
        "url": None,
        "excerpt": safe_web_text(excerpt, limit=8_000) if excerpt else None,
    }


def resolve_citation(
    project: Project, thread_id: str, citation_id: str
) -> dict[str, Any]:
    store = ProjectQAStore(project)
    citation = store.get_citation(citation_id)
    if store.get_turn(citation["turn_id"])["thread_id"] != thread_id:
        raise ProjectQANotFoundError("Source not found in this conversation.")
    projected = project_qa_safe_citation_projection(citation)
    result = {
        "id": citation_id,
        "label": projected["label"],
        "source_kind": citation["source_kind"],
        "excerpt": projected["excerpt"],
        "status": "unavailable",
        "message": "This source is no longer available in the project.",
        "target": None,
    }
    locator = citation["locator"]
    if citation["source_kind"] == "web":
        url = projected["url"]
        retrieved_at = locator.get("retrieved_at")
        fetched = locator.get("fetched")
        if (
            not isinstance(url, str)
            or not isinstance(retrieved_at, str)
            or not isinstance(fetched, bool)
        ):
            return result
        return {
            **result,
            "status": "unverified",
            "message": "This public-web source was retrieved at the recorded time and may have changed.",
            "target": {
                "kind": "web",
                "url": url,
                "retrieved_at": retrieved_at,
                "fetched": fetched,
            },
        }
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
    saved_version = locator.get("source_version")
    if saved_version is not None and locator.get("prepared_cell") is None:
        current_version = read_source_text(
            project, (sheet_id, row_id, column_id), limit=1
        )["version"]
        status = "current" if saved_version == current_version else "changed"
    else:
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
    prepared_cell = locator.get("prepared_cell")
    prepared_ref = locator.get("prepared_value_ref")
    prepared_link_id = locator.get("prepared_evidence_link_id")
    if (
        prepared_cell is not None
        or prepared_ref is not None
        or prepared_link_id is not None
    ):
        try:
            expected_cell = tuple(
                int(prepared_cell[key]) for key in ("sheet_id", "row_id", "column_id")
            )
            original = read_source_text(project, (sheet_id, row_id, column_id), limit=1)
            prepared = resolve_prepared_source(
                project,
                (sheet_id, row_id, column_id),
                expected_version=original["version"],
                evidence_link_id=str(prepared_link_id),
            )
        except (KeyError, TypeError, ValueError):
            prepared = None
            expected_cell = None
        if (
            prepared is None
            or prepared["cell"] != expected_cell
            or prepared["prepared_value_ref"] != prepared_ref
            or (
                saved_version is not None
                and saved_version != prepared["prepared_version"]
            )
        ):
            status = "changed"
        else:
            target = {
                "kind": "cell",
                "sheet_id": prepared["cell"][0],
                "row_id": prepared["cell"][1],
                "column_id": prepared["cell"][2],
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
        "label": safe_web_text(
            f"{sheet['name']} · row {row_id} · {column['name']}", limit=240
        ),
        "status": status,
        "message": {
            "current": None,
            "changed": "This value has changed since the answer was written.",
            "unverified": "The original value cannot be verified.",
        }[status],
        "target": target,
    }

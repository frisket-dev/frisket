"""Resolve Ask's saved source handles against current project content."""

from __future__ import annotations

from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQANotFoundError, ProjectQAStore
from frisket.server.services.project_qa_sources import (
    read_source_text,
    read_source_with_prepared,
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


def _unavailable_result(
    citation_id: str, citation: dict[str, Any], projected: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": citation_id,
        "label": projected["label"],
        "source_kind": citation["source_kind"],
        "excerpt": projected["excerpt"],
        "status": "unavailable",
        "message": "This source is no longer available in the project.",
        "target": None,
    }


def _resolve_web_citation(
    result: dict[str, Any], locator: dict[str, Any], url: object
) -> dict[str, Any]:
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


def _resolve_query_citation(
    project: Project, result: dict[str, Any], locator: dict[str, Any]
) -> dict[str, Any]:
    if not project.db.execute(
        "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (locator.get("sheet_id"),)
    ).fetchone():
        return result
    from frisket.server.services.project_qa_query import evaluate_query

    try:
        current = evaluate_query(project, locator["query"], locator["scope"], limit=0)
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


def _resolve_prepared_target(
    project: Project,
    source_cell: tuple[int, int, int],
    locator: dict[str, Any],
    target: dict[str, Any],
    status: str,
) -> tuple[dict[str, Any], str]:
    prepared_cell = locator.get("prepared_cell")
    prepared_ref = locator.get("prepared_value_ref")
    prepared_link_id = locator.get("prepared_evidence_link_id")
    if prepared_cell is None and prepared_ref is None and prepared_link_id is None:
        return target, status
    try:
        expected_cell = tuple(
            int(prepared_cell[key]) for key in ("sheet_id", "row_id", "column_id")
        )
        _cell, _source, prepared = read_source_with_prepared(
            project,
            source_cell,
            evidence_link_id=str(prepared_link_id),
        )
    except (KeyError, TypeError, ValueError):
        return target, "changed"
    if (
        prepared is None
        or prepared["cell"] != expected_cell
        or prepared["prepared_value_ref"] != prepared_ref
        or (
            locator.get("source_version") is not None
            and locator["source_version"] != prepared["prepared_version"]
        )
    ):
        return target, "changed"
    return {
        "kind": "cell",
        "sheet_id": prepared["cell"][0],
        "row_id": prepared["cell"][1],
        "column_id": prepared["cell"][2],
    }, status


def _resolve_evidence_target(
    project: Project,
    locator: dict[str, Any],
    target: dict[str, Any],
    status: str,
) -> tuple[dict[str, Any], str] | None:
    from frisket.engine.store.evidence import resolve_evidence_viewer

    try:
        viewer = resolve_evidence_viewer(project, locator["evidence_link_id"])
    except KeyError:
        return None
    span_exists = any(
        artifact["stable_id"] == locator.get("artifact_id")
        and any(
            span["stable_id"] == locator.get("span_id") for span in artifact["spans"]
        )
        for artifact in viewer["artifacts"]
    )
    if not span_exists:
        return None
    if (
        viewer["link"]["status"] != "active"
        or viewer["link"]["text_layer_hash_mismatch"]
    ):
        status = "changed"
    return {
        **target,
        "kind": "evidence",
        **{key: locator[key] for key in ("evidence_link_id", "artifact_id", "span_id")},
    }, status


def _binding_dependencies_current(project: Project, dependencies: object) -> bool:
    if not isinstance(dependencies, list) or not dependencies:
        return False
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return False
        row_id = dependency.get("row_id")
        column_id = dependency.get("column_id")
        revision = dependency.get("retrieval_revision")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (row_id, column_id, revision)
        ):
            return False
        if (
            project.db.execute(
                "SELECT 1 FROM current_cells WHERE row_id=? AND column_id=? "
                "AND retrieval_revision=?",
                (row_id, column_id, revision),
            ).fetchone()
            is None
        ):
            return False
    return True


def _resolve_project_citation(
    project: Project,
    citation: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    locator = citation["locator"]
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
    try:
        current_source = read_source_text(
            project, (sheet_id, row_id, column_id), limit=1
        )
    except ValueError:
        return result
    saved_ref = locator.get("value_ref")
    saved_version = locator.get("source_version")
    if saved_version is not None and locator.get("prepared_cell") is None:
        status = "current" if saved_version == current_source["version"] else "changed"
    else:
        status = (
            "unverified"
            if saved_ref is None
            else "current"
            if saved_ref == current_source["value_ref"]
            else "changed"
        )
    dependencies = locator.get("binding_dependencies")
    if dependencies is not None and not _binding_dependencies_current(
        project, dependencies
    ):
        status = "changed"
    target = {
        "kind": "cell",
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
    }
    target, status = _resolve_prepared_target(
        project, (sheet_id, row_id, column_id), locator, target, status
    )
    if citation["source_kind"] == "evidence":
        evidence = _resolve_evidence_target(project, locator, target, status)
        if evidence is None:
            return result
        target, status = evidence
    elif status == "current":
        navigation = locator.get("navigation")
        if isinstance(navigation, dict) and navigation.get("kind") == "document":
            target["navigation"] = navigation
        location = locator.get("location")
        if isinstance(location, dict) and isinstance(location.get("text"), dict):
            target["text_range"] = location["text"]
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


def resolve_citation(
    project: Project, thread_id: str, citation_id: str
) -> dict[str, Any]:
    store = ProjectQAStore(project)
    citation = store.get_citation(citation_id)
    if store.get_turn(citation["turn_id"])["thread_id"] != thread_id:
        raise ProjectQANotFoundError("Source not found in this conversation.")
    projected = project_qa_safe_citation_projection(citation)
    result = _unavailable_result(citation_id, citation, projected)
    locator = citation["locator"]
    if citation["source_kind"] == "web":
        return _resolve_web_citation(result, locator, projected["url"])
    if citation["source_kind"] == "query":
        return _resolve_query_citation(project, result, locator)
    if citation["source_kind"] in {"cell", "evidence"}:
        return _resolve_project_citation(project, citation, result)
    return result

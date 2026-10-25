"""Bounded source-document projection for one materialized row."""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.store.evidence import list_row_evidence
from frisket.engine.store.staleness import sheet_is_stale


MAX_SOURCE_ANCESTRY_DEPTH = 16
MAX_SOURCE_ANCESTRY_ROWS = 256
MAX_SOURCE_DOCUMENTS = 64
MAX_SOURCE_EVIDENCE_LINKS = 64
SQLITE_ID_CHUNK_SIZE = 500
DOCUMENT_COLUMN_TYPES = ("file", "image")


class RowSourceNotFound(KeyError):
    """The requested row is not a visible row in this project."""


def resolve_row_source_documents(
    project: Any,
    *,
    row_id: int,
    project_id: str,
) -> dict[str, Any]:
    """Resolve existing evidence and readable document ancestors for ``row_id``.

    ``materialized_row_sources`` is authoritative whenever a row has canonical
    memberships (notably both sides of a join). ``rows.parent_row_id`` is only
    the legacy/single-parent fallback. The walk is deliberately bounded and
    cycle safe; it never manufactures evidence from ancestry.
    """

    requested = _visible_row(project, row_id)
    if requested is None:
        raise RowSourceNotFound(row_id)

    requested_sheet_id = int(requested["sheet_id"])
    lineage_stale = sheet_is_stale(project, requested_sheet_id)

    evidence_links: list[dict[str, Any]] = []
    seen_evidence: set[str] = set()
    documents: list[dict[str, Any]] = []
    document_locations: dict[str, tuple[int, int]] = {}
    unavailable_sources: set[int] = set()
    visited: set[int] = {row_id}
    ancestry_found = False
    truncated = False

    truncated = _extend_evidence(
        project,
        sheet_id=requested_sheet_id,
        row_id=row_id,
        project_id=project_id,
        out=evidence_links,
        seen=seen_evidence,
    )

    frontier = [row_id]
    depth = 0
    while frontier:
        remaining = MAX_SOURCE_ANCESTRY_ROWS - len(visited)
        if remaining <= 0:
            truncated = True
            break
        candidate_ids, sources_truncated = _immediate_source_ids(
            project,
            frontier,
            visited=visited,
            limit=remaining,
        )
        stop_after_frontier = sources_truncated
        truncated = truncated or sources_truncated
        if depth == 0:
            ancestry_found = bool(candidate_ids) or _row_has_immediate_source(
                project, row_id
            )
        if not candidate_ids:
            break
        if depth >= MAX_SOURCE_ANCESTRY_DEPTH:
            truncated = True
            break
        if len(candidate_ids) > remaining:
            candidate_ids = candidate_ids[:remaining]
            truncated = True
            stop_after_frontier = True
        visible = _visible_rows(project, candidate_ids)
        next_frontier: list[int] = []
        for source_row_id in candidate_ids:
            visited.add(source_row_id)
            source_row = visible.get(source_row_id)
            if source_row is None:
                unavailable_sources.add(source_row_id)
                continue

            source_sheet_id = int(source_row["sheet_id"])
            truncated = (
                _extend_evidence(
                    project,
                    sheet_id=source_sheet_id,
                    row_id=source_row_id,
                    project_id=project_id,
                    out=evidence_links,
                    seen=seen_evidence,
                )
                or truncated
            )
            for document, identity in _row_documents(project, source_row):
                existing = document_locations.get(identity)
                if existing is None:
                    if len(documents) >= MAX_SOURCE_DOCUMENTS:
                        truncated = True
                    else:
                        document_locations[identity] = (depth + 1, len(documents))
                        documents.append(document)
                elif depth + 1 > existing[0]:
                    documents[existing[1]] = document
                    document_locations[identity] = (depth + 1, existing[1])
            next_frontier.append(source_row_id)
        if stop_after_frontier:
            break
        frontier = next_frontier
        depth += 1

    return {
        "schema_version": "frisket.row_source_documents.v1",
        "sheet_id": requested_sheet_id,
        "row_id": row_id,
        "ancestry_found": ancestry_found,
        "lineage_stale": lineage_stale,
        "unavailable_source_count": len(unavailable_sources),
        "truncated": truncated,
        "evidence_links": evidence_links,
        "documents": documents,
    }


def _visible_row(project: Any, row_id: int) -> Any | None:
    return project.db.execute(
        "SELECT r.id,r.sheet_id,r.position,s.name AS sheet_name "
        "FROM rows r JOIN sheets s ON s.id=r.sheet_id "
        "WHERE r.id=? AND r.hidden=0 AND s.hidden=0",
        (row_id,),
    ).fetchone()


def _visible_rows(project: Any, row_ids: list[int]) -> dict[int, Any]:
    rows: dict[int, Any] = {}
    for chunk in _chunks(row_ids):
        placeholders = ",".join("?" for _ in chunk)
        for row in project.db.execute(
            "SELECT r.id,r.sheet_id,r.position,s.name AS sheet_name "
            "FROM rows r JOIN sheets s ON s.id=r.sheet_id "
            f"WHERE r.id IN ({placeholders}) AND r.hidden=0 AND s.hidden=0",
            chunk,
        ):
            rows[int(row["id"])] = row
    return rows


def _immediate_source_ids(
    project: Any,
    row_ids: list[int],
    *,
    visited: set[int],
    limit: int,
) -> tuple[list[int], bool]:
    row_placeholders = ",".join("?" for _ in row_ids)
    visited_ids = sorted(visited)
    visited_placeholders = ",".join("?" for _ in visited_ids)
    source_rows = project.db.execute(
        "SELECT DISTINCT source_row_id FROM ("
        "SELECT m.source_row_id FROM materialized_row_sources m "
        f"WHERE m.materialized_row_id IN ({row_placeholders}) "
        "UNION ALL "
        "SELECT r.parent_row_id AS source_row_id FROM rows r "
        f"WHERE r.id IN ({row_placeholders}) AND r.parent_row_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM materialized_row_sources m "
        "WHERE m.materialized_row_id=r.id)"
        f") WHERE source_row_id NOT IN ({visited_placeholders}) "
        "ORDER BY source_row_id LIMIT ?",
        (*row_ids, *row_ids, *visited_ids, limit + 1),
    ).fetchall()
    truncated = len(source_rows) > limit
    return [int(row["source_row_id"]) for row in source_rows[:limit]], truncated


def _row_has_immediate_source(project: Any, row_id: int) -> bool:
    return (
        project.db.execute(
            "SELECT 1 FROM rows r WHERE r.id=? AND ("
            "r.parent_row_id IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM materialized_row_sources m "
            "WHERE m.materialized_row_id=r.id)) LIMIT 1",
            (row_id,),
        ).fetchone()
        is not None
    )


def _chunks(values: list[int]) -> list[list[int]]:
    return [
        values[offset : offset + SQLITE_ID_CHUNK_SIZE]
        for offset in range(0, len(values), SQLITE_ID_CHUNK_SIZE)
    ]


def _extend_evidence(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    project_id: str,
    out: list[dict[str, Any]],
    seen: set[str],
) -> bool:
    remaining = MAX_SOURCE_EVIDENCE_LINKS - len(out)
    payload = list_row_evidence(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        project_id=project_id,
        subject_ref={
            "kind": "materialized_row",
            "sheet_id": sheet_id,
            "row_id": row_id,
        },
        limit=max(0, remaining),
    )
    for link in payload["links"]:
        stable_id = str(link["stable_id"])
        if stable_id in seen:
            continue
        seen.add(stable_id)
        out.append(link)
    return bool(payload["truncated"])


def _row_documents(project: Any, row: Any) -> list[tuple[dict[str, Any], str]]:
    placeholders = ",".join("?" for _ in DOCUMENT_COLUMN_TYPES)
    rows = project.db.execute(
        "SELECT c.id AS column_id,c.name AS column_name,c.type AS column_type "
        "FROM columns c "
        "JOIN current_cell_values v ON v.column_id=c.id AND v.row_id=? "
        "WHERE c.sheet_id=? AND c.active=1 AND c.hidden=0 "
        f"AND c.type IN ({placeholders}) "
        "AND v.validity='valid' AND v.value_kind<>'null' AND v.value IS NOT NULL "
        "ORDER BY c.position,c.id",
        (int(row["id"]), int(row["sheet_id"]), *DOCUMENT_COLUMN_TYPES),
    ).fetchall()
    documents: list[tuple[dict[str, Any], str]] = []
    for document in rows:
        column_id = int(document["column_id"])
        value = project.get_values(
            int(row["sheet_id"]), column_id, row_ids=[int(row["id"])]
        ).get(int(row["id"]))
        documents.append(
            (
                {
                    "sheet_id": int(row["sheet_id"]),
                    "sheet_name": str(row["sheet_name"]),
                    "row_id": int(row["id"]),
                    "row_position": int(row["position"]),
                    "column_id": column_id,
                    "column_name": str(document["column_name"]),
                    "column_type": str(document["column_type"]),
                    "filename": (
                        str(value["filename"])
                        if isinstance(value, dict)
                        and isinstance(value.get("filename"), str)
                        and value["filename"].strip()
                        else None
                    ),
                    "page": (
                        int(value["page"])
                        if isinstance(value, dict)
                        and type(value.get("page")) is int
                        and value["page"] > 0
                        else None
                    ),
                },
                _document_identity(
                    value, int(row["sheet_id"]), int(row["id"]), column_id
                ),
            )
        )
    return documents


def _document_identity(value: Any, sheet_id: int, row_id: int, column_id: int) -> str:
    if isinstance(value, dict) and isinstance(value.get("blob"), str):
        locator = {"page": value["page"]} if "page" in value else {}
        return (
            "blob:"
            + value["blob"]
            + ":"
            + json.dumps(locator, sort_keys=True, separators=(",", ":"))
        )
    return f"cell:{sheet_id}:{row_id}:{column_id}"


__all__ = [
    "MAX_SOURCE_ANCESTRY_DEPTH",
    "MAX_SOURCE_ANCESTRY_ROWS",
    "MAX_SOURCE_DOCUMENTS",
    "MAX_SOURCE_EVIDENCE_LINKS",
    "RowSourceNotFound",
    "resolve_row_source_documents",
]

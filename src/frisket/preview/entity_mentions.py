"""Read-only entity-mentions preview (no receipt, no side effects).

The Mentions panel browses what `map.ner` already wrote into a marked
`entity_mentions` JSON column: fingerprint-grouped surface groups with exact
distinct-row counts, every raw spelling that was combined, and the
``entity_eq`` selector that filters the original sheet by that group.

Two invariants shape everything here:

* **Parity.** A group's ``row_count`` is the number of rows its OWN emitted
  selector would return. The aggregation walks entity JSON through the SAME
  guarded ``json_each`` construction the ``entity_eq`` WHERE clause uses
  (``frisket.querysets``) and over the same row scope (``sheet_id`` +
  ``hidden=0``) — one SQL shape, not two similar ones.

  Shared SQL is necessary but NOT sufficient, and parity is not a structural
  guarantee here. It rests on a WRITER premise: ``entity_fingerprint`` is a
  total pure function of ``(type, text)``, so one surface is either always
  keyed or always unkeyed and can never appear in both groups. Break that
  premise — the same ``{type,text}`` stored WITH a fingerprint in one row and
  WITHOUT one in another, reachable through ``cell.edit``, which accepts
  arbitrary JSON into a json column — and the surface splits into a
  fingerprint group and a text group whose ``text`` selector matches rows in
  both, so that group's reported ``row_count`` undercounts what its own
  selector returns. Nothing here defends against that; the invariant is
  writer determinism, upstream.
* **Exactness.** Counts, ``total_groups`` and search are full-sheet facts.
  There is no input-row cap and no approximate mode; ``limit``/``offset`` page
  WITHIN each type section, so after the search/type constraints every type
  contributes its own exact ``groups[offset : offset + limit]`` slice and no
  section is starved by a heavier one. ``total_groups`` is the exact overall
  group count across sections and ``type_totals`` carries each section's exact
  count, both independent of the page requested.

Aggregation is SQL (``json_each`` + GROUP BY), never Python iteration over
every cell: the Python that remains is bounded by the number of distinct
(group, surface) pairs.
"""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.ops.entities import UNFINGERPRINTED_ENTITY_TYPES
from frisket.preview.common import ColumnPreviewError, require_visible_sheet
from frisket.querysets import (
    ENTITY_MENTION_ALIAS,
    ENTITY_MENTION_ITEM_PREDICATE,
    ENTITY_MENTIONS_SEMANTIC_TYPE,
    column_semantic_type,
    entity_mention_source_sql,
    guarded_entity_array_params,
    is_entity_mentions_column,
    sheet_live_value_sql,
)

ENTITY_MENTIONS_PREVIEW_SCHEMA_VERSION = "entity-mentions-preview.v2"
ENTITY_MENTIONS_DEFAULT_LIMIT = 100
ENTITY_MENTIONS_MAX_LIMIT = 500

_ITEM = ENTITY_MENTION_ALIAS


class EntityMentionsPreviewError(ColumnPreviewError):
    pass


def _validated_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EntityMentionsPreviewError(
            "invalid_input_ref",
            f"entity mentions preview requires a positive {field}",
            field=field,
        )
    return value


def _validated_limit(limit: Any) -> int:
    if limit is None:
        return ENTITY_MENTIONS_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise EntityMentionsPreviewError(
            "invalid_params", "limit must be an integer", field="limit"
        )
    if limit < 1:
        raise EntityMentionsPreviewError(
            "invalid_params", "limit must be >= 1", field="limit"
        )
    # over-asks clamp to the ceiling (same as the column-values preview) so a
    # client can ask for "as much as you'll give me" without tracking it
    return min(limit, ENTITY_MENTIONS_MAX_LIMIT)


def _validated_offset(offset: Any) -> int:
    if offset is None:
        return 0
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise EntityMentionsPreviewError(
            "invalid_params", "offset must be an integer", field="offset"
        )
    if offset < 0:
        raise EntityMentionsPreviewError(
            "invalid_params", "offset must be >= 0", field="offset"
        )
    return offset


def _validated_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EntityMentionsPreviewError(
            "invalid_params", f"{field} must be a string", field=field
        )
    return value.strip() or None


def _require_eligible_column(project: Project, sheet_id: int, column_id: int) -> Any:
    column = project.db.execute(
        "SELECT * FROM columns WHERE id=? AND sheet_id=? AND hidden=0",
        (column_id, sheet_id),
    ).fetchone()
    if column is None:
        raise EntityMentionsPreviewError(
            "invalid_input_ref",
            "entity mentions preview column_id does not identify a visible "
            "column on this sheet",
            field="column_id",
            details={"sheet_id": sheet_id, "column_id": column_id},
        )
    if not is_entity_mentions_column(column):
        # Deliberately typed and loud. A legacy `map.ner` column, an imported
        # JSON array, or an OCR/evidence column is NOT adapted at read time —
        # the panel offers a rebuild instead of inventing fallback results.
        raise EntityMentionsPreviewError(
            "column_not_entity_mentions",
            "entity mentions preview requires a json column marked "
            f"semantic_type='{ENTITY_MENTIONS_SEMANTIC_TYPE}'",
            field="column_id",
            details={
                "sheet_id": sheet_id,
                "column_id": column_id,
                "column_type": column["type"],
                "semantic_type": column_semantic_type(column),
            },
        )
    generation_store = ResultGenerationStore(project)
    if generation_store.is_generation_managed(
        column_id
    ) and generation_store.has_mixed_origins(column_id):
        # Coverage and grouping currently expose one column-level run context.
        # Refuse a mixed exact-head set until that specialized contract carries
        # per-cell generation provenance.
        raise EntityMentionsPreviewError(
            "mixed_origin_column_unsupported",
            "entity mentions preview is unavailable for a mixed-origin column",
            field="column_id",
            details={"sheet_id": sheet_id, "column_id": column_id},
        )
    return column


def _coverage(project: Project, column: Any, sheet_id: int) -> dict[str, Any]:
    """Extraction coverage for the column's CURRENT run, read verbatim from
    the same run counters every run/provenance payload reports. No new notion
    of failure is invented here: ``completed_rows`` is every row the run
    PROCESSED and ``failed_rows`` is the subset of those that failed, exactly
    as ``RunResultStore.write_results`` maintains them.

    Those three are null when the column has no current run: an un-run column
    has no coverage, and zeroes would read as "0 of 0 rows extracted".

    ``sheet_rows`` is the sheet's live visible row count. ``scope_kind`` says
    whether comparing that count with the historical target is meaningful:
    exact-membership runs intentionally cover only their selected rows."""
    run_id = ResultGenerationStore(project).latest_applied_run_id(int(column["id"]))
    row = (
        project.db.execute(
            "SELECT total_rows, completed_rows, failed_rows, params "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_id
        else None
    )
    sheet_rows = int(project.row_count(sheet_id))
    if row is None:
        return {
            "target_rows": None,
            "completed_rows": None,
            "failed_rows": None,
            "sheet_rows": sheet_rows,
            "scope_kind": None,
        }
    try:
        params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError):
        params = {}
    scope_kind = (
        "exact_membership"
        if isinstance(params, dict) and "row_ids" in params
        else "all_rows"
    )
    return {
        "target_rows": int(row["total_rows"] or 0),
        "completed_rows": int(row["completed_rows"] or 0),
        "failed_rows": int(row["failed_rows"] or 0),
        "sheet_rows": sheet_rows,
        "scope_kind": scope_kind,
    }


def _mention_stream(
    project: Project, column: Any, sheet_id: int, entity_type: str | None
) -> tuple[str, list[Any]]:
    """The per-mention row stream: one row per entity ARRAY ITEM that is a
    usable entity, carrying its row id, canonical type, raw surface, and its
    grouping fingerprint (NULL when the group key is the exact surface).

    What counts as an entity is ``ENTITY_MENTION_ITEM_PREDICATE``, shared
    verbatim with the ``entity_eq`` WHERE clause so the two can never disagree
    about which items exist.
    """
    value_sql, value_params = sheet_live_value_sql("r", column)
    unfingerprinted = sorted(UNFINGERPRINTED_ENTITY_TYPES)
    placeholders = ", ".join("?" * len(unfingerprinted))

    where = [
        "r.sheet_id = ?",
        "r.hidden = 0",
        ENTITY_MENTION_ITEM_PREDICATE,
    ]
    where_params: list[Any] = [sheet_id]
    if entity_type is not None:
        where.append(f"json_extract({_ITEM}.value, '$.type') = ?")
        where_params.append(entity_type)

    sql = f"""
        SELECT
          r.id AS row_id,
          json_extract({_ITEM}.value, '$.type') AS entity_type,
          json_extract({_ITEM}.value, '$.text') AS surface,
          CASE
            WHEN json_type({_ITEM}.value, '$.fingerprint') = 'text'
             AND json_extract({_ITEM}.value, '$.fingerprint') <> ''
             AND json_extract({_ITEM}.value, '$.type') NOT IN ({placeholders})
            THEN json_extract({_ITEM}.value, '$.fingerprint')
          END AS fingerprint
        FROM rows r
        JOIN {entity_mention_source_sql(value_sql)}
        WHERE {" AND ".join(where)}
    """
    # Bind in SQL TEXT order: the CASE in the SELECT list precedes json_each()
    # in the FROM clause, which precedes the WHERE.
    params = [
        *unfingerprinted,
        *guarded_entity_array_params(value_params),
        *where_params,
    ]
    return sql, params


_SELECTOR_KIND_SQL = "CASE WHEN fingerprint IS NULL THEN 'text' ELSE 'fingerprint' END"
_GROUP_KEY_SQL = "CASE WHEN fingerprint IS NULL THEN surface ELSE fingerprint END"


def _aggregate(
    project: Project, stream_sql: str, stream_params: list[Any], *, by_surface: bool
) -> list[Any]:
    """One ``json_each`` + GROUP BY pass.

    ``by_surface=False`` gives per-GROUP counts; ``by_surface=True`` gives
    per-surface counts. Two passes rather than one because a group's
    ``row_count`` is DISTINCT rows across ALL its surfaces — summing the
    per-surface counts would double-count a row that contains two spellings of
    the same group, and that number is exactly what the emitted filter
    returns.
    """
    surface_select = "surface," if by_surface else ""
    surface_group = ", surface" if by_surface else ""
    return project.db.execute(
        f"""
        SELECT
          entity_type,
          {_SELECTOR_KIND_SQL} AS selector_kind,
          {_GROUP_KEY_SQL} AS group_key,
          {surface_select}
          COUNT(DISTINCT row_id) AS row_count,
          COUNT(*) AS mention_count
        FROM ({stream_sql}) mentions
        GROUP BY entity_type, selector_kind, group_key{surface_group}
        """,
        stream_params,
    ).fetchall()


def _selector(kind: str, key: str) -> dict[str, Any]:
    # The fingerprint is an internal comparison token: it travels in the
    # selector but is NEVER a label.
    return {"kind": kind, kind: key}


def resolve_entity_mentions_preview(
    project: Project,
    *,
    sheet_id: Any,
    column_id: Any,
    search: Any = None,
    type: Any = None,  # noqa: A002 - wire field name
    limit: Any = None,
    offset: Any = None,
) -> dict[str, Any]:
    """Fingerprint-grouped mention groups for one marked entity column.

    ``type`` is matched EXACTLY against the stored canonical type, the same
    comparison the ``entity_eq`` filter makes. There is no request-side alias
    canonicalization: this endpoint and the filter must answer the same
    question about the same column, so introducing a canonicalization step on
    only one of them would split one contract into two — a stored type that
    the request-side canonicalizer rewrites would return a page here that the
    equivalent ``entity_eq`` filter does not reproduce. ``map.ner``
    canonicalizes before it writes, so a caller echoing a type back from this
    endpoint's own ``items[].type`` always matches; a hand-typed alias
    (``"org"``) correctly returns an empty page rather than quietly meaning
    something the filter would disagree about.
    """
    effective_sheet_id = _validated_id(sheet_id, "sheet_id")
    effective_column_id = _validated_id(column_id, "column_id")
    effective_search = _validated_text(search, "search")
    effective_type = _validated_text(type, "type")
    effective_limit = _validated_limit(limit)
    effective_offset = _validated_offset(offset)

    require_visible_sheet(
        project,
        effective_sheet_id,
        error=EntityMentionsPreviewError,
        label="entity mentions preview",
    )
    column = _require_eligible_column(project, effective_sheet_id, effective_column_id)

    stream_sql, stream_params = _mention_stream(
        project, column, effective_sheet_id, effective_type
    )
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _aggregate(project, stream_sql, stream_params, by_surface=False):
        key = (row["entity_type"], row["selector_kind"], row["group_key"])
        groups[key] = {
            "type": row["entity_type"],
            "selector": _selector(row["selector_kind"], row["group_key"]),
            "row_count": int(row["row_count"]),
            "mention_count": int(row["mention_count"]),
            "surfaces": [],
        }
    for row in _aggregate(project, stream_sql, stream_params, by_surface=True):
        key = (row["entity_type"], row["selector_kind"], row["group_key"])
        groups[key]["surfaces"].append(
            {
                "text": row["surface"],
                "row_count": int(row["row_count"]),
                "mention_count": int(row["mention_count"]),
            }
        )

    items: list[dict[str, Any]] = []
    for group in groups.values():
        surfaces = group["surfaces"]
        # Label: the real spelling in the MOST DISTINCT ROWS, then shortest,
        # then lexical. Fully deterministic; never the fingerprint.
        label = min(
            surfaces, key=lambda s: (-s["row_count"], len(s["text"]), s["text"])
        )["text"]
        group["surfaces"] = sorted(surfaces, key=lambda s: (-s["row_count"], s["text"]))
        group["label"] = label
        group["surface_count"] = len(surfaces)
        items.append(group)

    if effective_search is not None:
        # Server-side, over EVERY raw surface, BEFORE paging. A hit on any
        # spelling returns the WHOLE group with its full-sheet counts and all
        # its surfaces — search never narrows the numbers to the spelling that
        # happened to match.
        needle = effective_search.casefold()
        items = [
            item
            for item in items
            if any(needle in surface["text"].casefold() for surface in item["surfaces"])
        ]

    # Section ordering weight, NOT a row count and never displayed: the sum of
    # the type's per-group row_counts over the items that survived search. It
    # deliberately over-weights a row carrying two groups of one type (that
    # row is counted once per group), so this is "how much of this page is
    # this type", not the type's distinct row coverage — those differ, and
    # calling it coverage would put a number in the code that no query
    # returns. Computing distinct rows per type would need a third
    # aggregation whose scope is the whole sheet, which would then disagree
    # with the searched subset it is ordering. Ties break on the canonical
    # type name, so the order stays deterministic either way.
    type_weight: dict[str, int] = {}
    for item in items:
        type_weight[item["type"]] = type_weight.get(item["type"], 0) + item["row_count"]
    items.sort(
        key=lambda item: (
            -type_weight[item["type"]],
            item["type"],
            -item["row_count"],
            item["label"],
        )
    )

    # Paging is per type section: every type that survives the search/type
    # constraints contributes its own groups[offset : offset+limit] slice, in
    # section order, so a heavy type can never starve the others off the page.
    # A typed request has exactly one section, so its paging is the plain
    # offset/limit slice it always was.
    section_order: list[str] = []
    sections: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item["type"] not in sections:
            section_order.append(item["type"])
            sections[item["type"]] = []
        sections[item["type"]].append(item)

    page = [
        item
        for entity_type in section_order
        for item in sections[entity_type][
            effective_offset : effective_offset + effective_limit
        ]
    ]
    return {
        "schema_version": ENTITY_MENTIONS_PREVIEW_SCHEMA_VERSION,
        "sheet_id": effective_sheet_id,
        "column": {
            "id": int(column["id"]),
            "name": column["name"],
            "semantic_type": column_semantic_type(column),
        },
        "coverage": _coverage(project, column, effective_sheet_id),
        "search": effective_search,
        "type": effective_type,
        "total_groups": len(items),
        "type_totals": [
            {"type": entity_type, "total_groups": len(sections[entity_type])}
            for entity_type in section_order
        ],
        "limit": effective_limit,
        "offset": effective_offset,
        "items": [
            {
                "type": item["type"],
                "selector": item["selector"],
                "label": item["label"],
                "row_count": item["row_count"],
                "mention_count": item["mention_count"],
                "surface_count": item["surface_count"],
                "surfaces": item["surfaces"],
            }
            for item in page
        ],
    }

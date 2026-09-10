"""Read-only per-mention drill-down: which documents one normalized mention
appears in, and where inside one of them.

This scoped lookup complements `resolve_entity_mentions_preview`, which
recomputes the whole column's GROUP BY on every call. That is the right shape
for browsing the inventory and the wrong shape for "tell me about this one
group" — a per-click full-column aggregation. This filters the same per-mention
stream down to ONE group and pages the rows.

Parity with the Mentions panel is structural, not asserted: the stream comes
from `entity_mentions._mention_stream`, the same construction the `entity_eq`
WHERE clause and the panel's aggregate share, over the same row scope. A group's
`documents` total here is `COUNT(DISTINCT row_id)` and its `mentions` total is
`COUNT(*)` — the panel's `row_count` and `mention_count`, computed the same way
over the same rows. Both therefore inherit the same writer premise the panel
documents: these numbers are exactly what the selector returns.

Route B is the lazy half: given ONE document, the occurrences of the same mention
inside it with a text snippet around each. It reads the coordinate substrate
(`resolve_text_annotations`) rather than the entity array, because only the
substrate knows where in the text a mention sits — and it inherits that query's
fail-closed positioning, so a cell whose text changed after extraction yields the
mismatch reason and no snippets, never a snippet cut at coordinates that stopped
applying.

Copy discipline travels with the data: this groups SPELLINGS by fingerprint (or,
for the unfingerprinted types, matches one exact spelling). It does not resolve
identities and never claims to.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.text_annotations import (
    _cp_to_utf16_prefix,
    resolve_text_annotations,
)
from frisket.ops.entities import UNFINGERPRINTED_ENTITY_TYPES
from frisket.preview.common import require_visible_sheet
from frisket.preview.entity_mentions import (
    _GROUP_KEY_SQL,
    _SELECTOR_KIND_SQL,
    _mention_stream,
    _require_eligible_column,
    _validated_id,
    _validated_limit,
    _validated_offset,
    _validated_text,
    EntityMentionsPreviewError,
)
from frisket.querysets import column_semantic_type

ENTITY_MENTION_DETAIL_SCHEMA_VERSION = "entity-mention-detail.v1"
ENTITY_MENTION_OCCURRENCES_SCHEMA_VERSION = "entity-mention-occurrences.v1"

# How much of a row's title column is worth carrying in a results list.
_TITLE_MAX = 160

# Code points of context on each side of an occurrence. 96 fits the panel's
# two-line snippet at the reader's measure; the ceiling exists so one request
# cannot ask for a page of whole transcripts.
_SNIPPET_RADIUS_DEFAULT = 96
_SNIPPET_RADIUS_MAX = 600
# Occurrences are cheap (they come from one already-resolved cell), so this page
# is larger than Route A's — but still a page: R-snippet-cost's whole point is
# that a document with 200 occurrences shows K of them.
_OCCURRENCE_LIMIT_DEFAULT = 25
_OCCURRENCE_LIMIT_MAX = 200


class EntityMentionDetailError(EntityMentionsPreviewError):
    pass


def _validated_selector(
    entity_type: str | None, fingerprint: Any, text: Any
) -> tuple[str, str, str]:
    """The mention identity, as (type, selector_kind, group_key).

    EXACTLY ONE of fingerprint/text, mirroring the two identity modes the
    Mentions panel emits (D6). Sending both would be two different questions in
    one request, and answering either would be a guess.

    The unfingerprinted types (date, money, quantity, …) are rejected with a
    fingerprint on purpose: they are grouped by exact spelling upstream, so a
    fingerprint selector for one of them names a group the writer never
    creates and would silently return nothing.
    """
    if entity_type is None:
        raise EntityMentionDetailError(
            "invalid_params", "a mention identity requires a type", field="type"
        )
    has_fingerprint = fingerprint is not None
    has_text = text is not None
    if has_fingerprint == has_text:
        raise EntityMentionDetailError(
            "invalid_params",
            "a mention identity is exactly one of fingerprint or text",
            field="fingerprint",
        )
    if has_fingerprint:
        if entity_type in UNFINGERPRINTED_ENTITY_TYPES:
            raise EntityMentionDetailError(
                "invalid_params",
                f"{entity_type} mentions are grouped by exact spelling, not by fingerprint",
                field="fingerprint",
            )
        key = _validated_text(fingerprint, "fingerprint")
        if key is None:
            raise EntityMentionDetailError(
                "invalid_params", "fingerprint must be non-empty", field="fingerprint"
            )
        return entity_type, "fingerprint", key
    # A text selector's key is the exact stored surface, so it is NOT stripped
    # the way a free-text search term would be — " Boeing" and "Boeing" are
    # different spellings and therefore different groups.
    if not isinstance(text, str) or text == "":
        raise EntityMentionDetailError(
            "invalid_params", "text must be a non-empty string", field="text"
        )
    return entity_type, "text", text


def _title_column_id(
    project: Project, sheet_id: int, exclude_column_id: int
) -> int | None:
    """The column whose value names a row in a results list.

    The sheet's explicit title column when one is set; otherwise the first
    visible column that is not the entity column being drilled into. This is
    the SERVER's approximation of the client's `rowTitle` priority — it cannot
    see the grid's current drag order, so a client that reorders columns may
    title a row differently in the grid than here. Titles are labels, not
    identity: `row_id` is what the click-through addresses.
    """
    row = project.db.execute(
        "SELECT title_column_id FROM sheets WHERE id=?", (sheet_id,)
    ).fetchone()
    if row is not None and row["title_column_id"] is not None:
        return int(row["title_column_id"])
    fallback = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND hidden=0 AND id<>? "
        "ORDER BY position, id LIMIT 1",
        (sheet_id, exclude_column_id),
    ).fetchone()
    return int(fallback["id"]) if fallback is not None else None


def resolve_entity_mention_documents(
    project: Project,
    *,
    sheet_id: Any,
    column_id: Any,
    type: Any = None,  # noqa: A002 - wire field name
    fingerprint: Any = None,
    text: Any = None,
    limit: Any = None,
    offset: Any = None,
) -> dict[str, Any]:
    """The rows one mention appears in, most-mentions first, paged."""
    effective_sheet_id = _validated_id(sheet_id, "sheet_id")
    effective_column_id = _validated_id(column_id, "column_id")
    effective_limit = _validated_limit(limit)
    effective_offset = _validated_offset(offset)
    entity_type, selector_kind, group_key = _validated_selector(
        _validated_text(type, "type"), fingerprint, text
    )

    require_visible_sheet(
        project,
        effective_sheet_id,
        error=EntityMentionDetailError,
        label="entity mention detail",
    )
    column = _require_eligible_column(project, effective_sheet_id, effective_column_id)

    stream_sql, stream_params = _mention_stream(
        project, column, effective_sheet_id, entity_type
    )
    scoped_sql = f"""
        SELECT row_id
        FROM ({stream_sql}) mentions
        WHERE {_SELECTOR_KIND_SQL} = ? AND {_GROUP_KEY_SQL} = ?
    """
    scoped_params = [*stream_params, selector_kind, group_key]

    totals = project.db.execute(
        f"SELECT COUNT(DISTINCT row_id) AS documents, COUNT(*) AS mentions "
        f"FROM ({scoped_sql})",
        scoped_params,
    ).fetchone()

    title_column_id = _title_column_id(project, effective_sheet_id, effective_column_id)
    # One page of rows. Ordered by occurrence count desc so the document with
    # the most to read is first, then row_id so the order is total (a tie on
    # title would not be).
    page = project.db.execute(
        f"""
        SELECT row_id, COUNT(*) AS occurrence_count
        FROM ({scoped_sql})
        GROUP BY row_id
        ORDER BY occurrence_count DESC, row_id
        LIMIT ? OFFSET ?
        """,
        [*scoped_params, effective_limit, effective_offset],
    ).fetchall()

    row_ids = [int(row["row_id"]) for row in page]
    titles: dict[int, str] = {}
    if title_column_id is not None and row_ids:
        values, _refs = project.get_values_with_refs(
            effective_sheet_id, title_column_id, row_ids=row_ids
        )
        for row_id, value in values.items():
            if value is None:
                continue
            titles[int(row_id)] = str(value)[:_TITLE_MAX]

    documents = [
        {
            "row_id": int(row["row_id"]),
            "title": titles.get(int(row["row_id"])),
            "occurrence_count": int(row["occurrence_count"]),
        }
        for row in page
    ]
    next_offset = effective_offset + len(documents)
    return {
        "schema_version": ENTITY_MENTION_DETAIL_SCHEMA_VERSION,
        "sheet_id": effective_sheet_id,
        "column": {
            "id": int(column["id"]),
            "name": column["name"],
            "semantic_type": column_semantic_type(column),
        },
        "type": entity_type,
        # Echoed back so the reader and the grid can cross-link to the SAME
        # filter without reconstructing it (D8).
        "selector": {"kind": selector_kind, selector_kind: group_key},
        "totals": {
            "documents": int(totals["documents"]),
            "mentions": int(totals["mentions"]),
        },
        "limit": effective_limit,
        "offset": effective_offset,
        "documents": documents,
        "next_offset": next_offset if next_offset < int(totals["documents"]) else None,
    }


def _validated_occurrence_limit(limit: Any) -> int:
    if limit is None:
        return _OCCURRENCE_LIMIT_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise EntityMentionDetailError(
            "invalid_params", "limit must be an integer", field="limit"
        )
    if limit < 1:
        raise EntityMentionDetailError(
            "invalid_params", "limit must be >= 1", field="limit"
        )
    return min(limit, _OCCURRENCE_LIMIT_MAX)


def _validated_radius(radius: Any) -> int:
    if radius is None:
        return _SNIPPET_RADIUS_DEFAULT
    if isinstance(radius, bool) or not isinstance(radius, int):
        raise EntityMentionDetailError(
            "invalid_params",
            "snippet_radius must be an integer",
            field="snippet_radius",
        )
    if radius < 0:
        raise EntityMentionDetailError(
            "invalid_params", "snippet_radius must be >= 0", field="snippet_radius"
        )
    return min(radius, _SNIPPET_RADIUS_MAX)


def _annotated_text_column(
    project: Project, sheet_id: int, row_id: int, output_column_id: int
) -> Any:
    """The TEXT column an annotation layer for this entity cell is drawn over.

    Route A hands the panel a `row_id`; the panel knows only the ENTITY column
    (that is what a mention group aggregates). The text a mention sits in is the
    layer's coordinate surface, which the substrate reaches from the other end.
    So this walks the same join `resolve_text_annotations` walks, keyed on the
    link's OUTPUT cell instead of its surface, to recover the source column.

    Deliberately does NOT require currentness or a matching hash: a stale layer
    still names its text column, and the caller reports the mismatch instead of
    claiming the document has no annotated text at all.
    """
    return project.db.execute(
        """
        SELECT c.id AS id, c.name AS name
        FROM text_surfaces ts
        JOIN source_spans sp
          ON sp.text_surface_id = ts.id AND sp.span_kind = 'text'
        JOIN evidence_link_spans els
          ON els.span_id = sp.id AND els.span_role = 'annotation'
        JOIN evidence_links el
          ON el.id = els.link_id
        JOIN columns c
          ON c.id = ts.text_column_id AND c.sheet_id = ts.text_sheet_id AND c.hidden = 0
        WHERE ts.surface_kind = 'cell'
          AND ts.text_sheet_id = ? AND ts.text_row_id = ?
          AND el.status = 'active' AND el.layer_family IS NOT NULL
          AND el.sheet_id = ? AND el.row_id = ? AND el.column_id = ?
        ORDER BY c.position, c.id
        LIMIT 1
        """,
        (sheet_id, row_id, sheet_id, row_id, output_column_id),
    ).fetchone()


def _snippet(
    text: str, prefix: list[int], cp_start: int, cp_end: int, radius: int
) -> dict[str, Any]:
    """A window of the cell text around one occurrence.

    Cut on CODE-POINT boundaries (so a window edge can never split a surrogate
    pair) and reported in UTF-16 (so the client, whose strings are UTF-16, can
    index the returned snippet directly). `truncated_*` say whether text was
    dropped, so the panel can draw an ellipsis it has actually earned rather
    than one it assumes."""
    window_start = max(0, cp_start - radius)
    window_end = min(len(text), cp_end + radius)
    return {
        "text": text[window_start:window_end],
        "mark_start": prefix[cp_start] - prefix[window_start],
        "mark_end": prefix[cp_end] - prefix[window_start],
        "truncated_start": window_start > 0,
        "truncated_end": window_end < len(text),
    }


def _span_matches(
    span: Any, entity_type: str, selector_kind: str, group_key: str
) -> bool:
    """Filter the cell's spans to the clicked mention.

    The two identity modes are matched exactly as the aggregation groups them —
    a fingerprint selector against the span's carried fingerprint, a text
    selector against the exact stored surface — so an occurrence list can never
    contain something the group's counts do not."""
    metadata = span.get("metadata") or {}
    if metadata.get("entity_type") != entity_type:
        return False
    if selector_kind == "fingerprint":
        return metadata.get("entity_fingerprint") == group_key
    return span.get("quote") == group_key


def resolve_entity_mention_occurrences(
    project: Project,
    *,
    sheet_id: Any,
    row_id: Any,
    column_id: Any,
    type: Any = None,  # noqa: A002 - wire field name
    fingerprint: Any = None,
    text: Any = None,
    limit: Any = None,
    offset: Any = None,
    snippet_radius: Any = None,
) -> dict[str, Any]:
    """Where inside ONE document a mention occurs, with context, paged."""
    effective_sheet_id = _validated_id(sheet_id, "sheet_id")
    effective_row_id = _validated_id(row_id, "row_id")
    effective_column_id = _validated_id(column_id, "column_id")
    effective_limit = _validated_occurrence_limit(limit)
    effective_offset = _validated_offset(offset)
    radius = _validated_radius(snippet_radius)
    entity_type, selector_kind, group_key = _validated_selector(
        _validated_text(type, "type"), fingerprint, text
    )

    require_visible_sheet(
        project,
        effective_sheet_id,
        error=EntityMentionDetailError,
        label="entity mention detail",
    )
    column = _require_eligible_column(project, effective_sheet_id, effective_column_id)

    base: dict[str, Any] = {
        "schema_version": ENTITY_MENTION_OCCURRENCES_SCHEMA_VERSION,
        "sheet_id": effective_sheet_id,
        "row_id": effective_row_id,
        "column": {
            "id": int(column["id"]),
            "name": column["name"],
            "semantic_type": column_semantic_type(column),
        },
        "type": entity_type,
        "selector": {"kind": selector_kind, selector_kind: group_key},
        "snippet_radius": radius,
        "limit": effective_limit,
        "offset": effective_offset,
        "text_column": None,
        "totals": {"occurrences": 0},
        "occurrences": [],
        "next_offset": None,
        "unpositioned": None,
    }

    text_column = _annotated_text_column(
        project, effective_sheet_id, effective_row_id, effective_column_id
    )
    if text_column is None:
        # No annotation layer reaches this cell — the mention is in the entity
        # array but nothing recorded where. An empty list, not an error: Route A
        # legitimately lists rows whose layer was never written or has since
        # been superseded.
        return base
    base["text_column"] = {"id": int(text_column["id"]), "name": text_column["name"]}

    resolved = resolve_text_annotations(
        project,
        sheet_id=effective_sheet_id,
        row_id=effective_row_id,
        column_id=int(text_column["id"]),
    )
    cell_text = resolved.get("text")
    layers = [
        layer
        for layer in resolved["layers"]
        if int(layer["output_column"]["id"]) == effective_column_id
    ]
    if not isinstance(cell_text, str) or not layers:
        return base

    unpositioned = next((layer for layer in layers if not layer["positioned"]), None)
    positioned = [layer for layer in layers if layer["positioned"]]
    if unpositioned is not None and not positioned:
        # The honest degradation (D9/§6): the layer's last-known total, the
        # reason, and no snippets. Drawing a window at stored coordinates over
        # text that no longer matches them is the confident-wrong highlight the
        # coordinate substrate exists to prevent.
        base["unpositioned"] = dict(unpositioned["unpositioned"])
        return base

    prefix = _cp_to_utf16_prefix(cell_text)
    matches: list[dict[str, Any]] = []
    for layer in positioned:
        for span in layer["spans"]:
            if _span_matches(span, entity_type, selector_kind, group_key):
                matches.append(span)
    # Reading order, which is the only order a document's occurrences have.
    matches.sort(key=lambda span: (span["start"], span["end"], span["occurrence_id"]))

    page = matches[effective_offset : effective_offset + effective_limit]
    occurrences = []
    for span in page:
        cp_start = bisect_left(prefix, span["start"])
        cp_end = bisect_left(prefix, span["end"])
        occurrences.append(
            {
                "occurrence_id": span["occurrence_id"],
                "start": span["start"],
                "end": span["end"],
                "quote": span["quote"],
                "snippet": _snippet(cell_text, prefix, cp_start, cp_end, radius),
            }
        )
    next_offset = effective_offset + len(occurrences)
    base["totals"] = {"occurrences": len(matches)}
    base["occurrences"] = occurrences
    base["next_offset"] = next_offset if next_offset < len(matches) else None
    if unpositioned is not None:
        base["unpositioned"] = dict(unpositioned["unpositioned"])
    return base

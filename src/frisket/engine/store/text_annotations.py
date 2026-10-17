"""The inverse query for annotated text layers.

Every existing evidence reader is subject-scoped ("what supports this output
cell"). The annotation reader needs the inverse — "what annotation layers point
*at* the text I am rendering" — resolved through the coordinate surface:

    text_surfaces(cell locator)
      -> source_spans(text_surface_id, span_kind='text')
      -> evidence_link_spans(span_id, span_role='annotation')
      -> evidence_links(status='active', layer_family NOT NULL)

This is a plain store function (no HTTP): guard triple (hidden sheet/row/column),
current-value-ref intersection on the link's OUTPUT cell (excludes a prior run's
superseded links), content-hash positioning (D4), geometry revalidation against
the current text, an allowlisted public DTO, and codepoint->UTF-16 offset
conversion at this boundary. Surface-less spans and non-annotation spans are
excluded by construction — the feature is opt-in and fails closed at read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from frisket.engine.store.evidence import _json_dumps, _json_loads
from frisket.engine.store.current_value_windows import (
    CurrentValueChanged,
    CurrentValueNotTextual,
    CurrentValueUnavailable,
    MANUAL_EDIT_BYTE_LIMIT,
    TEXT_WINDOW_CODEPOINT_LIMIT,
    current_text_metadata,
    current_text_hash,
    decode_text_prefix,
    read_current_column,
    read_current_text,
    read_sql_text,
    sql_text_sizes,
    limit_text_lines,
)
from frisket.engine.store.citation_text import passage_window

_RESPONSE_OFFSET_UNIT = "utf16_code_unit"
# v1 reads only code-point-stored offsets (all producers write this); a surface in
# any other unit is reported unpositioned rather than mis-sliced.


def _sheet_visible(project: Any, sheet_id: int) -> bool:
    row = project.db.execute(
        "SELECT hidden FROM sheets WHERE id=?", (sheet_id,)
    ).fetchone()
    return row is not None and not row["hidden"]


def _visible_cell(project: Any, sheet_id: int, row_id: int, column_id: int) -> bool:
    """Require the full locator to exist, share one sheet, and be visible."""
    return (
        project.db.execute(
            "SELECT 1 FROM sheets s "
            "JOIN rows r ON r.sheet_id=s.id "
            "JOIN columns c ON c.sheet_id=s.id "
            "WHERE s.id=? AND r.id=? AND c.id=? "
            "AND s.hidden=0 AND r.hidden=0 AND c.hidden=0",
            (sheet_id, row_id, column_id),
        ).fetchone()
        is not None
    )


@contextmanager
def _read_snapshot(project: Any):
    """Keep value refs, hashes, and inverse evidence on one SQLite snapshot."""
    owns_transaction = not project.db.in_transaction
    if owns_transaction:
        project.db.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction:
            project.db.rollback()


def _normalize_producer(producer_json: str | None) -> dict[str, Any]:
    """Coalesce the three producer-kind conventions (§0) into one public shape."""
    data = _json_loads(producer_json, {})
    if not isinstance(data, dict):
        data = {}
    kind = data.get("kind") or data.get("action_kind") or data.get("source_action_kind")
    out: dict[str, Any] = {"kind": kind}
    if data.get("engine") is not None:
        out["engine"] = data.get("engine")
    return out


def _cp_to_utf16_prefix(text: str) -> list[int]:
    """Cumulative UTF-16 code-unit offset for each code-point boundary: prefix[i]
    is the UTF-16 index of code-point i. An astral character (ord >= 0x10000) is
    two UTF-16 code units, so a single emoji earlier in the cell shifts every
    later mark. Built only for the returned passage."""
    prefix = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        prefix[i + 1] = prefix[i] + (1 if ord(ch) < 0x10000 else 2)
    return prefix


def _current_cell(project: Any, sheet_id: int, row_id: int, column_id: int):
    """Return (value, ref) for a cell, filtered for row visibility. ``get_values_
    with_refs`` already drops hidden rows, so a hidden row yields (None, None)."""
    read = read_current_column(
        project.db,
        sheet_id=sheet_id,
        column_id=column_id,
        row_ids=[row_id],
        bounded=True,
    ).get(row_id)
    return (read.value, read.value_ref) if read is not None else (None, None)


def _entity_identity_map(
    project: Any, sheet_id: int, row_id: int, column_id: int, span_ids: list[int]
) -> dict[int, str]:
    """Project only visible mention fingerprints; leave full arrays in SQLite."""
    rows = project.db.execute(
        "SELECT sp.id,substr(json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{}' END,'$.fingerprint'),1,240) AS fingerprint "
        "FROM current_cell_values c, json_each(CASE WHEN c.value_kind IN ('json','text') "
        "AND json_valid(c.value) THEN c.value ELSE '[]' END) item "
        "JOIN source_spans sp ON json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{}' END,'$.text')=sp.quote "
        "AND json_extract(CASE WHEN item.type='object' THEN item.value ELSE '{}' END,'$.type')=json_extract(sp.metadata,'$.entity_type') "
        "WHERE c.validity='valid' AND c.row_id=? AND c.column_id=? AND sp.id IN (SELECT value FROM json_each(?)) "
        "ORDER BY CAST(item.key AS INTEGER)",
        (row_id, column_id, _json_dumps(span_ids)),
    ).fetchall()
    return {
        int(row["id"]): str(row["fingerprint"])
        for row in rows
        if isinstance(row["fingerprint"], str) and row["fingerprint"]
    }


def resolve_text_annotations(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    start: int | None = None,
    limit: int | None = None,
    revision: int | None = None,
    occurrence_id: str | None = None,
) -> dict[str, Any]:
    """Resolve the annotation layers whose coordinate surface is the cell
    ``(sheet_id, row_id, column_id)`` — the text the reader is rendering.

    Returns the current cell text/hash and a list of layers. A positioned layer
    carries UTF-16 span geometry; a content-hash mismatch yields an unpositioned
    summary (D4). Nothing leaks from a hidden sheet/row/column."""
    with _read_snapshot(project):
        return _resolve_text_annotations(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            start=start,
            limit=limit,
            revision=revision,
            occurrence_id=occurrence_id,
        )


def _resolve_text_annotations(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    start: int | None = None,
    limit: int | None = None,
    revision: int | None = None,
    occurrence_id: str | None = None,
) -> dict[str, Any]:
    empty = {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "content_hash": None,
        "offset_unit": _RESPONSE_OFFSET_UNIT,
        "text": None,
        "layers": [],
    }
    if not _visible_cell(project, sheet_id, row_id, column_id):
        return empty

    try:
        meta = current_text_metadata(
            project.db, sheet_id=sheet_id, row_id=row_id, column_id=column_id
        )
    except (CurrentValueNotTextual, CurrentValueUnavailable):
        return empty
    if meta.value_kind != "text":
        return empty
    validity = project.db.execute(
        "SELECT validity FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    if validity is None or validity[0] != "valid":
        return empty
    if revision is not None and revision != meta.retrieval_revision:
        raise CurrentValueChanged(
            "source_changed: reopen annotations before continuing"
        )
    if start is not None and (start < 0 or (start > 0 and revision is None)):
        raise ValueError("revision is required for a continuation passage")
    if limit is not None and not 1 <= limit <= TEXT_WINDOW_CODEPOINT_LIMIT:
        raise ValueError("invalid annotation passage limit")
    bounded = (
        meta.total_bytes > MANUAL_EDIT_BYTE_LIMIT
        or start is not None
        or limit is not None
    )
    size = limit or (
        TEXT_WINDOW_CODEPOINT_LIMIT if bounded else max(1, meta.total_characters)
    )
    base = min(start or 0, meta.total_characters)
    if occurrence_id is not None:
        import re

        if re.fullmatch(r"[0-9]+:[0-9]+", occurrence_id) is None:
            raise ValueError("invalid annotation occurrence_id")
        span_id, link_id = map(int, occurrence_id.split(":"))
        anchor = project.db.execute(
            "SELECT sp.char_start FROM source_spans sp JOIN evidence_link_spans ls ON ls.span_id=sp.id "
            "JOIN evidence_links el ON el.id=ls.link_id JOIN text_surfaces ts ON ts.id=sp.text_surface_id "
            "WHERE sp.id=? AND el.id=? AND el.status='active' AND ls.span_role='annotation' "
            "AND ts.text_sheet_id=? AND ts.text_row_id=? AND ts.text_column_id=?",
            (span_id, link_id, sheet_id, row_id, column_id),
        ).fetchone()
        if anchor is None:
            raise ValueError("annotation occurrence does not belong to this source")
        if bounded and start is None:
            base = min(
                meta.total_characters, max(0, int(anchor[0]) - min(500, size // 2))
            )
    text = read_current_text(
        project.db,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        metadata=meta,
        start=base,
        limit=size,
    )
    if bounded:
        text = limit_text_lines(text)
    current_hash = current_text_hash(
        project.db, sheet_id=sheet_id, row_id=row_id, column_id=column_id, metadata=meta
    )
    prefix = _cp_to_utf16_prefix(text)

    query = """
        WITH candidates AS (SELECT ts.id            AS surface_id,
               ts.content_hash  AS surface_hash,
               ts.offset_unit   AS offset_unit,
               sp.id            AS span_id,
               sp.char_start    AS char_start,
               sp.char_end      AS char_end,
               CASE WHEN length(CAST(sp.quote AS BLOB))>8192 THEN substr(CAST(sp.quote AS BLOB),1,8192) ELSE sp.quote END AS quote,
               length(CAST(sp.quote AS BLOB))>8192 AS quote_truncated,
               sp.text_layer_hash AS span_hash,
               sp.metadata      AS span_metadata,
               el.id            AS link_id,
               el.sheet_id      AS out_sheet_id,
               el.row_id        AS out_row_id,
               el.column_id     AS out_column_id,
               el.subject_ref_json AS subject_ref_json,
               el.producer_json AS producer_json,
               el.layer_family  AS layer_family,
               COUNT(*) OVER (PARTITION BY el.id) AS span_total,
               ROW_NUMBER() OVER (PARTITION BY el.id ORDER BY sp.id) AS span_rank,
               MAX(ts.content_hash != :current_hash) OVER (PARTITION BY el.id) AS bad_surface,
               MAX(ts.offset_unit != 'unicode_codepoint') OVER (PARTITION BY el.id) AS bad_unit,
               MAX(COALESCE(sp.text_layer_hash != ts.content_hash,1)) OVER (PARTITION BY el.id) AS bad_span
        FROM text_surfaces ts
        JOIN source_spans sp
          ON sp.text_surface_id = ts.id AND sp.span_kind = 'text'
        JOIN evidence_link_spans els
          ON els.span_id = sp.id AND els.span_role = 'annotation'
        JOIN evidence_links el
          ON el.id = els.link_id
        WHERE ts.surface_kind = 'cell'
          AND ts.text_sheet_id = :sheet_id AND ts.text_row_id = :row_id AND ts.text_column_id = :column_id
          AND el.status = 'active'
          AND el.layer_family IS NOT NULL
        ) SELECT * FROM candidates
        """
    if bounded:
        query += " WHERE span_rank=1 OR (char_start < :window_end AND char_end > :window_start)"
    query += " ORDER BY link_id,char_start,char_end,span_id"
    rows = project.db.execute(
        query,
        {
            "sheet_id": sheet_id,
            "row_id": row_id,
            "column_id": column_id,
            "current_hash": current_hash,
            "window_start": base,
            "window_end": base + len(text),
        },
    ).fetchall()

    # Group by link (one link = one producer run over one output cell = one layer).
    by_link: dict[int, list[Any]] = {}
    for r in rows:
        by_link.setdefault(r["link_id"], []).append(r)

    # Cache output-cell currentness per (sheet,row,column) so N spans of one link
    # cost one lookup.
    current_ref_cache: dict[tuple[int, int, int], str | None] = {}

    def _output_is_current(r) -> bool:
        out = (r["out_sheet_id"], r["out_row_id"], r["out_column_id"])
        if None in out:
            return False
        if not _visible_cell(project, out[0], out[1], out[2]):
            return False
        key = out
        if key not in current_ref_cache:
            _val, ref = _current_cell(project, out[0], out[1], out[2])
            current_ref_cache[key] = _json_dumps(ref) if ref is not None else None
        stored = current_ref_cache[key]
        return stored is not None and stored == r["subject_ref_json"]

    # Cache the entity-identity map per output cell so N spans of one link cost
    # one read (see _entity_identity_map).
    identity_cache: dict[tuple[int, int, int], dict[int, str]] = {}

    layers: list[dict[str, Any]] = []
    for link_id, group in by_link.items():
        head = group[0]
        if not _output_is_current(head):
            continue  # superseded by a later run/edit or hidden output cell

        out_col_row = project.db.execute(
            "SELECT id, name FROM columns WHERE id=?", (head["out_column_id"],)
        ).fetchone()
        output_column = (
            {"id": int(out_col_row["id"]), "name": out_col_row["name"]}
            if out_col_row is not None
            else {"id": head["out_column_id"], "name": None}
        )
        producer = _normalize_producer(head["producer_json"])
        family = head["layer_family"]
        toggle_key = f"{sheet_id}:{head['out_column_id']}:{family}"
        common = {
            "toggle_key": toggle_key,
            "layer_family": family,
            "producer": producer,
            "output_column": output_column,
        }

        total = int(head["span_total"])
        # Decide positioning from ALL spans in the link, not just the head: one link
        # references one surface today, but don't trust that as an invariant.
        # v1 producers all write 'unicode_codepoint'; the reader's slice + prefix
        # conversion assumes code points, so any other stored unit is reported
        # unpositioned rather than mis-sliced (no utf16-stored producer exists yet).
        if head["bad_unit"]:
            layers.append(
                {
                    **common,
                    "positioned": False,
                    "unpositioned": {
                        "reason": "unsupported_offset_unit",
                        "total": total,
                    },
                }
            )
            continue
        if head["bad_surface"]:
            layers.append(
                {
                    **common,
                    "positioned": False,
                    "unpositioned": {"reason": "content_hash_mismatch", "total": total},
                }
            )
            continue
        if head["bad_span"]:
            layers.append(
                {
                    **common,
                    "positioned": False,
                    "unpositioned": {"reason": "invalid_geometry", "total": total},
                }
            )
            continue

        out_cell = (
            int(head["out_sheet_id"]),
            int(head["out_row_id"]),
            int(head["out_column_id"]),
        )
        if out_cell not in identity_cache:
            identity_cache[out_cell] = _entity_identity_map(
                project,
                *out_cell,
                [
                    int(r["span_id"])
                    for r in rows
                    if (r["out_sheet_id"], r["out_row_id"], r["out_column_id"])
                    == out_cell
                    and type(r["char_start"]) is int
                    and type(r["char_end"]) is int
                    and r["char_start"] < base + len(text)
                    and r["char_end"] > base
                ],
            )
        identities = identity_cache[out_cell]

        spans: list[dict[str, Any]] = []
        invalid = 0
        for r in group:
            start, end, quote = (
                r["char_start"],
                r["char_end"],
                decode_text_prefix(r["quote"]),
            )
            if (
                bounded
                and type(start) is int
                and type(end) is int
                and (end <= base or start >= base + len(text))
            ):
                continue
            local_start = max(0, start - base) if type(start) is int else 0
            local_end = min(len(text), end - base) if type(end) is int else 0
            visible_quote = (
                quote[
                    max(0, base - start) : max(0, base - start)
                    + local_end
                    - local_start
                ]
                if isinstance(quote, str) and type(start) is int
                else None
            )
            if r["quote_truncated"] and type(start) is int:
                sql = "SELECT quote AS value FROM source_spans WHERE id=:span_id"
                params = {"span_id": int(r["span_id"])}
                sizes = sql_text_sizes(project.db, sql, params)
                visible_quote = (
                    read_sql_text(
                        project.db,
                        sql,
                        params,
                        start=max(0, base - start),
                        limit=max(0, local_end - local_start),
                        sizes=sizes,
                    )
                    if sizes
                    else None
                )
            # Revalidate geometry against the CURRENT text and return the current
            # slice, never trusting stored text (§2.6). content_hash already matched,
            # so this should hold; a failure means stored corruption -> counted, not
            # drawn.
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not (0 <= start < end <= meta.total_characters)
                or text[local_start:local_end] != visible_quote
            ):
                invalid += 1
                continue
            md = _json_loads(r["span_metadata"], {})
            entity_type = md.get("entity_type") if isinstance(md, dict) else None
            spans.append(
                {
                    "occurrence_id": f"{r['span_id']}:{link_id}",
                    "start": prefix[local_start],
                    "end": prefix[local_end],
                    **({"source_start": start, "source_end": end} if bounded else {}),
                    **({"quote_truncated": True} if r["quote_truncated"] else {}),
                    "quote": quote,
                    "metadata": {
                        "entity_type": entity_type,
                        # The mention identity the Mentions panel groups by, so
                        # a click on one spelling can open the whole group.
                        # Absent for the unfingerprinted types (date, money, …),
                        # which group by exact spelling by design.
                        "entity_fingerprint": identities.get(int(r["span_id"]))
                        if isinstance(entity_type, str)
                        else None,
                    },
                }
            )
        if not spans and (not bounded or invalid):
            layers.append(
                {
                    **common,
                    "positioned": False,
                    "unpositioned": {"reason": "invalid_geometry", "total": total},
                }
            )
        else:
            layers.append(
                {
                    **common,
                    "positioned": True,
                    "counts": {
                        "shown": len(spans),
                        "total": total,
                        "invalid": invalid,
                    },
                    "spans": spans,
                }
            )

    return {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "content_hash": current_hash,
        "offset_unit": _RESPONSE_OFFSET_UNIT,
        "text": text,
        "layers": layers,
        **(
            {
                "window": passage_window(
                    base,
                    text,
                    total_characters=meta.total_characters,
                    total_bytes=meta.total_bytes,
                    revision=meta.retrieval_revision,
                    limit=size,
                )
            }
            if bounded
            else {}
        ),
    }


def annotated_text_column_ids(project: Any, sheet_id: int) -> list[int]:
    """The distinct source text columns on a sheet reachable through at least one
    valid annotation surface — SheetMeta.annotatedTextColumnIds. This is the
    narrow availability signal that gates the Document view for text; it is NOT
    ``citedColumnIds`` (which is the NER *output* column and every evidence kind).

    A column qualifies when it is a visible cell-surface locator reached through a
    text span + annotation association + active link with a family. Currentness /
    hash freshness is deliberately NOT required here: a same-cell revision-stale
    layer still keeps its source column available because the reader can show an
    unpositioned summary (D4)."""
    with _read_snapshot(project):
        return _annotated_text_column_ids(project, sheet_id)


def _annotated_text_column_ids(project: Any, sheet_id: int) -> list[int]:
    if not _sheet_visible(project, sheet_id):
        return []
    rows = project.db.execute(
        """
        SELECT DISTINCT ts.text_column_id AS column_id,
               el.sheet_id AS out_sheet_id,
               el.row_id AS out_row_id,
               el.column_id AS out_column_id,
               el.subject_ref_json AS subject_ref_json
        FROM text_surfaces ts
        JOIN source_spans sp
          ON sp.text_surface_id = ts.id AND sp.span_kind = 'text'
        JOIN evidence_link_spans els
          ON els.span_id = sp.id AND els.span_role = 'annotation'
        JOIN evidence_links el
          ON el.id = els.link_id
        JOIN columns c
          ON c.id = ts.text_column_id
         AND c.sheet_id = ts.text_sheet_id AND c.hidden = 0
        JOIN rows r
          ON r.id = ts.text_row_id
         AND r.sheet_id = ts.text_sheet_id AND r.hidden = 0
        WHERE ts.surface_kind = 'cell'
          AND ts.text_sheet_id = ?
          AND el.status = 'active'
          AND el.layer_family IS NOT NULL
        ORDER BY ts.text_column_id, el.id
        """,
        (sheet_id,),
    ).fetchall()
    refs: dict[tuple[int, int, int], str | None] = {}
    available: set[int] = set()
    for row in rows:
        out = (row["out_sheet_id"], row["out_row_id"], row["out_column_id"])
        if None in out or not _visible_cell(project, out[0], out[1], out[2]):
            continue
        if out not in refs:
            _value, ref = _current_cell(project, out[0], out[1], out[2])
            refs[out] = _json_dumps(ref) if ref is not None else None
        if refs[out] == row["subject_ref_json"]:
            available.add(int(row["column_id"]))
    return sorted(available)

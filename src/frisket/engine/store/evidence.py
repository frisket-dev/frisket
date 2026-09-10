"""Canonical source-span evidence helpers.

Evidence is durable project data, not receipt-only metadata. The helpers here
are intentionally small: action families can write artifacts/spans/links, and
server/UI layers can resolve stable evidence refs into viewer payloads without
knowing the project-db schema.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from frisket.engine.store.deep_link import compose_deep_link
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.project import Project


CELL_EVIDENCE_SCHEMA_VERSION = "frisket.cell_evidence.v1"
EVIDENCE_VIEWER_SCHEMA_VERSION = "frisket.evidence_viewer.v1"
TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION = (
    "frisket.timestamped_transcript_evidence.v1"
)
# A distinct schema version from CELL_EVIDENCE_SCHEMA_VERSION even though it
# reuses _link_summary's per-link shape — the ENVELOPE differs (a `rows`
# array keyed by row_id, not one row's `links`).
COLUMN_EVIDENCE_BATCH_SCHEMA_VERSION = "frisket.column_evidence_batch.v1"

# A layer_family is a lowercase slug (e.g. 'entities').
# Mirrors the fresh-schema CHECK on evidence_links.layer_family; the writer is the
# authority on bundles that predate that CHECK.
_LAYER_FAMILY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
# Mirrors the text_surfaces.content_hash CHECK ('sha256:' + 64 lowercase hex).
_CONTENT_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
_OFFSET_UNITS = ("unicode_codepoint", "utf16_code_unit")


def record_source_artifact(
    project: Project,
    *,
    artifact_kind: str,
    media_type: str,
    stable_id: str | None = None,
    blob_hash: str | None = None,
    source_url: str | None = None,
    canonical_url: str | None = None,
    title: str | None = None,
    filename: str | None = None,
    page_count: int | None = None,
    duration_ms: int | None = None,
    source_sheet_id: int | None = None,
    source_row_id: int | None = None,
    source_column_id: int | None = None,
    external_ref: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a source artifact and return its public row shape."""

    artifact_stable_id = stable_id or _stable_id("source_artifact")
    owns_transaction = not project.db.in_transaction
    cur = project.db.execute(
        "INSERT INTO source_artifacts ("
        "stable_id, artifact_kind, media_type, blob_hash, source_url, "
        "canonical_url, title, filename, page_count, duration_ms, "
        "source_sheet_id, source_row_id, source_column_id, external_ref_json, "
        "metadata"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            artifact_stable_id,
            artifact_kind,
            media_type,
            blob_hash,
            source_url,
            canonical_url,
            title,
            filename,
            page_count,
            duration_ms,
            source_sheet_id,
            source_row_id,
            source_column_id,
            _json_dumps(external_ref or {}),
            _json_dumps(metadata or {}),
        ),
    )
    if owns_transaction:
        project.db.commit()
    return _artifact_row(project, int(cur.lastrowid))


def record_source_span(
    project: Project,
    *,
    artifact_id: int,
    span_kind: str,
    stable_id: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    bbox: list[dict[str, Any]] | None = None,
    selector: dict[str, Any] | None = None,
    quote: str | None = None,
    snippet: str | None = None,
    text_layer_hash: str | None = None,
    text_surface_id: int | None = None,
    preview: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a first-class source span inside an artifact.

    ``text_surface_id`` is the exact string indexed by ``char_start``/
    ``char_end``. Character offsets are accepted only as a complete, ordered
    integer range with both a surface and that surface's matching content hash.
    Page, bbox, and timestamp selectors are independent coordinate systems and
    do not exempt a character range from this rule."""

    have_start = char_start is not None
    have_end = char_end is not None
    if have_start != have_end:
        raise ValueError("char_start and char_end must be provided together")
    if have_start:
        if (
            isinstance(char_start, bool)
            or isinstance(char_end, bool)
            or not isinstance(char_start, int)
            or not isinstance(char_end, int)
            or char_start < 0
            or char_end <= char_start
        ):
            raise ValueError(
                "character offsets must be integers with 0 <= char_start < char_end"
            )
        if text_surface_id is None or text_layer_hash is None:
            raise ValueError(
                "character offsets require text_surface_id and text_layer_hash"
            )
        surface = project.db.execute(
            "SELECT content_hash FROM text_surfaces WHERE id=?",
            (int(text_surface_id),),
        ).fetchone()
        if surface is None:
            raise ValueError(f"text surface not found: {text_surface_id}")
        if surface["content_hash"] != text_layer_hash:
            raise ValueError("text_layer_hash must match the referenced text surface")
    span_stable_id = stable_id or _stable_id("source_span")
    owns_transaction = not project.db.in_transaction
    cur = project.db.execute(
        "INSERT INTO source_spans ("
        "stable_id, artifact_id, span_kind, page_start, page_end, start_ms, "
        "end_ms, char_start, char_end, bbox_json, selector_json, quote, "
        "snippet, text_layer_hash, text_surface_id, preview_json, metadata"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            span_stable_id,
            artifact_id,
            span_kind,
            page_start,
            page_end,
            start_ms,
            end_ms,
            char_start,
            char_end,
            _json_dumps(bbox or []),
            _json_dumps(selector or {}),
            quote,
            snippet,
            text_layer_hash,
            text_surface_id,
            _json_dumps(preview or {}),
            _json_dumps(metadata or {}),
        ),
    )
    if owns_transaction:
        project.db.commit()
    return _span_row(project, int(cur.lastrowid))


def record_text_surface(
    project: Project,
    *,
    surface_kind: str,
    content_hash: str,
    offset_unit: str,
    text_sheet_id: int | None = None,
    text_row_id: int | None = None,
    text_column_id: int | None = None,
    value_ref: dict[str, Any] | None = None,
    surface_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ensure a ``text_surfaces`` row for a coordinate surface, returning its
    public shape.

    The surface is what a text span's char offsets actually index — distinct from
    the artifact (provenance). Ensure/dedup is deterministic: ``stable_id`` is a
    sha256 of the surface's *canonical storage identity*, so entity spans from
    one capture share a row while a later value ref retains its own lineage.
    Surfaces are append-only; a correction or new capture ref is a new row.

    ``surface_kind='cell'`` requires the full ``(sheet, row, column)`` locator (the
    offsets index exactly that stored cell's string). ``'composite'`` is a
    synthesized string equal to no cell (NER join/template): its locator is all
    null and its provenance lives in ``surface_ref['identity']``. Only the
    ``identity`` portion of ``surface_ref`` enters the stable id, so diagnostics
    can never change surface identity. ``offset_unit`` is the STORED, producer-
    native truth (``'unicode_codepoint'`` for Python), converted only at a reader
    boundary."""
    if surface_kind not in ("cell", "composite"):
        raise ValueError(f"invalid surface_kind: {surface_kind!r}")
    if offset_unit not in _OFFSET_UNITS:
        raise ValueError(f"invalid offset_unit: {offset_unit!r}")
    if not _CONTENT_HASH_RE.fullmatch(content_hash or ""):
        raise ValueError(f"invalid content_hash: {content_hash!r}")
    locator = (text_sheet_id, text_row_id, text_column_id)
    if surface_kind == "cell" and any(part is None for part in locator):
        raise ValueError("cell surface requires a full (sheet, row, column) locator")
    if surface_kind == "composite" and any(part is not None for part in locator):
        raise ValueError("composite surface must not carry a cell locator")
    if surface_kind == "composite" and value_ref is not None:
        raise ValueError("composite surface must not carry a cell value_ref")

    surface_ref = dict(surface_ref or {})
    identity: dict[str, Any] = {
        "surface_kind": surface_kind,
        "content_hash": content_hash,
        "offset_unit": offset_unit,
    }
    if surface_kind == "cell":
        # value_ref is deliberately NOT in the identity: content_hash + locator
        # already fully define the coordinate space the offsets index, and no
        # reader consumes the surface's value_ref. Including it only forks the
        # append-only row across reruns of identical text — extra orphan surfaces
        # for a field nothing observes. It is retained only as a non-authoritative
        # first-capture diagnostic, not value-revision identity.
        identity["locator"] = {
            "sheet_id": int(text_sheet_id),
            "row_id": int(text_row_id),
            "column_id": int(text_column_id),
        }
    else:
        composite_identity = surface_ref.get("identity")
        if not composite_identity:
            raise ValueError(
                "composite surface requires a non-empty surface_ref['identity'] "
                "(its provenance); without it distinct compositions would collapse"
            )
        identity["composite"] = composite_identity
    surface_stable_id = (
        "text_surface:"
        + hashlib.sha256(_json_dumps(identity).encode("utf-8")).hexdigest()
    )

    existing = project.db.execute(
        "SELECT id FROM text_surfaces WHERE stable_id=?", (surface_stable_id,)
    ).fetchone()
    if existing is not None:
        return _surface_row(project, int(existing["id"]))

    owns_transaction = not project.db.in_transaction
    try:
        cur = project.db.execute(
            "INSERT INTO text_surfaces ("
            "stable_id, surface_kind, text_sheet_id, text_row_id, text_column_id, "
            "value_ref_json, content_hash, offset_unit, surface_ref_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                surface_stable_id,
                surface_kind,
                None if text_sheet_id is None else int(text_sheet_id),
                None if text_row_id is None else int(text_row_id),
                None if text_column_id is None else int(text_column_id),
                _json_dumps(value_ref) if value_ref is not None else None,
                content_hash,
                offset_unit,
                _json_dumps(surface_ref),
            ),
        )
    except sqlite3.IntegrityError:
        # A concurrent writer inserted the same deterministic surface first; the
        # UNIQUE(stable_id) lost the race. Re-fetch rather than duplicate.
        if owns_transaction:
            project.db.rollback()
        row = project.db.execute(
            "SELECT id FROM text_surfaces WHERE stable_id=?", (surface_stable_id,)
        ).fetchone()
        if row is None:
            raise
        return _surface_row(project, int(row["id"]))
    if owns_transaction:
        project.db.commit()
    return _surface_row(project, int(cur.lastrowid))


def get_source_span(project: Project, span_id: int) -> dict[str, Any]:
    """Fetch an existing source span by id (its public row shape).

    Lets a caller REUSE an already-recorded span (e.g. the ``av`` temporal
    spans ``media.transcribe``'s evidence writer records per segment) instead
    of re-recording a duplicate -- so two citations of the same segment point
    at the SAME span row (extract-scalar-temporal-anchors-v1: strategy B's
    lookup-not-record semantics)."""

    return _span_row(project, int(span_id))


def merge_artifact_metadata(
    project: Project, artifact_id: int, extra: dict[str, Any]
) -> dict[str, Any]:
    """Shallow-merge ``extra`` into an artifact's stored metadata and return the
    refreshed row. Used by the extract aligner (W2.1) to borrow a sibling OCR
    artifact's ``page_images`` onto the extract artifact so its aligned region
    spans have an image to draw on."""

    # ``metadata.timeline`` is an immutable, host-owned presentation-clock
    # identity.  Generic enrichment merges may add descriptive metadata but
    # must never create, replace, or delete that reserved key; use
    # store.artifact_timeline's ensure helpers instead.  Without this guard an
    # OCR/extract enrichment could silently invalidate every persisted temporal
    # value anchored to the artifact.
    if "timeline" in extra:
        raise ValueError("metadata.timeline is reserved for artifact timeline identity")

    row = project.db.execute(
        "SELECT metadata FROM source_artifacts WHERE id=?", (int(artifact_id),)
    ).fetchone()
    if row is None:
        raise KeyError(f"source artifact not found: {artifact_id!r}")
    metadata = _json_loads(row["metadata"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(extra)
    owns_transaction = not project.db.in_transaction
    project.db.execute(
        "UPDATE source_artifacts SET metadata=? WHERE id=?",
        (_json_dumps(metadata), int(artifact_id)),
    )
    if owns_transaction:
        project.db.commit()
    return _artifact_row(project, int(artifact_id))


def record_evidence_link(
    project: Project,
    *,
    subject_kind: str,
    subject_ref: dict[str, Any],
    spans: list[dict[str, Any]],
    stable_id: str | None = None,
    sheet_id: int | None = None,
    row_id: int | None = None,
    column_id: int | None = None,
    run_id: int | None = None,
    op_id: int | None = None,
    receipt_id: str | None = None,
    link_role: str = "support",
    status: str = "active",
    confidence: float | None = None,
    pinned: bool = False,
    producer: dict[str, Any] | None = None,
    layer_family: str | None = None,
    stale_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach one product subject to one or more source spans.

    ``layer_family`` is the stable semantic family of an
    annotation layer, e.g. ``'entities'``. Set it only when a producer intends a
    reader layer, together with ``span_role='annotation'`` on the relevant spans;
    ordinary support/citation links leave it NULL and never render as a layer."""

    if layer_family is not None and not _LAYER_FAMILY_RE.fullmatch(layer_family):
        raise ValueError(f"invalid layer_family: {layer_family!r}")
    link_stable_id = stable_id or _stable_id("evidence_link")
    owns_transaction = not project.db.in_transaction
    cur = project.db.cursor()
    try:
        if owns_transaction:
            cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "INSERT INTO evidence_links ("
            "stable_id, subject_kind, subject_ref_json, sheet_id, row_id, "
            "column_id, run_id, op_id, receipt_id, link_role, status, "
            "confidence, pinned, producer_json, layer_family, stale_reason, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link_stable_id,
                subject_kind,
                _json_dumps(subject_ref),
                sheet_id,
                row_id,
                column_id,
                run_id,
                op_id,
                receipt_id,
                link_role,
                status,
                confidence,
                int(pinned),
                _json_dumps(producer or {}),
                layer_family,
                stale_reason,
                _json_dumps(metadata or {}),
            ),
        )
        link_id = int(cur.lastrowid)
        for index, span in enumerate(spans):
            span_id = _span_id_for_ref(project, span)
            cur.execute(
                "INSERT INTO evidence_link_spans ("
                "link_id, span_id, rank, span_role, required, note"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    link_id,
                    span_id,
                    int(span.get("rank", index)),
                    str(span.get("span_role") or "support"),
                    int(bool(span.get("required", False))),
                    span.get("note"),
                ),
            )
        if owns_transaction:
            project.db.commit()
    except Exception:
        if owns_transaction:
            project.db.rollback()
        raise
    return _link_row(project, link_id)


def sheet_cited_column_ids(project: Project, sheet_id: int) -> list[int]:
    """The answers-view availability signal (answers-view-cited-columns-
    signal-v1 contract):
    every column on this sheet carrying at least one ACTIVE evidence link,
    in ascending column-id order. Mirrors the graph-view materializedKind
    precedent — a server-computed, sheet-shape signal the switcher consumes
    synchronously, not a probe endpoint. Index-served by
    idx_evidence_links_sheet_status (schema.py) so a sheet_id-scoped query
    does not fall back to the row_id-leading idx_evidence_links_cell_status
    index. Always returns a list (possibly empty) — never None/absent, so
    callers can attach it to SheetMeta unconditionally."""

    rows = project.db.execute(
        "SELECT DISTINCT column_id FROM evidence_links "
        "WHERE sheet_id=? AND status='active' AND column_id IS NOT NULL "
        "ORDER BY column_id",
        (int(sheet_id),),
    ).fetchall()
    return [int(row["column_id"]) for row in rows]


def list_cell_evidence(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    include_stale: bool = False,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Resolve evidence links for the cell's current value ref.

    Active evidence is current-value scoped. Stale audit evidence is counted by
    row/column because it intentionally belongs to an older value for the same
    cell.
    """

    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    current_ref = refs.get(row_id)
    rows: list[sqlite3.Row] = []
    if current_ref is not None:
        rows.extend(
            project.db.execute(
                "SELECT * FROM evidence_links "
                "WHERE row_id=? AND column_id=? AND status='active' "
                "AND subject_ref_json=? "
                "ORDER BY created_at, id",
                (row_id, column_id, _json_dumps(current_ref)),
            ).fetchall()
        )
    stale_count = int(
        project.db.execute(
            "SELECT COUNT(*) FROM evidence_links "
            "WHERE row_id=? AND column_id=? AND status='stale'",
            (row_id, column_id),
        ).fetchone()[0]
    )
    if include_stale:
        rows.extend(
            project.db.execute(
                "SELECT * FROM evidence_links "
                "WHERE row_id=? AND column_id=? AND status='stale' "
                "ORDER BY stale_at, id",
                (row_id, column_id),
            ).fetchall()
        )
    return {
        "schema_version": CELL_EVIDENCE_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "row_id": row_id,
        "column_id": column_id,
        "current_value_ref": current_ref,
        "links": [_link_summary(project, row, project_id=project_id) for row in rows],
        "stale_count": stale_count,
    }


def list_column_evidence(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Batch column-evidence projection for the Grounded Answers reading
    view's middle pane.

    Per row THAT HAS ACTIVE evidence on this column, returns its row_id plus
    the same ``_link_summary`` chip projection ``list_cell_evidence`` produces
    for one cell — so a chip here is byte-identical to a chip in the row
    Detail drawer. Current-value-ref scoped exactly like ``list_cell_evidence``
    (a link only counts if its ``subject_ref_json`` matches the row's CURRENT
    value ref), just batched: one ``get_values_with_refs`` call for every row
    in `row_ids` (or the whole column when `row_ids` is None) instead of N
    per-cell round trips, then ONE grouped query for active links across those
    rows instead of N single-row queries — the spec's "N per-cell calls
    forbidden" constraint (risk 3), satisfied at the SQL layer too, not just
    the HTTP layer. `row_ids` pairs with the list's virtualization window; a
    row with no active evidence is simply absent from `rows` (not a
    zero-links entry) — smaller payload for the common case.
    """

    empty: dict[str, Any] = {
        "schema_version": COLUMN_EVIDENCE_BATCH_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "column_id": column_id,
        "rows": [],
    }
    if row_ids is not None and not row_ids:
        return empty
    _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=row_ids)
    if not refs:
        return empty
    target_row_ids = list(refs.keys())
    placeholders = ",".join("?" * len(target_row_ids))
    link_rows = project.db.execute(
        "SELECT * FROM evidence_links "
        f"WHERE column_id=? AND status='active' AND row_id IN ({placeholders}) "
        "ORDER BY row_id, created_at, id",
        (column_id, *target_row_ids),
    ).fetchall()
    by_row: dict[int, list[sqlite3.Row]] = {}
    for link_row in link_rows:
        ref = refs.get(link_row["row_id"])
        # Current-value-ref scoping (list_cell_evidence's exact rule): a link
        # only counts for a row if it was recorded against that row's CURRENT
        # value ref, excluding stale/superseded links left behind by an
        # earlier run/edit that a later run/edit has since replaced.
        if ref is None or link_row["subject_ref_json"] != _json_dumps(ref):
            continue
        by_row.setdefault(link_row["row_id"], []).append(link_row)
    return {
        "schema_version": COLUMN_EVIDENCE_BATCH_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "column_id": column_id,
        "rows": [
            {
                "row_id": row_id,
                "links": [
                    _link_summary(project, link_row, project_id=project_id)
                    for link_row in link_rows_for_row
                ],
            }
            for row_id, link_rows_for_row in by_row.items()
        ],
    }


def find_item_evidence_link(
    project: Project,
    *,
    row_id: int,
    column_id: int,
    item_index: int,
    value_hash: str,
    run_id: int | None = None,
) -> dict[str, Any] | None:
    """Find the ACTIVE per-item evidence link a list-item grounding write
    (extract-list-item-grounding-v1, ``sdk/ops/extract.py``'s
    ``_write_list_field_grounding``) recorded for ``(row_id, column_id,
    item_index)`` and the admitted item's ``value_hash``. When the source has
    an authoritative run, only links from that run can match. Used during
    list-table admission to capture the exact source item's grounding.
    Returns the link's id/stable_id plus its ordered spans
    (span_id/rank/span_role/required), or ``None`` if no per-item link was
    ever written for that item -- nothing to propagate, not an error (any
    list source other than extract-list-item-grounding-v1's write path has no
    per-item links and is a silent no-op at the caller)."""

    rows = project.db.execute(
        "SELECT * FROM evidence_links "
        "WHERE row_id=? AND column_id=? AND status='active' "
        "ORDER BY created_at, id",
        (int(row_id), int(column_id)),
    ).fetchall()
    for row in rows:
        metadata = _json_loads(row["metadata"], {})
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("item_index")) is not int
            or metadata["item_index"] != item_index
            or metadata.get("value_hash") != value_hash
            or (run_id is not None and row["run_id"] != run_id)
        ):
            continue
        span_rows = project.db.execute(
            "SELECT span_id, rank, span_role, required FROM evidence_link_spans "
            "WHERE link_id=? ORDER BY rank, span_id",
            (int(row["id"]),),
        ).fetchall()
        return {
            "id": int(row["id"]),
            "stable_id": row["stable_id"],
            "spans": [
                {
                    "span_id": int(span_row["span_id"]),
                    "rank": int(span_row["rank"]),
                    "span_role": span_row["span_role"],
                    "required": bool(span_row["required"]),
                }
                for span_row in span_rows
            ],
        }
    return None


def list_row_evidence(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Resolve ACTIVE row-level (column-less) evidence links for ``row_id`` --
    the shape ``derive.table_from_list``'s item-evidence propagation writes
    (table-from-list-evidence-v1: a derived row's citation is the whole row,
    not one of its columns). The column-scoped sibling is
    :func:`list_cell_evidence`."""

    rows = project.db.execute(
        "SELECT * FROM evidence_links "
        "WHERE sheet_id=? AND row_id=? AND column_id IS NULL AND status='active' "
        "ORDER BY created_at, id",
        (int(sheet_id), int(row_id)),
    ).fetchall()
    return {
        "schema_version": CELL_EVIDENCE_SCHEMA_VERSION,
        "sheet_id": sheet_id,
        "row_id": row_id,
        "links": [_link_summary(project, row, project_id=project_id) for row in rows],
    }


# Contiguous-run grouping for a citation's CITED (required=1) temporal
# spans. Two cited spans on the SAME artifact merge into one run when the
# gap between the earlier span's end_ms and the later span's start_ms is
# <= this tolerance; a bigger gap starts a new run. Value chosen from a
# real 6-span citation, a transcript-grounded map.extract field:
# within-thought ASR pause gaps landed at 190-900ms; the gap BETWEEN two
# distinct drywall tips was 14,750ms -- a wide, unambiguous margin, so
# 1500ms cleanly separates "same thought" from "a different citation"
# without per-artifact tuning. Computed ONCE here and reused by both the
# viewer payload (per-span run_index / per-artifact runs list, below) and
# the run-level clip endpoint (media_clip.py's resolve_link_run_clip_source)
# -- so the clip a user downloads always matches the range the viewer
# highlighted as that run.
CITATION_RUN_GAP_TOLERANCE_MS = 1500


def _group_citation_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows``: cited temporal spans, already ordered by (artifact_key,
    start_ms). Returns runs in the same order, each ``{artifact_key,
    start_ms, end_ms, span_stable_ids}``."""
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        if (
            current is not None
            and current["artifact_key"] == row["artifact_key"]
            and row["start_ms"] - current["end_ms"] <= CITATION_RUN_GAP_TOLERANCE_MS
        ):
            current["end_ms"] = max(current["end_ms"], row["end_ms"])
            current["span_stable_ids"].append(row["stable_id"])
            continue
        current = {
            "artifact_key": row["artifact_key"],
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "span_stable_ids": [row["stable_id"]],
        }
        runs.append(current)
    return runs


def citation_temporal_runs(
    project: Project, evidence_link_ref: int | str
) -> list[dict[str, Any]]:
    """A citation link's cited (``required=1``) temporal spans on an ``av``
    artifact, grouped into contiguous runs (:data:`CITATION_RUN_GAP_TOLERANCE_MS`).
    Each run carries its owning artifact's clip-relevant fields (blob_hash/
    media_type/filename/duration_ms/title) so media_clip.py's run-clip
    resolver needs no second query. Raises ``KeyError`` if the link does not
    exist; returns ``[]`` for a link with no cited AV temporal spans (a
    text/page citation, or one with nothing marked required)."""
    link = _lookup_link(project, evidence_link_ref)
    if link is None:
        raise KeyError(f"evidence link not found: {evidence_link_ref!r}")
    rows = project.db.execute(
        """
        SELECT sp.stable_id AS span_stable_id, sp.start_ms, sp.end_ms,
               art.stable_id AS artifact_stable_id, art.blob_hash,
               art.media_type, art.filename, art.duration_ms, art.title
        FROM evidence_link_spans els
        JOIN source_spans sp ON sp.id = els.span_id
        JOIN source_artifacts art ON art.id = sp.artifact_id
        WHERE els.link_id=? AND els.required=1 AND sp.span_kind='temporal'
              AND art.artifact_kind='av'
              AND sp.start_ms IS NOT NULL AND sp.end_ms IS NOT NULL
        ORDER BY art.id, sp.start_ms, sp.id
        """,
        (int(link["id"]),),
    ).fetchall()
    grouped = _group_citation_runs(
        [
            {
                "artifact_key": str(row["artifact_stable_id"]),
                "start_ms": int(row["start_ms"]),
                "end_ms": int(row["end_ms"]),
                "stable_id": row["span_stable_id"],
            }
            for row in rows
        ]
    )
    artifact_meta: dict[str, sqlite3.Row] = {}
    for row in rows:
        artifact_meta.setdefault(str(row["artifact_stable_id"]), row)
    runs: list[dict[str, Any]] = []
    for index, run in enumerate(grouped):
        meta = artifact_meta[run["artifact_key"]]
        runs.append(
            {
                "index": index,
                "artifact_stable_id": run["artifact_key"],
                "start_ms": run["start_ms"],
                "end_ms": run["end_ms"],
                "span_stable_ids": run["span_stable_ids"],
                "blob_hash": meta["blob_hash"],
                "media_type": meta["media_type"],
                "filename": meta["filename"],
                "duration_ms": meta["duration_ms"],
                "title": meta["title"],
            }
        )
    return runs


def resolve_evidence_viewer(
    project: Project,
    evidence_link_ref: int | str,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Resolve an evidence link into a renderer-neutral viewer payload."""

    link = _lookup_link(project, evidence_link_ref)
    if link is None:
        raise KeyError(f"evidence link not found: {evidence_link_ref!r}")
    joined = project.db.execute(
        """
        SELECT els.rank, els.span_role, els.required, els.note,
               sp.*, art.stable_id AS artifact_stable_id,
               art.artifact_kind, art.media_type, art.blob_hash,
               art.source_url, art.canonical_url, art.title, art.filename,
               art.page_count, art.duration_ms, art.source_sheet_id,
               art.source_row_id, art.source_column_id, art.external_ref_json,
               art.metadata AS artifact_metadata
        FROM evidence_link_spans els
        JOIN source_spans sp ON sp.id = els.span_id
        JOIN source_artifacts art ON art.id = sp.artifact_id
        WHERE els.link_id=?
        ORDER BY els.rank, sp.id
        """,
        (int(link["id"]),),
    ).fetchall()
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_order: list[str] = []
    blob_metadata_cache: dict[str, dict[str, Any]] = {}
    blob_store = MediaBlobStore(project)
    for row in joined:
        artifact_key = str(row["artifact_stable_id"])
        if artifact_key not in artifacts:
            artifact = _artifact_payload_from_join(row, project_id=project_id)
            artifacts[artifact_key] = artifact
            artifact_order.append(artifact_key)
        blob_hash = row["blob_hash"]
        if blob_hash and blob_hash not in blob_metadata_cache:
            try:
                blob_metadata_cache[blob_hash] = blob_store.acquisition_metadata(
                    blob_hash
                )
            except KeyError:
                blob_metadata_cache[blob_hash] = {}
        artifacts[artifact_key]["spans"].append(
            _span_payload(
                row,
                link,
                blob_metadata=blob_metadata_cache.get(blob_hash, {}),
                project_id=project_id,
            )
        )

    for artifact in artifacts.values():
        artifact["pages"] = _page_payloads(artifact, project_id=project_id)

    # Attach each cited span's run membership (run_index) and each
    # artifact's run-level clip affordances -- ONE clip per contiguous run
    # of the citation's own temporal spans, start of the run's first span
    # -> end of its last (media_clip.py's resolve_link_run_clip_source, same
    # padded_range/cut_clip machinery the per-span clip already uses).
    runs = citation_temporal_runs(project, int(link["id"]))
    run_index_by_span: dict[str, int] = {}
    runs_by_artifact: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        for span_stable_id in run["span_stable_ids"]:
            run_index_by_span[span_stable_id] = run["index"]
        clip_url = (
            f"/api/projects/{project_id or 'local'}"
            f"/evidence/links/{link['stable_id']}/runs/{run['index']}/clip"
            if run["blob_hash"]
            else None
        )
        runs_by_artifact.setdefault(run["artifact_stable_id"], []).append(
            {
                "index": run["index"],
                "start_ms": run["start_ms"],
                "end_ms": run["end_ms"],
                "span_ids": run["span_stable_ids"],
                "clip_url": clip_url,
            }
        )
    for artifact_key, artifact in artifacts.items():
        artifact["runs"] = runs_by_artifact.get(artifact_key, [])
        for span in artifact["spans"]:
            span["run_index"] = run_index_by_span.get(span["stable_id"])

    metadata = _json_loads(link["metadata"], {})
    producer = _json_loads(link["producer_json"], {})
    warnings = _dedupe_strings(
        [
            *(_list_value(metadata.get("warnings"))),
            *(_list_value(producer.get("warnings"))),
        ]
    )
    return {
        "schema_version": EVIDENCE_VIEWER_SCHEMA_VERSION,
        "link": {
            "id": int(link["id"]),
            "stable_id": link["stable_id"],
            "export_ref": link["stable_id"],
            "subject_kind": link["subject_kind"],
            "subject_ref": _json_loads(link["subject_ref_json"], {}),
            "sheet_id": link["sheet_id"],
            "row_id": link["row_id"],
            "column_id": link["column_id"],
            "run_id": link["run_id"],
            "op_id": link["op_id"],
            "receipt_id": link["receipt_id"],
            "role": link["link_role"],
            "status": link["status"],
            "confidence": link["confidence"],
            "pinned": bool(link["pinned"]),
            "producer": producer,
            "stale_reason": link["stale_reason"],
            "stale_at": link["stale_at"],
            "created_at": link["created_at"],
            # A hash-mismatch flag, NOT a silent hide and NOT a delete. Every
            # span's `text_layer_hash` (recorded at write time) is revalidated
            # against the link's cell's CURRENT live value, independent of
            # whether staleness marking fired at the mutation site.
            "text_layer_hash_mismatch": _text_layer_hash_mismatch(
                project, link, joined
            ),
        },
        "artifacts": [artifacts[key] for key in artifact_order],
        "warnings": warnings,
    }


def mark_evidence_stale_for_cell_refs(
    project: Project,
    targets: list[dict[str, Any]],
    *,
    reason: str = "manual_cell_edit",
) -> list[str]:
    """Mark active evidence for the targets' current value refs stale.

    The update includes the subject/value ref, not just row/column, so an
    unrelated active link for the same cell but a different run/manual value is
    not incorrectly hidden. This is the manual-edit/review-decision shape:
    the caller knows the EXACT prior ref being replaced.

    A target may instead set ``match_all_active=True`` (no ``current_value_ref``
    needed) for the producing-run-reprocess shape (evidence-stale-on-reprocess-v1):
    re-OCR/re-extract with different params creates a brand-new active link for
    the same (row_id, column_id) whose OLD subject ref the writer never tracked
    explicitly. ``match_all_active`` stales every currently-active link on that
    cell regardless of subject ref, so the superseded grounding does not sit
    forever invisible -- neither "active-current" (its subject_ref_json no
    longer matches the cell's live ref once the new run wins) nor counted as
    stale (``list_cell_evidence``'s ``stale_count`` only counts
    ``status='stale'``) -- the exact SILENTLY-WRONG #3 orphan this task closes.
    Callers MUST invoke this BEFORE writing the new run's active link, so the
    just-written link is never caught by its own stale sweep.

    Transaction-safe standalone (``owns_transaction`` -- the same guard
    ``record_evidence_link``/``record_source_artifact`` use): the two
    existing manual-edit/review-decision call sites run inside the
    executor's own BEGIN IMMEDIATE/commit envelope, so this is a no-op
    there; the reprocess call site (``media/ocr.py``) has no such envelope
    around the write step, so it commits its own update.
    """

    owns_transaction = not project.db.in_transaction
    stale_ids: list[str] = []
    try:
        if owns_transaction:
            project.db.execute("BEGIN IMMEDIATE")
        for target in targets:
            ref = target.get("current_value_ref")
            match_all = bool(target.get("match_all_active"))
            if not match_all and not isinstance(ref, dict):
                continue
            row_id = int(target["row_id"])
            column_id = int(target["column_id"])
            if match_all:
                select_sql = (
                    "SELECT stable_id FROM evidence_links "
                    "WHERE status='active' AND row_id=? AND column_id=?"
                )
                select_params: tuple[Any, ...] = (row_id, column_id)
                update_sql = (
                    "UPDATE evidence_links SET status='stale', stale_reason=?, "
                    "stale_at=datetime('now') "
                    "WHERE status='active' AND row_id=? AND column_id=?"
                )
                update_params: tuple[Any, ...] = (reason, row_id, column_id)
            else:
                select_sql = (
                    "SELECT stable_id FROM evidence_links "
                    "WHERE status='active' AND row_id=? AND column_id=? "
                    "AND subject_ref_json=?"
                )
                select_params = (row_id, column_id, _json_dumps(ref))
                update_sql = (
                    "UPDATE evidence_links SET status='stale', stale_reason=?, "
                    "stale_at=datetime('now') "
                    "WHERE status='active' AND row_id=? AND column_id=? "
                    "AND subject_ref_json=?"
                )
                update_params = (reason, row_id, column_id, _json_dumps(ref))
            rows = project.db.execute(select_sql, select_params).fetchall()
            if not rows:
                continue
            stale_ids.extend(str(row["stable_id"]) for row in rows)
            project.db.execute(update_sql, update_params)
        if owns_transaction:
            project.db.commit()
    except Exception:
        if owns_transaction:
            project.db.rollback()
        raise
    return stale_ids


def _text_hash(text: str) -> str:
    """Same ``sha256:`` digest convention as ``media_text_hash``
    (``executor/action_families/media/_shared.py``) and ``ner.py``'s
    ``_text_hash`` -- duplicated on purpose rather than imported: the store
    layer must not depend on the executor layer (which itself imports
    ``frisket.store``), and the digest only needs to be byte-identical across
    the producers that write ``text_layer_hash``, not shared code."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_layer_hash_mismatch(
    project: Project, link: sqlite3.Row, joined_spans: list[sqlite3.Row]
) -> bool:
    """True if any span's stored ``text_layer_hash`` no longer matches the
    link's subject cell's CURRENT live value (evidence-stale-on-reprocess-v1).

    This is independent of ``status``/``stale_reason``: it revalidates the
    hash even for a link staleness marking never touched, so a mutation site
    that forgot to call ``mark_evidence_stale_for_cell_refs`` (the documented
    gap -- ``store/staleness.py``: "fires from only 2 of many mutation
    sites") still surfaces a loud flag instead of silently serving drifted
    grounding. A link with no hash-bearing spans, or whose subject cell can't
    be resolved as plain text, has nothing to revalidate and reports False.
    """
    hash_bearing = [row for row in joined_spans if row["text_layer_hash"]]
    if not hash_bearing:
        return False
    sheet_id = link["sheet_id"]
    row_id = link["row_id"]
    column_id = link["column_id"]
    if sheet_id is None or row_id is None or column_id is None:
        return False
    values = project.get_values(int(sheet_id), int(column_id), row_ids=[int(row_id)])
    current = values.get(int(row_id))
    if not isinstance(current, str):
        return False
    current_hash = _text_hash(current)
    return any(row["text_layer_hash"] != current_hash for row in hash_bearing)


def _stable_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


def _artifact_row(project: Project, artifact_id: int) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT * FROM source_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"source artifact not found: {artifact_id}")
    return {
        "id": int(row["id"]),
        "stable_id": row["stable_id"],
        "artifact_kind": row["artifact_kind"],
        "media_type": row["media_type"],
        "blob_hash": row["blob_hash"],
        "source_url": row["source_url"],
        "canonical_url": row["canonical_url"],
        "title": row["title"],
        "filename": row["filename"],
        "page_count": row["page_count"],
        "duration_ms": row["duration_ms"],
        "source_sheet_id": row["source_sheet_id"],
        "source_row_id": row["source_row_id"],
        "source_column_id": row["source_column_id"],
        "external_ref": _json_loads(row["external_ref_json"], {}),
        "metadata": _json_loads(row["metadata"], {}),
        "created_at": row["created_at"],
    }


def _span_row(project: Project, span_id: int) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT * FROM source_spans WHERE id=?", (span_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"source span not found: {span_id}")
    return {
        "id": int(row["id"]),
        "stable_id": row["stable_id"],
        "artifact_id": int(row["artifact_id"]),
        "span_kind": row["span_kind"],
        "page_start": row["page_start"],
        "page_end": row["page_end"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "bbox": _json_loads(row["bbox_json"], []),
        "selector": _json_loads(row["selector_json"], {}),
        "quote": row["quote"],
        "snippet": row["snippet"],
        "text_layer_hash": row["text_layer_hash"],
        "text_surface_id": row["text_surface_id"],
        "preview": _json_loads(row["preview_json"], {}),
        "metadata": _json_loads(row["metadata"], {}),
        "created_at": row["created_at"],
    }


def _surface_row(project: Project, surface_id: int) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT * FROM text_surfaces WHERE id=?", (surface_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"text surface not found: {surface_id}")
    return {
        "id": int(row["id"]),
        "stable_id": row["stable_id"],
        "surface_kind": row["surface_kind"],
        "text_sheet_id": row["text_sheet_id"],
        "text_row_id": row["text_row_id"],
        "text_column_id": row["text_column_id"],
        "value_ref": _json_loads(row["value_ref_json"], None),
        "content_hash": row["content_hash"],
        "offset_unit": row["offset_unit"],
        "surface_ref": _json_loads(row["surface_ref_json"], {}),
        "created_at": row["created_at"],
    }


def _link_row(project: Project, link_id: int) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT * FROM evidence_links WHERE id=?", (link_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"evidence link not found: {link_id}")
    return {
        "id": int(row["id"]),
        "stable_id": row["stable_id"],
        "subject_kind": row["subject_kind"],
        "subject_ref": _json_loads(row["subject_ref_json"], {}),
        "sheet_id": row["sheet_id"],
        "row_id": row["row_id"],
        "column_id": row["column_id"],
        "run_id": row["run_id"],
        "op_id": row["op_id"],
        "receipt_id": row["receipt_id"],
        "link_role": row["link_role"],
        "status": row["status"],
        "confidence": row["confidence"],
        "pinned": bool(row["pinned"]),
        "producer": _json_loads(row["producer_json"], {}),
        "stale_reason": row["stale_reason"],
        "stale_at": row["stale_at"],
        "metadata": _json_loads(row["metadata"], {}),
        "created_at": row["created_at"],
    }


def _span_id_for_ref(project: Project, span: dict[str, Any]) -> int:
    if "span_id" in span:
        return int(span["span_id"])
    stable_id = span.get("stable_id") or span.get("source_span_ref")
    if stable_id is None:
        raise ValueError("evidence link span requires span_id or stable_id")
    row = project.db.execute(
        "SELECT id FROM source_spans WHERE stable_id=?", (str(stable_id),)
    ).fetchone()
    if row is None:
        raise KeyError(f"source span not found: {stable_id}")
    return int(row["id"])


def _lookup_link(project: Project, evidence_link_ref: int | str) -> sqlite3.Row | None:
    if isinstance(evidence_link_ref, int):
        return project.db.execute(
            "SELECT * FROM evidence_links WHERE id=?", (evidence_link_ref,)
        ).fetchone()
    text = str(evidence_link_ref)
    if text.isdecimal():
        row = project.db.execute(
            "SELECT * FROM evidence_links WHERE id=?", (int(text),)
        ).fetchone()
        if row is not None:
            return row
    return project.db.execute(
        "SELECT * FROM evidence_links WHERE stable_id=?", (text,)
    ).fetchone()


def evidence_link_by_stable_id(project: Project, stable_id: str) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM evidence_links WHERE stable_id=?", (stable_id,)
    ).fetchone()


def _link_summary(
    project: Project, row: sqlite3.Row, *, project_id: str | None
) -> dict[str, Any]:
    summary = project.db.execute(
        """
        SELECT COUNT(*) AS span_count,
               COUNT(DISTINCT sp.artifact_id) AS artifact_count,
               MIN(els.rank) AS first_rank
        FROM evidence_link_spans els
        JOIN source_spans sp ON sp.id=els.span_id
        WHERE els.link_id=?
        """,
        (int(row["id"]),),
    ).fetchone()
    first = project.db.execute(
        """
        SELECT sp.span_kind, sp.snippet, sp.quote, sp.page_start, sp.page_end
        FROM evidence_link_spans els
        JOIN source_spans sp ON sp.id=els.span_id
        WHERE els.link_id=?
        ORDER BY els.rank, sp.id
        LIMIT 1
        """,
        (int(row["id"]),),
    ).fetchone()
    evidence_kind = first["span_kind"] if first is not None else "unknown"
    snippet = None
    if first is not None:
        snippet = first["snippet"] or first["quote"] or _page_snippet(first)
    pid = project_id or project.path.stem
    return {
        "id": int(row["id"]),
        "stable_id": row["stable_id"],
        "export_ref": row["stable_id"],
        "status": row["status"],
        "role": row["link_role"],
        "evidence_kind": evidence_kind,
        "span_count": int(summary["span_count"] or 0),
        "artifact_count": int(summary["artifact_count"] or 0),
        "snippet": snippet,
        "viewer_href": f"/api/projects/{pid}/evidence/links/{row['stable_id']}/viewer",
    }


def _page_snippet(row: sqlite3.Row) -> str | None:
    if row["page_start"] is None:
        return None
    if row["page_end"] is not None and row["page_end"] != row["page_start"]:
        return f"Pages {row['page_start']}-{row['page_end']}"
    return f"Page {row['page_start']}"


def _artifact_payload_from_join(
    row: sqlite3.Row, *, project_id: str | None
) -> dict[str, Any]:
    external_ref = _json_loads(row["external_ref_json"], {})
    metadata = _json_loads(row["artifact_metadata"], {})
    artifact_ref = {
        "kind": "source_artifact",
        "stable_id": row["artifact_stable_id"],
        "artifact_kind": row["artifact_kind"],
        "media_type": row["media_type"],
        "blob": _blob_ref(
            row["blob_hash"],
            filename=row["filename"],
            project_id=project_id,
        ),
        "source_url": row["source_url"],
        "external_ref": external_ref,
    }
    source_cell = None
    if row["source_sheet_id"] is not None or row["source_row_id"] is not None:
        source_cell = {
            "sheet_id": row["source_sheet_id"],
            "row_id": row["source_row_id"],
            "column_id": row["source_column_id"],
        }
    return {
        "id": int(row["artifact_id"]),
        "stable_id": row["artifact_stable_id"],
        "export_ref": row["artifact_stable_id"],
        "artifact_kind": row["artifact_kind"],
        "media_type": row["media_type"],
        "title": row["title"],
        "filename": row["filename"],
        "page_count": row["page_count"],
        "duration_ms": row["duration_ms"],
        "source_url": row["source_url"],
        "canonical_url": row["canonical_url"],
        "source_cell": source_cell,
        "external_ref": external_ref,
        "artifact_ref": artifact_ref,
        "metadata": metadata,
        "spans": [],
        "pages": [],
        # Populated by resolve_evidence_viewer after the join loop (needs
        # the full span set to group runs).
        "runs": [],
    }


def _span_payload(
    row: sqlite3.Row,
    link: sqlite3.Row,
    *,
    blob_metadata: dict[str, Any] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    metadata = _json_loads(row["metadata"], {})
    selector_json = _json_loads(row["selector_json"], {})
    preview = _json_loads(row["preview_json"], {})
    quote = row["quote"]
    snippet = row["snippet"] or quote or preview.get("snippet")
    selector = _selector_payload(row, selector_json)

    # Both affordances only apply to temporal spans. deep_link_url is
    # metadata-only (compose_deep_link degrades to None with no
    # webpage_url -- graceful absence, no clutter). clip_url is offered
    # only when the guard holds (av artifact + a resolvable blob_hash); the
    # clip route re-validates the same guard server-side, this is just
    # affordance discoverability.
    blob_metadata = blob_metadata or {}
    deep_link_url: str | None = None
    clip_url: str | None = None
    if row["span_kind"] == "temporal":
        deep_link_url = compose_deep_link(
            webpage_url=blob_metadata.get("webpage_url"),
            extractor=blob_metadata.get("extractor"),
            yt_dlp_id=blob_metadata.get("yt_dlp_id"),
            start_ms=row["start_ms"],
        )
        if row["artifact_kind"] == "av" and row["blob_hash"]:
            clip_url = (
                f"/api/projects/{project_id or 'local'}"
                f"/evidence/spans/{row['stable_id']}/clip"
            )

    return {
        "id": int(row["id"]),
        "stable_id": row["stable_id"],
        "export_ref": row["stable_id"],
        "span_kind": row["span_kind"],
        "rank": int(row["rank"]),
        "span_role": row["span_role"],
        "required": bool(row["required"]),
        "note": row["note"],
        "status": link["status"],
        "selector": selector,
        "quote": quote,
        "snippet": snippet,
        "text_layer_hash": row["text_layer_hash"],
        "preview": preview,
        "raw": metadata.get("raw") or {},
        "warnings": _list_value(metadata.get("warnings")),
        "deep_link_url": deep_link_url,
        "clip_url": clip_url,
        # Which contiguous cited run (see CITATION_RUN_GAP_TOLERANCE_MS)
        # this span belongs to -- None for an uncited span, or a cited span
        # resolve_evidence_viewer's run pass hasn't reached yet (overwritten
        # right after this loop, once the full span set is known).
        "run_index": None,
        "_page_start": row["page_start"],
        "_page_end": row["page_end"],
        "_bbox": _json_loads(row["bbox_json"], []),
    }


def _selector_payload(
    row: sqlite3.Row, selector_json: dict[str, Any]
) -> dict[str, Any]:
    kind = str(row["span_kind"])
    selector: dict[str, Any] = {"kind": kind}
    if row["page_start"] is not None:
        selector["page_start"] = int(row["page_start"])
    if row["page_end"] is not None:
        selector["page_end"] = int(row["page_end"])
    if row["start_ms"] is not None:
        selector["start_ms"] = int(row["start_ms"])
    if row["end_ms"] is not None:
        selector["end_ms"] = int(row["end_ms"])
    if row["char_start"] is not None:
        selector["char_start"] = int(row["char_start"])
    if row["char_end"] is not None:
        selector["char_end"] = int(row["char_end"])
    bbox = _json_loads(row["bbox_json"], [])
    if bbox:
        selector["bbox"] = bbox
    if kind == "html":
        selector["html"] = selector_json
    elif kind == "table":
        selector["table"] = selector_json
    elif kind == "temporal":
        selector["temporal"] = selector_json
    elif selector_json:
        selector["data"] = selector_json
    return selector


def _page_payloads(
    artifact: dict[str, Any], *, project_id: str | None
) -> list[dict[str, Any]]:
    metadata = (
        artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    )
    page_images = metadata.get("page_images") if isinstance(metadata, dict) else {}
    text_pages = metadata.get("text_pages") if isinstance(metadata, dict) else {}
    if not isinstance(page_images, dict):
        page_images = {}
    if not isinstance(text_pages, dict):
        text_pages = {}
    pages: set[int] = set()
    for span in artifact["spans"]:
        start = span.pop("_page_start", None)
        end = span.pop("_page_end", None)
        span.pop("_bbox", None)
        if start is None:
            continue
        start_int = int(start)
        end_int = int(end if end is not None else start)
        for page in range(start_int, min(end_int, start_int + 100) + 1):
            pages.add(page)
    payloads: list[dict[str, Any]] = []
    for page in sorted(pages):
        image = page_images.get(str(page)) or page_images.get(page)
        if isinstance(image, dict) and image.get("blob_hash"):
            image_payload = {
                "blob_hash": image["blob_hash"],
                "url": _blob_url(image["blob_hash"], project_id=project_id),
                "width": image.get("width"),
                "height": image.get("height"),
            }
        else:
            image_payload = None
        regions = [
            {
                "id": span["id"],
                "stable_id": span["stable_id"],
                "bbox": span["selector"].get("bbox", []),
                "snippet": span.get("snippet"),
                "raw": span.get("raw") or {},
            }
            for span in artifact["spans"]
            if span["span_kind"] == "region"
            and span["selector"].get("page_start") == page
            and span["selector"].get("bbox")
        ]
        payloads.append(
            {
                "page": page,
                "image": image_payload,
                "text": text_pages.get(str(page)) or text_pages.get(page),
                "regions": regions,
            }
        )
    return payloads


def _blob_ref(
    blob_hash: str | None, *, filename: str | None, project_id: str | None
) -> dict[str, Any] | None:
    if not blob_hash:
        return None
    return {
        "hash": blob_hash,
        "url": _blob_url(blob_hash, project_id=project_id),
        "filename": filename,
    }


def _blob_url(blob_hash: str, *, project_id: str | None) -> str:
    pid = project_id or "local"
    return f"/api/projects/{pid}/blobs/{blob_hash}"


def _list_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out

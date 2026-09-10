"""Source rowset resolution for dataset exports.

This is the single owner of "which rows, in which order" for every dataset
export destination (CSV download, ``export.sheet_csv``, ``export.google_sheets``,
JSONL, Parquet). It resolves a sheet source through the same QuerySpec evaluator
the grid uses and yields row-id batches; it never stringifies values.
"""

from __future__ import annotations

from typing import Any, Iterator, NamedTuple

from frisket.querysets import (
    SheetRowSetError,
    resolve_sheet_filter_rows,
    sheet_row_scope_query,
)
from frisket.engine.store import Project
from frisket.features.watchlists.specs import (
    canonical_json,
    normalize_query_spec,
    query_spec_hash,
)

# Bound on the number of exported row ids stored explicitly in a receipt before
# the export switches to a bounded ``exported_rowset`` summary. Matches
# the historical filtered-CSV cap so small exports keep explicit row ids.
MAX_EXPLICIT_EXPORT_ROW_IDS = 10_000

EXPORT_EVALUATOR = {"kind": "frisket.querysets.sheet_filter", "version": "v1"}


class ExportError(Exception):
    """Typed export failure carrying a stable code for destination mapping.

    Destination layers translate these into an ``ActionError`` (executor) or an
    ``HTTPException`` (direct routes) without re-deriving rowset semantics.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}


class ResolvedSheet(NamedTuple):
    sheet_id: int
    sheet_name: str


class ResolvedRowset(NamedTuple):
    # ``row_ids is None`` means "all visible rows in row-position order" and lets
    # the iterator stream/paginate instead of materializing every id up front.
    row_ids: list[int] | None
    query: dict[str, Any] | None
    query_hash: str | None
    total: int | None
    evaluator: dict[str, str] | None


def resolve_sheet(project: Project, sheet_id: int) -> ResolvedSheet:
    row = project.db.execute(
        "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if row is None:
        raise ExportError(
            "invalid_sheet_ref",
            f"sheet_id {sheet_id} does not identify a visible sheet",
            field="params.sheet_id",
            details={"sheet_id": sheet_id},
        )
    return ResolvedSheet(int(row["id"]), str(row["name"]))


def resolve_rowset(
    project: Project,
    sheet_id: int,
    query_spec: dict[str, Any] | None,
    *,
    max_rows: int | None,
    query_field: str = "params.query",
    materialize_small: bool = True,
) -> ResolvedRowset:
    """Resolve a current_sheet (query None) or current_view (query) rowset.

    ``max_rows`` is supplied by the caller so the historical cap stays
    monkeypatchable on the caller module (server app / export action family).
    """
    if query_spec is None:
        if max_rows is not None:
            total = int(
                project.db.execute(
                    "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0",
                    (sheet_id,),
                ).fetchone()[0]
                or 0
            )
            _enforce_limit(total, max_rows, query_field)
        return ResolvedRowset(None, None, None, None, None)
    try:
        query = normalize_query_spec(query_spec)
    except ValueError as exc:
        raise ExportError("invalid_query_spec", str(exc), field=query_field) from exc
    if query["kind"] != "sheet.filter":
        raise ExportError(
            "invalid_query_spec",
            "export query must be a sheet.filter query",
            field=f"{query_field}.kind",
        )
    scope = query.get("scope")
    if not isinstance(scope, dict) or scope.get("sheet_id") != sheet_id:
        raise ExportError(
            "invalid_sheet_ref",
            "export query sheet_id must match the source sheet_id",
            field=f"{query_field}.scope.sheet_id",
        )
    filter_json = canonical_json(query.get("filter", {}))
    sort_json = canonical_json(query["sort"]) if "sort" in query else None
    try:
        _, where_sql, where_params, _order_parts, _order_params = sheet_row_scope_query(
            project, sheet_id, filter_=filter_json, sort=sort_json
        )
        total = int(
            project.db.execute(
                f"SELECT COUNT(*) FROM rows r WHERE {where_sql}", where_params
            ).fetchone()[0]
            or 0
        )
        if max_rows is not None:
            _enforce_limit(total, max_rows, query_field)
    except SheetRowSetError as exc:
        raise ExportError(
            "invalid_query_filter", str(exc), field=f"{query_field}.filter"
        ) from exc
    row_ids: list[int] | None = None
    # Preserve the compact-plan compatibility contract for ordinary views.
    # Only a corpus-sized selected view takes the cursor-backed representation.
    if materialize_small and total <= MAX_EXPLICIT_EXPORT_ROW_IDS:
        row_ids = resolve_sheet_filter_rows(
            project, sheet_id, filter_=filter_json, sort=sort_json, limit=total
        ).row_ids
    return ResolvedRowset(
        # Query rowsets deliberately remain replayable specifications rather
        # than a corpus-sized id list.  The iterator compiles the same scope
        # and holds only one cursor page at a time.
        row_ids=row_ids,
        query=query,
        query_hash=query_spec_hash(query),
        total=total,
        evaluator=dict(EXPORT_EVALUATOR),
    )


def _enforce_limit(total: int, max_rows: int, query_field: str) -> None:
    if total > max_rows:
        raise ExportError(
            "export_rowset_too_large",
            f"sheet export exceeds the hosted row limit of {max_rows}",
            field=query_field,
            details={"row_count": total, "max_rows": max_rows},
        )


def iter_row_id_batches(
    project: Project,
    sheet_id: int,
    row_ids: list[int] | None,
    *,
    batch_size: int = 1000,
    query: dict[str, Any] | None = None,
) -> Iterator[list[int]]:
    """Yield batches of row ids in export order.

    When ``row_ids`` is provided the batches preserve that explicit order;
    otherwise visible rows are streamed in (position, id) order so large
    unfiltered sheets never materialize every id at once.
    """
    if row_ids is not None:
        for start in range(0, len(row_ids), batch_size):
            yield [int(rid) for rid in row_ids[start : start + batch_size]]
        return

    if query is not None:
        filter_json = canonical_json(query.get("filter", {}))
        sort_json = canonical_json(query["sort"]) if "sort" in query else None
        try:
            _, where_sql, where_params, order_parts, order_params = (
                sheet_row_scope_query(
                    project, sheet_id, filter_=filter_json, sort=sort_json
                )
            )
        except SheetRowSetError:
            # Validation already occurred during planning; retain this guard
            # for callers constructing plans manually.
            raise
        cursor = project.db.execute(
            f"SELECT r.id AS row_id FROM rows r WHERE {where_sql} "
            f"ORDER BY {', '.join(order_parts)}",
            [*where_params, *order_params],
        )
        try:
            while batch := cursor.fetchmany(batch_size):
                yield [int(row["row_id"]) for row in batch]
        finally:
            cursor.close()
        return

    last_position: int | None = None
    last_id = 0
    while True:
        where = "sheet_id=? AND hidden=0"
        params: list[Any] = [sheet_id]
        if last_position is not None:
            where += " AND (position, id) > (?, ?)"
            params.extend([last_position, last_id])
        params.append(batch_size)
        batch = project.db.execute(
            f"SELECT id, position FROM rows WHERE {where} "
            "ORDER BY position ASC, id ASC LIMIT ?",
            params,
        ).fetchall()
        if not batch:
            break
        yield [int(row["id"]) for row in batch]
        last_position = int(batch[-1]["position"])
        last_id = int(batch[-1]["id"])

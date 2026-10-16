"""Freshness of named embedding indexes in the unified retrieval sidecar."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.ai.embeddings.scope import (
    IndexScopeError,
    index_scope_plan,
    iter_index_scope_row_pages,
)
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.engine.store import Project
from frisket.retrieval.query import PinnedRetrievalReader, RetrievalQueryError
from frisket.retrieval.store import RetrievalStore

from .unified_index import descriptor_space, recipe_for_index

FRESHNESS_FRESH = "fresh"
FRESHNESS_SCOPE_INVALID = "scope_invalid"
FRESHNESS_NEVER_REFRESHED = "never_refreshed"
FRESHNESS_MISSING_ROWS = "missing_rows"
FRESHNESS_STALE_ROWS = "stale_rows"
FRESHNESS_ERRORED_ITEMS = "errored_items"

_SAMPLE_LIMIT = 20
_MISSING_PAGE_SIZE = 500


@dataclass(frozen=True)
class IndexFreshness:
    """Exact counts plus bounded diagnostic samples."""

    ready_count: int = 0
    stale_count: int = 0
    missing_count: int = 0
    surplus_count: int = 0
    ready_source_key_samples: tuple[str, ...] = ()
    stale_source_key_samples: tuple[str, ...] = ()
    missing_source_key_samples: tuple[str, ...] = ()
    scope_resolved: bool = True
    target_count: int = 0


def freshness_reason(index: Any, fresh: IndexFreshness) -> str:
    if not fresh.scope_resolved:
        return FRESHNESS_SCOPE_INVALID
    if index["last_refreshed_at"] is None:
        return FRESHNESS_NEVER_REFRESHED
    if fresh.missing_count:
        return FRESHNESS_MISSING_ROWS
    if fresh.stale_count or fresh.surplus_count:
        return FRESHNESS_STALE_ROWS
    if index["error_items"]:
        return FRESHNESS_ERRORED_ITEMS
    return FRESHNESS_FRESH


def freshness_error_code(reason: str) -> str | None:
    if reason == FRESHNESS_STALE_ROWS:
        return "embedding_source_stale"
    if reason in (
        FRESHNESS_MISSING_ROWS,
        FRESHNESS_ERRORED_ITEMS,
        FRESHNESS_NEVER_REFRESHED,
    ):
        return "embedding_index_incomplete"
    if reason == FRESHNESS_SCOPE_INVALID:
        return "embedding_index_scope_invalid"
    return None


def embedding_search_freshness_error(
    project: Project, index_id: Any
) -> tuple[str, str] | None:
    from frisket.ai.embeddings.store import EmbeddingStore

    index = EmbeddingStore(project).get_index(index_id)
    if index is None:
        return ("embedding_index_not_found", f"no embedding index {index_id!r}")
    reason = freshness_reason(index, index_freshness(project, index))
    code = freshness_error_code(reason)
    if code is None:
        return None
    return (
        code,
        f"the embedding index is not fresh enough to search ({reason}); "
        "refresh it before searching",
    )


def _safe_columns(index: Any) -> list[str]:
    try:
        parsed = json.loads(index["source_columns_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(column) for column in parsed] if isinstance(parsed, list) else []


def _member_status_sql(plan: Any) -> str:
    current = (
        "part.recipe_id=? AND representation.space_id=? AND "
        "EXISTS (SELECT 1 FROM idx.binding_dependencies AS required "
        "WHERE required.binding_key=binding.binding_key) AND NOT EXISTS ("
        "SELECT 1 FROM idx.binding_dependencies AS dependency "
        "WHERE dependency.binding_key=binding.binding_key "
        "AND dependency.retrieval_revision != COALESCE(("
        "SELECT current_head.retrieval_revision FROM main.current_cells AS current_head "
        "WHERE current_head.column_id=dependency.column_id "
        "AND current_head.row_id=dependency.row_id),0)) AND NOT EXISTS ("
        "SELECT 1 FROM idx.binding_dependencies AS visible_dependency "
        "LEFT JOIN main.columns AS dependency_column "
        "ON dependency_column.id=visible_dependency.column_id "
        "AND dependency_column.sheet_id=binding.sheet_id "
        "WHERE visible_dependency.binding_key=binding.binding_key "
        "AND (dependency_column.id IS NULL OR dependency_column.hidden!=0))"
    )
    return f"""
        WITH scoped_rows AS (
          SELECT DISTINCT r.id AS row_id
          FROM {plan.filter_from_sql}
          WHERE {plan.where_sql}
        ), member_status AS (
          SELECT binding.row_id,
                 MAX(CASE WHEN scoped.row_id IS NOT NULL AND {current}
                          THEN 1 ELSE 0 END) AS is_current,
                 MAX(CASE WHEN scoped.row_id IS NOT NULL THEN 1 ELSE 0 END)
                   AS in_scope
          FROM idx.named_dataset_membership AS member
          JOIN idx.parts AS part ON part.part_key=member.part_key
          JOIN idx.representations AS representation
            ON representation.representation_key=member.representation_key
          JOIN idx.bindings AS binding ON binding.source_key=part.source_key
          LEFT JOIN scoped_rows AS scoped ON scoped.row_id=binding.row_id
          WHERE member.dataset_id=? AND binding.sheet_id=?
          GROUP BY binding.row_id
        ), numbered AS (
          SELECT row_id,is_current,in_scope,
                 ROW_NUMBER() OVER (
                   PARTITION BY is_current ORDER BY row_id
                 ) AS sample_number
          FROM member_status
        )
        SELECT
          COUNT(*) FILTER (WHERE is_current=1) AS ready_count,
          COUNT(*) FILTER (WHERE is_current=0) AS stale_count,
          COUNT(*) FILTER (WHERE in_scope=0) AS surplus_count,
          COALESCE(json_group_array(CAST(row_id AS TEXT)) FILTER (
            WHERE is_current=1 AND sample_number<=?
          ),'[]') AS ready_samples,
          COALESCE(json_group_array(CAST(row_id AS TEXT)) FILTER (
            WHERE is_current=0 AND sample_number<=?
          ),'[]') AS stale_samples
        FROM numbered
    """


def _member_status(
    reader: PinnedRetrievalReader,
    *,
    plan: Any,
    dataset_id: str,
    sheet_id: int,
    recipe_id: str,
    space_id: str,
) -> tuple[int, int, int, tuple[str, ...], tuple[str, ...]]:
    row = reader.db.execute(
        _member_status_sql(plan),
        [
            *plan.filter_params,
            recipe_id,
            space_id,
            dataset_id,
            sheet_id,
            _SAMPLE_LIMIT,
            _SAMPLE_LIMIT,
        ],
    ).fetchone()
    return (
        int(row["ready_count"] or 0),
        int(row["stale_count"] or 0),
        int(row["surplus_count"] or 0),
        tuple(str(value) for value in json.loads(row["ready_samples"])),
        tuple(str(value) for value in json.loads(row["stale_samples"])),
    )


def _stale_scope_row_pages(
    reader: PinnedRetrievalReader,
    *,
    plan: Any,
    dataset_id: str,
    sheet_id: int,
    recipe_id: str,
    space_id: str,
):
    """Yield in-scope rows whose published member is no longer current."""

    current = (
        "part.recipe_id=? AND representation.space_id=? AND "
        "EXISTS (SELECT 1 FROM idx.binding_dependencies AS required "
        "WHERE required.binding_key=binding.binding_key) AND NOT EXISTS ("
        "SELECT 1 FROM idx.binding_dependencies AS dependency "
        "WHERE dependency.binding_key=binding.binding_key "
        "AND dependency.retrieval_revision != COALESCE(("
        "SELECT current_head.retrieval_revision FROM main.current_cells AS current_head "
        "WHERE current_head.column_id=dependency.column_id "
        "AND current_head.row_id=dependency.row_id),0)) AND NOT EXISTS ("
        "SELECT 1 FROM idx.binding_dependencies AS visible_dependency "
        "LEFT JOIN main.columns AS dependency_column "
        "ON dependency_column.id=visible_dependency.column_id "
        "AND dependency_column.sheet_id=binding.sheet_id "
        "WHERE visible_dependency.binding_key=binding.binding_key "
        "AND (dependency_column.id IS NULL OR dependency_column.hidden!=0))"
    )
    offset = 0
    while True:
        rows = reader.db.execute(
            f"""
            WITH scoped_rows AS (
              SELECT DISTINCT r.id AS row_id
              FROM {plan.filter_from_sql}
              WHERE {plan.where_sql}
            )
            SELECT binding.row_id
            FROM idx.named_dataset_membership AS member
            JOIN idx.parts AS part ON part.part_key=member.part_key
            JOIN idx.representations AS representation
              ON representation.representation_key=member.representation_key
            JOIN idx.bindings AS binding ON binding.source_key=part.source_key
            JOIN scoped_rows AS scoped ON scoped.row_id=binding.row_id
            WHERE member.dataset_id=? AND binding.sheet_id=?
            GROUP BY binding.row_id
            HAVING MAX(CASE WHEN {current} THEN 1 ELSE 0 END)=0
            ORDER BY binding.row_id
            LIMIT ? OFFSET ?
            """,
            [
                *plan.filter_params,
                dataset_id,
                sheet_id,
                recipe_id,
                space_id,
                _MISSING_PAGE_SIZE,
                offset,
            ],
        ).fetchall()
        if not rows:
            return
        yield [int(row["row_id"]) for row in rows]
        if len(rows) < _MISSING_PAGE_SIZE:
            return
        offset += len(rows)


def _missing_scope_row_pages(
    reader: PinnedRetrievalReader,
    *,
    plan: Any,
    dataset_id: str,
    sheet_id: int,
):
    """Yield scoped rows without any published membership for this dataset."""

    order_clause = ", ".join(plan.order_parts) or "r.position ASC"
    offset = 0
    while True:
        rows = reader.db.execute(
            f"""
            SELECT r.id
            FROM {plan.from_sql}
            WHERE ({plan.where_sql}) AND NOT EXISTS (
              SELECT 1
              FROM idx.named_dataset_membership AS member
              JOIN idx.parts AS part ON part.part_key=member.part_key
              JOIN idx.bindings AS binding ON binding.source_key=part.source_key
              WHERE member.dataset_id=?
                AND binding.sheet_id=?
                AND binding.row_id=r.id
            )
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            [
                *plan.join_params,
                *plan.where_params,
                dataset_id,
                sheet_id,
                *plan.order_params,
                _MISSING_PAGE_SIZE,
                offset,
            ],
        ).fetchall()
        if not rows:
            return
        yield [int(row["id"]) for row in rows]
        if len(rows) < _MISSING_PAGE_SIZE:
            return
        offset += len(rows)


def index_freshness(project: Project, index: Any) -> IndexFreshness:
    """Read exact counts without reconstructing every ready source payload.

    Published members are authoritative when their stored recipe, embedding
    space, and every binding dependency still match ``current_cells``. Only
    never-published scoped rows use the canonical payload builder, to distinguish
    missing embeddable rows from intentionally blank rows.
    """

    if not RetrievalStore(project).path.is_file():
        from .store import EmbeddingStore

        space = EmbeddingStore(project).get_space(index["space_id"])
        if space is None or index["sheet_id"] is None:
            return IndexFreshness(scope_resolved=False)
        try:
            descriptor, _declared_space = descriptor_space(space)
            recipe_for_index(project, index, descriptor)
            missing = 0
            samples: list[str] = []
            columns = _safe_columns(index)
            visible_columns = {
                str(column["name"])
                for column in project.columns(
                    int(index["sheet_id"]), include_hidden=False
                )
            }
            if all(column in visible_columns for column in columns):
                for row_ids in iter_index_scope_row_pages(project, index):
                    payloads = build_source_payloads(project, index, columns, row_ids)
                    missing += len(payloads)
                    if len(samples) < _SAMPLE_LIMIT:
                        samples.extend(
                            str(payload["source_key"])
                            for payload in payloads[: _SAMPLE_LIMIT - len(samples)]
                        )
            return IndexFreshness(
                missing_count=missing,
                missing_source_key_samples=tuple(samples),
                target_count=missing,
            )
        except (IndexScopeError, KeyError, TypeError, ValueError):
            return IndexFreshness(scope_resolved=False)

    try:
        with PinnedRetrievalReader(project) as reader:
            pinned_index = reader.db.execute(
                "SELECT * FROM main.embedding_indexes WHERE id=?", (index["id"],)
            ).fetchone()
            if pinned_index is None or pinned_index["sheet_id"] is None:
                return IndexFreshness(scope_resolved=False)
            space = reader.db.execute(
                "SELECT * FROM main.embedding_spaces WHERE id=?",
                (pinned_index["space_id"],),
            ).fetchone()
            if space is None:
                return IndexFreshness(scope_resolved=False)
            plan = index_scope_plan(reader, pinned_index)
            if plan is None:
                return IndexFreshness(scope_resolved=False)
            descriptor, _declared_space = descriptor_space(space)
            recipe = recipe_for_index(reader, pinned_index, descriptor)
            dataset_id = str(pinned_index["id"])
            sheet_id = int(pinned_index["sheet_id"])
            ready, stale, _surplus, ready_samples, stale_samples = _member_status(
                reader,
                plan=plan,
                dataset_id=dataset_id,
                sheet_id=sheet_id,
                recipe_id=recipe.recipe_id,
                space_id=str(space["id"]),
            )
            missing = 0
            stale_targets = 0
            missing_samples: list[str] = []
            columns = _safe_columns(pinned_index)
            visible_columns = {
                str(column["name"])
                for column in reader.columns(sheet_id, include_hidden=False)
            }
            if all(column in visible_columns for column in columns):
                for row_ids in _stale_scope_row_pages(
                    reader,
                    plan=plan,
                    dataset_id=dataset_id,
                    sheet_id=sheet_id,
                    recipe_id=recipe.recipe_id,
                    space_id=str(space["id"]),
                ):
                    stale_targets += len(
                        build_source_payloads(reader, pinned_index, columns, row_ids)
                    )
                for row_ids in _missing_scope_row_pages(
                    reader,
                    plan=plan,
                    dataset_id=dataset_id,
                    sheet_id=sheet_id,
                ):
                    payloads = build_source_payloads(
                        reader, pinned_index, columns, row_ids
                    )
                    missing += len(payloads)
                    if len(missing_samples) < _SAMPLE_LIMIT:
                        missing_samples.extend(
                            str(payload["source_key"])
                            for payload in payloads[
                                : _SAMPLE_LIMIT - len(missing_samples)
                            ]
                        )
    except (IndexScopeError, RetrievalQueryError, KeyError, TypeError, ValueError):
        return IndexFreshness(scope_resolved=False)

    return IndexFreshness(
        ready_count=ready,
        stale_count=stale_targets,
        missing_count=missing,
        surplus_count=stale - stale_targets,
        ready_source_key_samples=ready_samples,
        stale_source_key_samples=stale_samples,
        missing_source_key_samples=tuple(missing_samples),
        scope_resolved=True,
        target_count=ready + stale_targets + missing,
    )


def stale_ready_source_key_samples(
    project: Project, index: Any, source_columns: list[str] | None = None
) -> tuple[str, ...]:
    """Return bounded stale-key diagnostics for a named index."""

    del source_columns
    return index_freshness(project, index).stale_source_key_samples

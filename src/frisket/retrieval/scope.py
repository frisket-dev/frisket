"""Compile retrieval authorization scopes into normal SQLite relations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from frisket.querysets import sheet_row_scope_plan

from .contracts import RetrievalScope


class ScopeReader(Protocol):
    db: sqlite3.Connection

    def columns(
        self, sheet_id: int, include_hidden: bool = False
    ) -> list[sqlite3.Row]: ...


@dataclass(frozen=True)
class CompiledRetrievalScope:
    """A correlated row/column authorization relation and its ordered params."""

    cte_sql: str
    params: tuple[Any, ...]


def compile_scope(reader: ScopeReader, scope: RetrievalScope) -> CompiledRetrievalScope:
    """Compile scope against the query-owned pinned project connection.

    Each input sheet scope remains an independent union branch. This preserves a
    union such as "all columns in row A plus column X in row B" without turning
    the branch-specific row and column rules into a cross-product.
    """

    if scope.is_empty:
        return _empty_scope()
    if scope.sheets is None:
        return CompiledRetrievalScope(
            "scoped_bindings(sheet_id,row_id,column_id) AS NOT MATERIALIZED ("
            "SELECT r.sheet_id,r.id,NULL FROM rows AS r "
            "JOIN sheets AS s ON s.id=r.sheet_id "
            "WHERE r.hidden=0 AND s.hidden=0)",
            (),
        )

    selects: list[str] = []
    params: list[Any] = []
    for sheet_scope in scope.sheets:
        if sheet_scope.is_empty:
            continue
        plan = sheet_row_scope_plan(
            reader,
            sheet_scope.sheet_id,
            parent_row_id=sheet_scope.parent_row_id,
            filter_=sheet_scope.filter_json,
        )
        joins = [
            "JOIN sheets AS retrieval_sheet "
            "ON retrieval_sheet.id=r.sheet_id AND retrieval_sheet.hidden=0"
        ]
        select_params: list[Any] = [*plan.filter_join_params]
        if sheet_scope.row_ids is not None:
            joins.append(
                "JOIN json_each(?) AS selected "
                "ON selected.type='integer' AND selected.value=r.id"
            )
            select_params.append(json.dumps(sheet_scope.row_ids))
        selected_column = "NULL"
        if sheet_scope.column_ids is not None:
            joins.append(
                "JOIN json_each(?) AS allowed_column ON allowed_column.type='integer'"
            )
            select_params.append(json.dumps(sheet_scope.column_ids))
            selected_column = "allowed_column.value"
        select_params.extend(plan.where_params)
        selects.append(
            f"SELECT r.sheet_id,r.id,{selected_column} FROM "
            f"{plan.filter_from_sql} {' '.join(joins)} WHERE {plan.where_sql}"
        )
        params.extend(select_params)

    if not selects:
        return _empty_scope()
    return CompiledRetrievalScope(
        "scoped_bindings(sheet_id,row_id,column_id) AS NOT MATERIALIZED ("
        + " UNION ALL ".join(selects)
        + ")",
        tuple(params),
    )


def _empty_scope() -> CompiledRetrievalScope:
    return CompiledRetrievalScope(
        "scoped_bindings(sheet_id,row_id,column_id) AS (SELECT NULL,NULL,NULL WHERE 0)",
        (),
    )

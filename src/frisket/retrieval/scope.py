"""Compile retrieval authorization scopes into normal SQLite relations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from frisket.querysets import sheet_row_scope_plan

from .contracts import RetrievalScope, SheetScope


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

    Scopes sharing a sheet/filter plan use one JSON relation. Each JSON item
    retains its own row and column restrictions, preserving a union such as
    "all columns in row A plus column X in row B" without turning independent
    rules into a cross-product or one compound-SELECT branch per selected cell.
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

    materialization = (
        "MATERIALIZED"
        if any(item.row_ids is not None and not item.is_empty for item in scope.sheets)
        else "NOT MATERIALIZED"
    )
    selects: list[str] = []
    params: list[Any] = []
    grouped_scopes: dict[tuple[int, int | None, str | None], list[SheetScope]] = {}
    for sheet_scope in scope.sheets:
        if not sheet_scope.is_empty:
            key = (
                sheet_scope.sheet_id,
                sheet_scope.parent_row_id,
                sheet_scope.filter_json,
            )
            grouped_scopes.setdefault(key, []).append(sheet_scope)

    for (sheet_id, parent_row_id, filter_json), plan_scopes in grouped_scopes.items():
        plan = sheet_row_scope_plan(
            reader,
            sheet_id,
            parent_row_id=parent_row_id,
            filter_=filter_json,
        )
        shaped_scopes: dict[tuple[bool, bool], list[SheetScope]] = {}
        for plan_scope in plan_scopes:
            shape = (plan_scope.row_ids is not None, plan_scope.column_ids is not None)
            shaped_scopes.setdefault(shape, []).append(plan_scope)
        for (has_rows, has_columns), scoped_items in shaped_scopes.items():
            restrictions = []
            for plan_scope in scoped_items:
                restriction: dict[str, tuple[int, ...]] = {}
                if plan_scope.row_ids is not None:
                    restriction["row_ids"] = plan_scope.row_ids
                if plan_scope.column_ids is not None:
                    restriction["column_ids"] = plan_scope.column_ids
                restrictions.append(restriction)
            rows_join = "CROSS JOIN rows AS r"
            row_predicate = ""
            if has_rows:
                rows_join = (
                    "JOIN json_each(requested_scope.value,'$.row_ids') AS selected "
                    "ON selected.type='integer' CROSS JOIN rows AS r"
                )
                row_predicate = " AND r.id=selected.value"
            columns_join = ""
            selected_column = "NULL"
            if has_columns:
                columns_join = (
                    "JOIN json_each(requested_scope.value,'$.column_ids') "
                    "AS allowed_column ON allowed_column.type='integer'"
                )
                selected_column = "allowed_column.value"
            select_params: list[Any] = [
                json.dumps(restrictions, separators=(",", ":")),
                *plan.filter_join_params,
            ]
            select_params.extend(plan.where_params)
            selects.append(
                f"SELECT r.sheet_id,r.id,{selected_column} FROM json_each(?) "
                f"AS requested_scope {columns_join} "
                f"{rows_join} {' '.join(plan.filter_joins)} "
                "JOIN sheets AS retrieval_sheet "
                "ON retrieval_sheet.id=r.sheet_id AND retrieval_sheet.hidden=0 "
                f"WHERE {plan.where_sql}{row_predicate}"
            )
            params.extend(select_params)

    if not selects:
        return _empty_scope()
    return CompiledRetrievalScope(
        f"scoped_bindings(sheet_id,row_id,column_id) AS {materialization} ("
        + " UNION ALL ".join(selects)
        + ")",
        tuple(params),
    )


def _empty_scope() -> CompiledRetrievalScope:
    return CompiledRetrievalScope(
        "scoped_bindings(sheet_id,row_id,column_id) AS (SELECT NULL,NULL,NULL WHERE 0)",
        (),
    )

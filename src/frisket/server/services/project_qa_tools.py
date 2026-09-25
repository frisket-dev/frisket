"""The deliberately small, scope-enforcing read surface for Project Ask."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore


MAX_READ_ROWS = 50
MAX_VALUE_CHARS = 2_000
MAX_OBSERVATION_CHARS = 20_000


class ProjectQAScopeError(ValueError):
    """A requested project read falls outside the turn's frozen scope."""


def validate_scope(project: Project, scope: Mapping[str, Any]) -> None:
    """Refuse source references that are not visible project metadata.

    This is intentionally a metadata-only admission check.  The actual cell
    read boundary remains ``ProjectQATools``, which also enforces the frozen
    turn scope on every individual tool call.
    """

    if scope.get("kind") == "project":
        return
    if scope.get("kind") != "sources" or not isinstance(scope.get("sources"), list):
        raise ProjectQAScopeError("scope is not a supported source selection")
    visible_sheets = {int(sheet["id"]) for sheet in project.sheets()}
    for source in scope["sources"]:
        if not isinstance(source, Mapping):
            raise ProjectQAScopeError("scope source is invalid")
        sheet_id = source.get("sheet_id")
        if (
            isinstance(sheet_id, bool)
            or not isinstance(sheet_id, int)
            or sheet_id not in visible_sheets
        ):
            raise ProjectQAScopeError("scope references a hidden or missing sheet")
        source_kind = source.get("kind")
        if source_kind == "sheet":
            continue
        visible_rows = set(project.visible_row_ids(sheet_id))
        if source_kind == "rows":
            row_ids = source.get("row_ids")
            if (
                not isinstance(row_ids, list)
                or not all(
                    isinstance(row_id, int) and not isinstance(row_id, bool)
                    for row_id in row_ids
                )
                or not set(row_ids) <= visible_rows
            ):
                raise ProjectQAScopeError("scope references a hidden or missing row")
        elif source_kind == "file":
            row_id, column_id = source.get("row_id"), source.get("column_id")
            visible_columns = {
                int(column["id"]) for column in project.columns(sheet_id)
            }
            if (
                isinstance(row_id, bool)
                or not isinstance(row_id, int)
                or isinstance(column_id, bool)
                or not isinstance(column_id, int)
                or row_id not in visible_rows
                or column_id not in visible_columns
            ):
                raise ProjectQAScopeError("scope references a hidden or missing cell")
        else:
            raise ProjectQAScopeError("scope source kind is unsupported")


class ProjectQATools:
    """Read project cells only through the immutable submitted turn scope."""

    def __init__(
        self, project: Project, turn: Mapping[str, Any], store: ProjectQAStore
    ) -> None:
        self.project = project
        self.turn = dict(turn)
        self.store = store
        self.turn_id = str(turn["id"])
        self._citation_ids: set[str] = set()
        validate_scope(project, self.turn["scope"])

    @property
    def citation_ids(self) -> frozenset[str]:
        return frozenset(self._citation_ids)

    def inspect_sheets(self) -> dict[str, Any]:
        """Return schema/count metadata for sheets the turn may inspect."""

        allowed = self._allowed_sheets()
        out = []
        for sheet in self.project.sheets():
            sheet_id = int(sheet["id"])
            if allowed is not None and sheet_id not in allowed:
                continue
            allowed_rows, file_cells = self._sheet_access(sheet_id)
            columns = self._visible_columns(sheet_id, allowed_rows, file_cells)
            entry = {
                "sheet_id": sheet_id,
                "name": str(sheet["name"]),
                "row_count": self._visible_count(sheet_id),
                "columns": [
                    {
                        "column_id": int(column["id"]),
                        "name": str(column["name"]),
                        "type": str(column["type"]),
                    }
                    for column in columns
                ],
            }
            out.append(entry)
        return {"sheets": out}

    def read_rows(
        self,
        sheet_id: int,
        row_ids: list[int] | None = None,
        column_ids: list[int] | None = None,
        limit: int = MAX_READ_ROWS,
    ) -> dict[str, Any]:
        """Read a bounded, scope-authorized cell set and mint citations."""

        if isinstance(sheet_id, bool) or not isinstance(sheet_id, int):
            raise ProjectQAScopeError("sheet_id must be an integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_READ_ROWS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_READ_ROWS}")
        allowed_rows, file_cells = self._sheet_access(sheet_id)
        selected_rows = self._rows_for_read(
            sheet_id, row_ids, allowed_rows, file_cells, limit
        )
        requested_columns = self._requested_columns(sheet_id, column_ids)

        column_by_id = {
            int(column["id"]): column for column in self.project.columns(sheet_id)
        }
        rows: list[dict[str, Any]] = []
        remaining = MAX_OBSERVATION_CHARS
        observation_truncated = False
        for row_id in selected_rows:
            selected_columns = self._columns_for_row(
                requested_columns, row_id, allowed_rows, file_cells
            )
            cells: list[dict[str, Any]] = []
            for column_id in selected_columns:
                if remaining <= 0:
                    observation_truncated = True
                    break
                value, refs = self.project.get_values_with_refs(
                    sheet_id, column_id, row_ids=[row_id], include_validity=True
                )
                if row_id not in value:
                    continue
                cell_value = value[row_id]
                rendered = json.dumps(cell_value, ensure_ascii=False, default=str)
                allowed_chars = min(MAX_VALUE_CHARS, remaining)
                truncated = len(rendered) > allowed_chars
                observation_truncated = observation_truncated or truncated
                excerpt = rendered[:allowed_chars] + ("…" if truncated else "")
                remaining -= len(excerpt)
                column = column_by_id[column_id]
                citation = self.store.add_citation(
                    self.turn_id,
                    label=f"{self._sheet_name(sheet_id)} · row {row_id} · {column['name']}",
                    source_kind="cell",
                    locator={
                        "sheet_id": sheet_id,
                        "row_id": row_id,
                        "column_id": column_id,
                        "value_ref": refs.get(row_id),
                    },
                    excerpt=excerpt,
                    metadata={"truncated": truncated},
                )
                self._citation_ids.add(citation["id"])
                cells.append(
                    {
                        "column_id": column_id,
                        "name": str(column["name"]),
                        "value": cell_value if not truncated else excerpt,
                        "citation_id": citation["id"],
                        "truncated": truncated,
                    }
                )
            rows.append({"row_id": row_id, "cells": cells})
            if observation_truncated:
                break
        return {
            "sheet_id": sheet_id,
            "rows": rows,
            "observation_truncated": observation_truncated,
        }

    def _allowed_sheets(self) -> set[int] | None:
        scope = self.turn["scope"]
        if scope["kind"] == "project":
            return None
        return {int(source["sheet_id"]) for source in scope["sources"]}

    def _sheet_access(
        self, sheet_id: int
    ) -> tuple[set[int] | None, set[tuple[int, int]]]:
        if (
            self._allowed_sheets() is not None
            and sheet_id not in self._allowed_sheets()
        ):
            raise ProjectQAScopeError("sheet is outside this turn's scope")
        scope = self.turn["scope"]
        if scope["kind"] == "project":
            return None, set()
        rows: set[int] = set()
        whole_sheet = False
        file_cells: set[tuple[int, int]] = set()
        for source in scope["sources"]:
            if int(source["sheet_id"]) != sheet_id:
                continue
            if source["kind"] == "sheet":
                whole_sheet = True
            elif source["kind"] == "rows":
                rows.update(int(row_id) for row_id in source["row_ids"])
            elif source["kind"] == "file":
                file_cells.add((int(source["row_id"]), int(source["column_id"])))
        return (None if whole_sheet else rows), file_cells

    def _rows_for_read(
        self,
        sheet_id: int,
        requested: list[int] | None,
        allowed_rows: set[int] | None,
        file_cells: set[tuple[int, int]],
        limit: int,
    ) -> list[int]:
        if requested is not None:
            if not all(
                isinstance(row_id, int) and not isinstance(row_id, bool)
                for row_id in requested
            ):
                raise ProjectQAScopeError("row_ids must contain integers")
            candidate = list(dict.fromkeys(requested))
            permitted_rows = (allowed_rows or set()) | {
                row_id for row_id, _column_id in file_cells
            }
            if allowed_rows is not None and not set(candidate) <= permitted_rows:
                raise ProjectQAScopeError("row is outside this turn's scope")
            if len(candidate) > limit:
                raise ValueError(f"read may include at most {limit} rows")
        elif allowed_rows is not None:
            candidate = sorted(
                allowed_rows | {row_id for row_id, _column_id in file_cells}
            )[:limit]
        else:
            candidate = [
                int(row["id"])
                for row in self.project.db.execute(
                    "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position LIMIT ?",
                    (sheet_id, limit),
                )
            ]
        return self.project.visible_row_ids(sheet_id, candidate)

    def _requested_columns(
        self,
        sheet_id: int,
        requested: list[int] | None,
    ) -> list[int]:
        available = {int(column["id"]) for column in self.project.columns(sheet_id)}
        if requested is None:
            selected = sorted(available)
        else:
            if not all(
                isinstance(column_id, int) and not isinstance(column_id, bool)
                for column_id in requested
            ):
                raise ProjectQAScopeError("column_ids must contain integers")
            selected = list(dict.fromkeys(requested))
        if not set(selected) <= available:
            raise ProjectQAScopeError("column is not in sheet")
        return selected

    def _visible_columns(
        self,
        sheet_id: int,
        allowed_rows: set[int] | None,
        file_cells: set[tuple[int, int]],
    ) -> list[Any]:
        columns = self.project.columns(sheet_id)
        if allowed_rows is None or allowed_rows:
            return columns
        allowed_column_ids = {column_id for _row_id, column_id in file_cells}
        return [column for column in columns if int(column["id"]) in allowed_column_ids]

    @staticmethod
    def _columns_for_row(
        requested_columns: list[int],
        row_id: int,
        allowed_rows: set[int] | None,
        file_cells: set[tuple[int, int]],
    ) -> list[int]:
        """A file scope can widen only its one selected cell, never its row."""

        if allowed_rows is None or row_id in allowed_rows:
            return requested_columns
        allowed_file_columns = {
            column_id for file_row_id, column_id in file_cells if file_row_id == row_id
        }
        selected = [
            column_id
            for column_id in requested_columns
            if column_id in allowed_file_columns
        ]
        if not selected:
            raise ProjectQAScopeError("file scope may read only the selected file cell")
        return selected

    def _visible_count(self, sheet_id: int) -> int:
        allowed_rows, file_cells = self._sheet_access(sheet_id)
        if allowed_rows is None:
            return self.project.row_count(sheet_id)
        row_ids = allowed_rows | {row_id for row_id, _column_id in file_cells}
        return len(self.project.visible_row_ids(sheet_id, sorted(row_ids)))

    def _sheet_name(self, sheet_id: int) -> str:
        row = self.project.db.execute(
            "SELECT name FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()
        return str(row["name"]) if row is not None else f"Sheet {sheet_id}"

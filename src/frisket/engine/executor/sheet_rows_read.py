"""Request-scoped ordinary-row admission for typed table producers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from typing import Any

from pydantic import BaseModel

from frisket.actions.types import (
    Row,
    RowSource,
    SheetRowInput,
    SheetRows,
    TableError,
)
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    validate_typed_project_references,
)


_READ_BATCH_SIZE = 500


class AdmittedSheetRowsReader:
    """Read one request-owned row selection without exposing project access."""

    def __init__(self, project: Any, *, scope: SheetRows, params: BaseModel) -> None:
        if not isinstance(scope, SheetRows):
            raise TypeError("sheet row reader requires a sheet_rows request scope")
        try:
            references = validate_typed_project_references(
                project, scope.sheet_id, params
            )
        except TypedMapRowsPlanError as exc:
            raise TableError(exc.code, str(exc), details=dict(exc.details)) from exc
        self._project = project
        self._scope = scope
        self._column_ids = dict(references.source_column_ids)
        self._column_types = dict(references.source_column_types)
        self.sources: set[RowSource] = set()
        self.parent_sheet_id: int | None = None
        self.facts: list[dict[str, Any]] = []
        self._read = False
        self._iterator: Iterator[SheetRowInput] | None = None

    def read(self) -> Iterable[SheetRowInput]:
        if self._read:
            raise TableError("invalid_input_ref", "Sheet rows may be read only once")
        self._read = True
        self.parent_sheet_id = self._scope.sheet_id
        self._iterator = self._rows()
        return self._iterator

    def close(self) -> None:
        iterator = self._iterator
        self._iterator = None
        close = getattr(iterator, "close", None)
        if callable(close):
            close()

    def _rows(self) -> Iterator[SheetRowInput]:
        scope = self._scope
        with self._project.read_snapshot() as project:
            requested = list(scope.row_ids) if scope.row_ids is not None else None
            row_ids = project.visible_row_ids(scope.sheet_id, requested)
            if requested is not None and set(row_ids) != set(requested):
                raise TableError(
                    "invalid_input_ref",
                    "Selected rows are no longer visible on the source sheet",
                )
            fact: dict[str, Any] = {
                "kind": "sheet_rows_read",
                "sheet_id": scope.sheet_id,
                "row_ids": row_ids,
                "read_row_ids": [],
                "columns": [
                    {
                        "name": name,
                        "column_id": column_id,
                        "type": self._column_types[name],
                    }
                    for name, column_id in self._column_ids.items()
                ],
            }
            # Publication can legitimately stop after its first source row.
            # Record the admitted scope before yielding so that early consumers
            # never publish a sourced row with no corresponding read fact.
            self.facts.append(fact)
            digest = hashlib.sha256()
            read_row_ids = fact["read_row_ids"]
            try:
                for start in range(0, len(row_ids), _READ_BATCH_SIZE):
                    page = row_ids[start : start + _READ_BATCH_SIZE]
                    values_by_column = {
                        name: project.get_values(
                            scope.sheet_id, column_id, row_ids=page
                        )
                        for name, column_id in self._column_ids.items()
                    }
                    for row_id in page:
                        values = {
                            name: values_by_column[name].get(row_id)
                            for name in self._column_ids
                        }
                        digest.update(
                            json.dumps(
                                {"row_id": row_id, "values": values},
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode()
                        )
                        source = RowSource(sheet_id=scope.sheet_id, row_id=row_id)
                        self.sources.add(source)
                        read_row_ids.append(row_id)
                        fact["source_values_hash"] = "sha256:" + digest.hexdigest()
                        yield SheetRowInput(row=Row(values), source=source)
            finally:
                fact["source_values_hash"] = "sha256:" + digest.hexdigest()
                if read_row_ids == row_ids:
                    fact.pop("read_row_ids")

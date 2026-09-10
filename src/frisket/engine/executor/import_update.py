"""Exact-key planning shared by imported-row update preview and publication."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

from frisket.contracts.action import canonical_column_type
from frisket.engine.runner.confirmation_context import (
    ParamsScope,
    mint_confirmation_hash,
    scope_confirmation,
)


class TypedRowsPreflight(Protocol):
    def __call__(
        self,
        project: Any,
        *,
        column_types_by_name: Mapping[str, str],
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]: ...


def _normalized_import_value(value: Any) -> Any:
    """Treat whitespace-only imported text as blank without trimming content."""

    if isinstance(value, str) and not value.strip():
        return None
    return value


def _component(value: Any, type_name: str) -> str | None:
    value = _normalized_import_value(value)
    if value is None:
        return None
    canonical_type = canonical_column_type(type_name)
    if canonical_type == "number":
        value = float(value)
    elif canonical_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid integer key")
    return (
        canonical_type
        + ":"
        + json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


@dataclass(frozen=True)
class ImportUpdatePlan:
    matched: int
    unmatched: int
    blank_keys: int
    ambiguous: int
    changed_cells: int
    cleared_cells: int
    targets: tuple[dict[str, Any], ...]
    samples: tuple[dict[str, Any], ...]
    confirmation: str


class ImportUpdateAmbiguous(ValueError):
    pass


def plan_import_update(
    project: Any,
    *,
    sheet_id: int,
    columns: Iterable[Any],
    key_columns: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    keep_existing_on_blank: bool,
    preflight_rows: TypedRowsPreflight,
    sample_limit: int = 20,
    max_rows: int | None = None,
) -> ImportUpdatePlan:
    """Resolve exact live-value matches and bind all reviewed state in one token."""

    declared = list(columns)
    keys = tuple(key_columns)
    by_name = {column.name: column for column in declared}
    update_names = tuple(name for name in by_name if name not in keys)
    stored = project.db.execute(
        "SELECT c.id,c.name,c.type,c.hidden,s.hidden AS sheet_hidden,s.parent_sheet_id "
        "FROM sheets s JOIN columns c ON c.sheet_id=s.id WHERE s.id=?",
        (sheet_id,),
    ).fetchall()
    if not stored or int(stored[0]["sheet_hidden"]):
        raise LookupError("sheet_not_found")
    if stored[0]["parent_sheet_id"] is not None:
        raise LookupError("derived_update_unsupported")
    destination = {str(row["name"]): row for row in stored if not int(row["hidden"])}
    for column in declared:
        target = destination.get(column.name)
        if target is None or canonical_column_type(
            str(target["type"])
        ) != canonical_column_type(column.type):
            raise LookupError("incompatible_update_mapping")

    visible_rows = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position,id",
            (sheet_id,),
        )
    ]
    live: dict[str, dict[int, Any]] = {}
    live_refs: dict[str, dict[int, Any]] = {}
    for name in by_name:
        values, refs = project.get_values_with_refs(
            sheet_id, int(destination[name]["id"]), row_ids=visible_rows
        )
        live[name] = values
        live_refs[name] = refs
    index: dict[tuple[str, ...], list[int]] = {}
    destination_snapshot: list[list[Any]] = []
    for row_id in visible_rows:
        parts = tuple(
            _component(live[name].get(row_id), str(destination[name]["type"]))
            for name in keys
        )
        destination_snapshot.append(
            [row_id, *[live[name].get(row_id) for name in keys + update_names]]
        )
        if all(part is not None for part in parts):
            index.setdefault(parts, []).append(row_id)  # type: ignore[arg-type]

    targets: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    matched = unmatched = blank_keys = ambiguous = cleared = 0
    records: list[dict[str, Any]] = []
    type_names = {name: str(destination[name]["type"]) for name in by_name}
    for row_number, record in enumerate(rows, 1):
        if max_rows is not None and row_number > max_rows:
            raise OverflowError(row_number)
        normalized = {
            name: _normalized_import_value(value) for name, value in record.items()
        }
        records.append(
            preflight_rows(project, column_types_by_name=type_names, rows=[normalized])[
                0
            ]
        )
    source_key_counts: dict[tuple[str, ...], int] = {}
    source_hasher = hashlib.sha256()
    for record in records:
        source_hasher.update(
            json.dumps(
                record, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        )
        source_hasher.update(b"\n")
        parts = tuple(
            _component(record.get(name), str(by_name[name].type)) for name in keys
        )
        if all(part is not None for part in parts):
            source_key_counts[parts] = source_key_counts.get(parts, 0) + 1  # type: ignore[arg-type]
    for source_row, record in enumerate(records, 1):
        parts = tuple(
            _component(record.get(name), str(by_name[name].type)) for name in keys
        )
        status = "unmatched"
        target_row_id: int | None = None
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        if any(part is None for part in parts):
            blank_keys += 1
            unmatched += 1
            status = "blank_key"
        else:
            typed_parts = parts  # narrowed by the branch above
            candidates = index.get(typed_parts, [])  # type: ignore[arg-type]
            if len(candidates) > 1 or source_key_counts[typed_parts] > 1:  # type: ignore[index]
                ambiguous += 1
                status = "ambiguous"
            elif not candidates:
                unmatched += 1
            else:
                matched += 1
                status = "matched"
                target_row_id = candidates[0]
                for name in update_names:
                    old = live[name].get(target_row_id)
                    supplied = record.get(name)
                    blank = supplied is None
                    new = None if blank else supplied
                    before[name] = old
                    after[name] = old if keep_existing_on_blank and blank else new
                    if keep_existing_on_blank and blank:
                        continue
                    if old != new:
                        if new is None:
                            cleared += 1
                        targets.append(
                            {
                                "sheet_id": sheet_id,
                                "row_id": target_row_id,
                                "column_id": int(destination[name]["id"]),
                                "column_name": name,
                                "value_before": old,
                                "current_value_ref": live_refs[name].get(target_row_id),
                                "value_after": new,
                            }
                        )
        if len(samples) < sample_limit:
            samples.append(
                {
                    "source_row": source_row,
                    "key": {name: record.get(name) for name in keys},
                    "status": status,
                    "target_row_id": target_row_id,
                    "before": before,
                    "after": after,
                }
            )

    bindings = {
        "sheet_id": sheet_id,
        "columns": [
            [name, int(destination[name]["id"]), str(destination[name]["type"])]
            for name in by_name
        ],
        "destination": destination_snapshot,
        "source_hash": source_hasher.hexdigest(),
        "key_columns": list(keys),
        "keep_existing_on_blank": keep_existing_on_blank,
    }
    confirmation = mint_confirmation_hash(
        scope_confirmation(
            family_kind="import.update_rows",
            scope=ParamsScope(
                params={"columns": list(by_name), "key_columns": list(keys)}
            ),
            bindings=bindings,
        )
    )
    return ImportUpdatePlan(
        matched,
        unmatched,
        blank_keys,
        ambiguous,
        len(targets),
        cleared,
        tuple(targets),
        tuple(samples),
        confirmation,
    )

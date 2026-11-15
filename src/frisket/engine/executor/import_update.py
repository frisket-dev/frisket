"""Exact-key planning shared by imported-row update preview and publication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

from frisket.contracts.action import canonical_column_type
from frisket.engine.store.keyed_updates import (
    ValidatedKeyedInput,
    changed_fields,
    observation,
    canonical,
    UpdatePolicy,
    retained_hidden_targets,
    typed_key,
)
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
    observations: tuple[dict[str, Any], ...] = ()
    key_definition: tuple[tuple[int, str], ...] = ()
    restored_rows: tuple[int, ...] = ()


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
    relationship_id: str | None = None,
    update_policy: UpdatePolicy = "keep_changes",
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

    targets: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    restored_rows: list[int] = []
    samples: list[dict[str, Any]] = []
    matched = unmatched = blank_keys = ambiguous = cleared = 0
    type_names = {name: str(destination[name]["type"]) for name in by_name}
    destination_hash = hashlib.sha256()
    with ValidatedKeyedInput() as staged:
        # Index existing current keys in bounded batches. Historical hidden keys
        # never intercept a different visible row carrying the current key.
        cursor = project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position,id",
            (sheet_id,),
        )
        while batch := cursor.fetchmany(500):
            row_ids = [int(row[0]) for row in batch]
            current = {
                name: project.get_values(
                    sheet_id, int(destination[name]["id"]), row_ids
                )
                for name in by_name
            }
            keys_for_batch = []
            for row_id in row_ids:
                parts = tuple(
                    (canonical_column_type(type_names[name]), current[name].get(row_id))
                    for name in keys
                )
                key = typed_key(parts)
                keys_for_batch.append((key, row_id))
                destination_hash.update(
                    canonical(
                        [row_id, {name: current[name].get(row_id) for name in by_name}]
                    ).encode()
                    + b"\n"
                )
            staged.add_targets(keys_for_batch)

        def prepared():
            for row_number, record in enumerate(rows, 1):
                if max_rows is not None and row_number > max_rows:
                    raise OverflowError(row_number)
                normalized = {
                    name: _normalized_import_value(value)
                    for name, value in record.items()
                }
                record = preflight_rows(
                    project, column_types_by_name=type_names, rows=[normalized]
                )[0]
                parts = tuple(
                    (canonical_column_type(str(by_name[name].type)), record.get(name))
                    for name in keys
                )
                key = typed_key(parts)
                yield key, record

        staged.stage(prepared())
        source_hash = staged.seal()
        for item in staged.records():
            record = item.record
            status = "unmatched"
            target_row_id = None
            before = {}
            after = {}
            if item.key is None:
                blank_keys += item.copies
                unmatched += item.copies
                status = "blank_key"
            else:
                candidates = staged.targets(item.key)
                hidden_target = False
                if not candidates and relationship_id is not None:
                    candidates = retained_hidden_targets(
                        project.db, relationship_id, item.key
                    )
                    hidden_target = bool(candidates)
                if item.conflicting or len(candidates) > 1:
                    ambiguous += item.copies
                    status = "ambiguous"
                elif not candidates:
                    unmatched += 1
                else:
                    matched += 1
                    status = "matched"
                    target_row_id = candidates[0]
                    live = {}
                    refs = {}
                    for name in update_names:
                        values, value_refs = project.get_values_with_refs(
                            sheet_id,
                            int(destination[name]["id"]),
                            row_ids=[target_row_id],
                        )
                        if (
                            value_refs.get(target_row_id, {}).get("kind") != "missing"
                            and target_row_id in values
                        ):
                            live[str(destination[name]["id"])] = values[target_row_id]
                        refs[name] = value_refs.get(target_row_id)
                    incoming = {
                        str(destination[name]["id"]): value
                        for name, value in record.items()
                        if not (keep_existing_on_blank and value is None)
                    }
                    baseline = observation(project.db, relationship_id, target_row_id)
                    changes = changed_fields(live, incoming, baseline, update_policy)
                    if hidden_target:
                        source_changed = baseline is None or any(
                            name not in baseline
                            or canonical(value) != canonical(baseline[name])
                            for name, value in incoming.items()
                        )
                        if update_policy == "keep_changes" or not source_changed:
                            changes = {}
                        else:
                            restored_rows.append(target_row_id)
                    destination_hash.update(
                        canonical([target_row_id, baseline]).encode() + b"\n"
                    )
                    observations.append(
                        {"row_id": target_row_id, "key": item.key, "record": incoming}
                    )
                    for name in update_names:
                        column_id = int(destination[name]["id"])
                        old = live.get(str(column_id))
                        before[name] = old
                        after[name] = changes.get(str(column_id), old)
                        if str(column_id) not in changes:
                            continue
                        new = changes[str(column_id)]
                        cleared += int(new is None)
                        targets.append(
                            {
                                "sheet_id": sheet_id,
                                "row_id": target_row_id,
                                "column_id": column_id,
                                "column_name": name,
                                "value_before": old,
                                "current_value_ref": refs[name],
                                "value_after": new,
                            }
                        )
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "source_row": item.ordinal,
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
        "destination_hash": destination_hash.hexdigest(),
        "source_hash": source_hash,
        "relationship_id": relationship_id,
        "update_policy": update_policy,
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
        tuple(observations),
        tuple(
            (int(destination[name]["id"]), str(destination[name]["type"]))
            for name in keys
        ),
        tuple(restored_rows),
    )

"""Publish validated keyed records in the import or source owner's transaction."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .cell_writes import BaseCellWrite, replace_base_cells
from .changes import start_change
from .keyed_updates import (
    ValidatedKeyedInput,
    UpdatePolicy,
    observation,
    observe,
    reconcile_fields,
    require_relationship,
    observed_key_targets,
    typed_key,
)
from .structural_changes import note_row_membership_change


def index_keyed_targets(project, sheet_id, key_definition, staged):
    """Index visible current identities, never historical aliases."""
    cursor = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY id", (sheet_id,)
    )
    while batch := cursor.fetchmany(500):
        row_ids = [int(row[0]) for row in batch]
        values = [
            project.get_values(sheet_id, column, row_ids)
            for column, _ in key_definition
        ]
        staged.add_targets(
            (
                typed_key(
                    [
                        (kind, value.get(row))
                        for (_, kind), value in zip(key_definition, values, strict=True)
                    ]
                ),
                row,
            )
            for row in row_ids
        )


@dataclass
class KeyedPublication:
    matched: int = 0
    skipped: int = 0
    duplicates_collapsed: int = 0
    published: dict[int, int] = field(default_factory=dict)
    created: list[int] = field(default_factory=list)
    restored: list[int] = field(default_factory=list)


def publish_keyed_records(
    project: Any,
    *,
    sheet_id: int,
    relationship_id: str,
    key_definition: Sequence[tuple[int, str]],
    columns: Mapping[str, int],
    writable_columns: set[int],
    staged: ValidatedKeyedInput,
    records: Iterable[tuple[int, Mapping[str, Any]]],
    policy: UpdatePolicy,
    writer: Callable[[], tuple[int, int]],
    create_rows: Callable[[list[int], int], list[int]] | None = None,
    keep_existing_on_blank: bool = False,
    preserve_invalid: bool = False,
    import_writer_authority: str | None = None,
    prefer_observed_targets: bool = False,
) -> KeyedPublication:
    """Apply a page atomically with its observations; never infer absence.

    The caller owns the transaction, operation and Undo metadata. An import has
    no create_rows callback and can only update existing identities. Polling
    supplies batched creation and lazily acquires an operation only when needed.
    """
    db = project.db
    require_relationship(
        db,
        relationship_id=relationship_id,
        sheet_id=sheet_id,
        key_definition=key_definition,
    )
    result = KeyedPublication()
    resolved = []
    new_ordinals = []
    for ordinal, record in records:
        admitted = staged.first_record(ordinal)
        if admitted is None:
            continue
        result.duplicates_collapsed += max(0, admitted.copies - 1)
        if admitted.conflicting or admitted.key is None:
            result.skipped += admitted.copies
            continue
        targets = staged.targets(admitted.key)
        hidden = False
        # Older polls retained visible revisions with the same key. Their
        # tracked current-key identity can disambiguate those visible rows;
        # a deleted association cannot displace a visible current-key match.
        if len(targets) > 1 and prefer_observed_targets:
            preferred = observed_key_targets(
                db, relationship_id, admitted.key, hidden=False
            )
            if len(preferred) == 1:
                targets = preferred
        if not targets:
            targets = observed_key_targets(db, relationship_id, admitted.key)
            hidden = bool(targets)
        if len(targets) > 1 or not targets and create_rows is None:
            result.skipped += admitted.copies
            continue
        target = targets[0] if targets else None
        if target is not None:
            membership = db.execute(
                "SELECT hidden FROM rows WHERE id=? AND sheet_id=?", (target, sheet_id)
            ).fetchone()
            if membership is None or bool(membership[0]) != hidden:
                result.skipped += admitted.copies
                continue
            if (
                not hidden
                and typed_key(
                    [
                        (
                            kind,
                            project.get_values(sheet_id, column, [target]).get(target),
                        )
                        for column, kind in key_definition
                    ]
                )
                != admitted.key
            ):
                result.skipped += admitted.copies
                continue
        else:
            new_ordinals.append(ordinal)
        resolved.append((admitted, record, target, hidden))
    new_targets = {}
    if new_ordinals:
        _, producer = writer()
        for offset in range(0, len(new_ordinals), 500):
            batch = new_ordinals[offset : offset + 500]
            new_targets.update(zip(batch, create_rows(batch, producer), strict=True))
    writes = []
    for admitted, record, target, hidden in resolved:
        incoming = {
            str(columns[name]): value
            for name, value in record.items()
            if not (keep_existing_on_blank and value is None)
        }
        if target is None:
            target = new_targets[admitted.ordinal]
            result.created.append(target)
            result.published[admitted.ordinal] = target
        else:
            current = {}
            for column in writable_columns:
                values, refs = project.get_values_with_refs(
                    sheet_id, column, [target], preserve_invalid=preserve_invalid
                )
                if refs.get(target, {}).get("kind") != "missing" and target in values:
                    current[str(column)] = values[target]
            changes, restore = reconcile_fields(
                current,
                incoming,
                observation(db, relationship_id, target),
                policy,
                deleted=hidden,
            )
            changes = {
                column: value
                for column, value in changes.items()
                if int(column) in writable_columns
            }
            if restore:
                op_id, _ = writer()
                start_change(db, op_id, writing=True)
                db.execute("UPDATE rows SET hidden=0 WHERE id=?", (target,))
                result.restored.append(target)
            if changes or restore:
                result.published[admitted.ordinal] = target
                writes.extend(
                    BaseCellWrite(target, int(column), value)
                    for column, value in changes.items()
                )
            else:
                result.skipped += 1
        observe(
            db,
            relationship_id=relationship_id,
            row_id=target,
            key=admitted.key,
            incoming=incoming,
        )
        result.matched += 1
    refreshed_columns = {cell.column_id for cell in writes}
    if writes:
        _, producer = writer()
        replace_base_cells(
            db,
            producer_id=producer,
            column_ids=refreshed_columns,
            row_ids={cell.row_id for cell in writes} | set(result.restored),
            cells=writes,
            import_writer_authority=import_writer_authority,
            clear_omitted=False,
        )
    if result.restored:
        note_row_membership_change(
            db, [sheet_id], already_refreshed_columns=refreshed_columns
        )
    return result

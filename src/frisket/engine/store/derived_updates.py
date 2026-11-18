"""Declared derived identity and observation-based refresh publication.

Rows are retained. Incoming values and membership follow the same three-way
comparison as keyed imports; Undo changes working contributions, not observations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .keyed_updates import canonical, observe, reconcile_fields, require_relationship
from .transactions import inserted_rowid, require_transaction


@dataclass(frozen=True)
class MaterializedIdentity:
    kind: Literal["join", "list", "group"]
    # None explicitly means occurrences cannot be matched on a subsequent run.
    keys: Sequence[str] | None
    key_definition: Sequence[tuple[int, str]] = ()


def join_identity(sources: Sequence[tuple[int, str]]) -> str:
    """Exact ordered source pair, including an unmatched side; never values."""
    roles = dict((role, row) for row, role in sources)
    return canonical([roles.get("join_left"), roles.get("join_right")])


def establish_identity(db, *, sheet_id, op_id, column_ids, rows, row_ids, identity):
    require_transaction(db, "derived identity")
    if identity.keys is not None and len(identity.keys) != len(row_ids):
        raise ValueError("derived identities must match output rows")
    definition = list(identity.key_definition)
    scope = hashlib.sha256(canonical([identity.kind, definition]).encode()).hexdigest()[
        :24
    ]
    relationship_id = f"derived:{sheet_id}:{scope}"
    require_relationship(
        db,
        relationship_id=relationship_id,
        sheet_id=sheet_id,
        key_definition=definition,
    )
    config = {
        "version": 1,
        "kind": identity.kind,
        "relationship_id": relationship_id,
        "keyed": identity.keys is not None,
        "columns": dict(column_ids),
        "key_definition": definition,
    }
    for index, (row_id, values) in enumerate(zip(row_ids, rows, strict=True)):
        observe(
            db,
            relationship_id=relationship_id,
            row_id=row_id,
            key=identity.keys[index]
            if identity.keys is not None
            else f"occurrence:{row_id}",
            incoming={str(column_ids[name]): value for name, value in values.items()},
            observed_present=True,
        )
    spec = json.loads(
        db.execute("SELECT spec FROM ops WHERE id=?", (op_id,)).fetchone()[0] or "{}"
    )
    spec["derived_refresh"] = config
    db.execute("UPDATE ops SET spec=? WHERE id=?", (canonical(spec), op_id))
    return config


def refresh_config(parent_op):
    if parent_op is None:
        return None
    spec = json.loads(parent_op["spec"] or "{}")
    config = spec.get("derived_refresh")
    return config if isinstance(config, dict) and config.get("version") == 1 else None


@dataclass(frozen=True)
class DerivedRefreshRow:
    values: Mapping[str, Any]
    key: str | None = None
    parent_row_id: int | None = None
    sources: Sequence[tuple[int, str]] = ()


def reconcile_rows_into_open_change(
    db, *, op_id, sheet_id, config, columns, rows, policy="keep_changes"
):
    """Publish one bounded, identity-validated batch; never infer absence.

    The owner stages duplicate validation before the first call. Returned IDs
    align with input rows; a conflicting retained identity returns None.
    """
    from collections import defaultdict
    from types import SimpleNamespace

    from .cell_writes import (
        BaseCellWrite,
        create_base_cell_producer,
        initialize_base_cells,
        replace_base_cells,
    )
    from .changes import ChangeStore
    from .current_value_windows import read_current_column
    from .materialization import _insert_materialized_row_source
    from .structural_changes import record_structural_changes

    require_transaction(db, "derived refresh")
    if policy not in ("keep_changes", "incoming_wins"):
        raise ValueError("unknown keyed update policy")
    relationship_id = config["relationship_id"]
    if (
        db.execute(
            "SELECT 1 FROM keyed_relationships WHERE id=? AND sheet_id=?",
            (relationship_id, sheet_id),
        ).fetchone()
        is None
    ):
        raise ValueError(
            "Derived refresh identity is unavailable; create a new derived sheet"
        )
    owned_columns = {name: int(cid) for name, cid in config["columns"].items()}
    declared = {column.name: column for column in columns}
    if set(declared) != set(owned_columns):
        raise ValueError("Derived output fields changed; create a new derived sheet")
    for name, column_id in owned_columns.items():
        existing = db.execute(
            "SELECT active,sheet_id FROM columns WHERE id=?", (column_id,)
        ).fetchone()
        if (
            existing is None
            or not existing["active"]
            or existing["sheet_id"] != sheet_id
        ):
            raise ValueError(
                f"Derived output column {name!r} is unavailable; restore it before refresh"
            )

    # Duplicate key validation happens before the first mutation. Conflicting
    # duplicates stay seen for absence, but do not advance values or baseline.
    incoming = {}
    conflicts = set()
    for index, row in enumerate(rows):
        key = row.key if config["keyed"] else f"new:{index}"
        if key is None:
            raise ValueError("A keyed derived output omitted its declared identity")
        if key in incoming and canonical(incoming[key].values) != canonical(row.values):
            conflicts.add(key)
        incoming.setdefault(key, row)
    observed = {
        row["key"]: row
        for row in db.execute(
            "SELECT o.*,r.hidden FROM keyed_observations o JOIN rows r ON r.id=o.row_id "
            "WHERE o.relationship_id=? AND o.key IN (SELECT value FROM json_each(?))",
            (relationship_id, json.dumps(list(incoming)) if config["keyed"] else "[]"),
        )
    }
    # Derivation-owned identity is immutable, rather than inferred from editable
    # destination key cells. A duplicate retained identity cannot be guessed.
    duplicates = {
        row[0]
        for row in db.execute(
            "SELECT key FROM keyed_observations WHERE relationship_id=? "
            "AND key IN (SELECT value FROM json_each(?)) GROUP BY key HAVING COUNT(*)>1",
            (relationship_id, json.dumps(list(incoming)) if config["keyed"] else "[]"),
        )
    }
    conflicts.update(duplicates)
    # Reappearance may reverse this derivation's absence, but not a later
    # explicit deletion. Use the same ordered membership contributions as Undo.
    hidden_ids = [int(row["row_id"]) for row in observed.values() if row["hidden"]]
    refresh_hidden = {
        int(row["object_id"])
        for row in db.execute(
            "WITH ranked AS (SELECT effect.object_id,effect.after_visible,owner.kind,owner.spec,"
            "ROW_NUMBER() OVER (PARTITION BY effect.object_id ORDER BY owner.change_order DESC) AS rank "
            "FROM structural_changes effect JOIN ops owner ON owner.id=effect.op_id "
            "WHERE effect.kind='row' AND effect.object_id IN (SELECT value FROM json_each(?)) "
            "AND owner.status='applied' AND owner.change_order IS NOT NULL) "
            "SELECT object_id FROM ranked WHERE rank=1 AND after_visible=0 "
            "AND json_extract(spec,'$.derived_refresh.relationship_id')=?",
            (json.dumps(hidden_ids), relationship_id),
        )
    }
    target_ids = [
        int(observed[key]["row_id"])
        for key in incoming
        if key in observed and key not in conflicts
    ]
    current = {row_id: {} for row_id in target_ids}
    for column_id in owned_columns.values():
        for row_id, cell in read_current_column(
            db,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=target_ids,
            bounded=False,
            preserve_invalid=True,
            include_hidden=True,
        ).items():
            current[row_id][str(column_id)] = cell.value
    project = SimpleNamespace(db=db)
    ChangeStore(project).require_publication(op_id)
    from .column_descriptors import set_descriptor
    from frisket.contracts.action import canonical_column_type

    for name, column in declared.items():
        column_id = owned_columns[name]
        existing = db.execute(
            "SELECT type,format FROM columns WHERE id=?", (column_id,)
        ).fetchone()
        value = canonical_column_type(column.type)
        if existing["type"] != value:
            set_descriptor(project, column_id, "type", value, op_id=op_id, commit=False)
    producer_id = create_base_cell_producer(db, stage_id=f"op:{op_id}", op_id=op_id)
    info = {"created_rows": [], "deleted_rows": [], "restored_rows": []}
    max_position = db.execute(
        "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?", (sheet_id,)
    ).fetchone()[0]
    writes = defaultdict(list)
    output_ids = []
    created_cells = []
    for key, row in incoming.items():
        if key in conflicts:
            output_ids.append(None)
            continue
        incoming_values = {
            str(owned_columns[name]): value for name, value in row.values.items()
        }
        previous = observed.get(key) if config["keyed"] else None
        if previous is None:
            max_position += 1
            cursor = db.execute(
                "INSERT INTO rows(sheet_id,position,parent_row_id) VALUES(?,?,?)",
                (sheet_id, max_position, row.parent_row_id),
            )
            row_id = inserted_rowid(cursor)
            info["created_rows"].append(row_id)
            created_cells.extend(
                BaseCellWrite(row_id, int(column), value)
                for column, value in incoming_values.items()
            )
            baseline_key = key if config["keyed"] else f"occurrence:{row_id}"
        else:
            row_id = int(previous["row_id"])
            baseline_key = key
            baseline = json.loads(previous["record"])
            reappeared = not previous["observed_present"]
            restore_absence = reappeared and row_id in refresh_hidden
            changes, restore = reconcile_fields(
                current[row_id],
                incoming_values,
                baseline,
                policy,
                deleted=bool(previous["hidden"]) and not restore_absence,
            )
            if previous["hidden"] and (
                restore or restore_absence or (reappeared and policy == "incoming_wins")
            ):
                db.execute("UPDATE rows SET hidden=0 WHERE id=?", (row_id,))
                info["restored_rows"].append(row_id)
            if changes:
                writes[tuple(sorted(int(column) for column in changes))].append(
                    (row_id, changes)
                )
        for source_id, role in row.sources:
            _insert_materialized_row_source(
                db.cursor(),
                materialized_row_id=row_id,
                source_row_id=source_id,
                op_id=op_id,
                role=role,
            )
        observe(
            db,
            relationship_id=relationship_id,
            row_id=row_id,
            key=baseline_key,
            incoming=incoming_values,
            observed_present=True,
        )
        output_ids.append(row_id)
    initialize_base_cells(db, producer_id=producer_id, cells=created_cells)
    for column_ids, records in writes.items():
        replace_base_cells(
            db,
            producer_id=producer_id,
            column_ids=column_ids,
            row_ids=[row_id for row_id, _ in records],
            cells=[
                BaseCellWrite(row_id, int(column), value)
                for row_id, changes in records
                for column, value in changes.items()
            ],
        )
    record_structural_changes(db, op_id, info)
    _refresh_membership_batch(db, op_id, sheet_id, info, owned_columns.values())
    return output_ids


def _refresh_membership_batch(db, op_id, sheet_id, info, initialized_columns=()):
    from .structural_changes import note_row_additions, project_structural_changes
    from .current_cells import refresh_current_cells

    created = info.get("created_rows", [])
    changed = [*info.get("deleted_rows", []), *info.get("restored_rows", [])]
    if created or changed:
        project_structural_changes(db, op_id, object_ids=[*created, *changed])
    if created:
        note_row_additions(
            db, sheet_id, created, initialized_columns=initialized_columns
        )
    if changed:
        refresh_current_cells(
            db,
            column_ids=[
                row[0]
                for row in db.execute(
                    "SELECT id FROM columns WHERE sheet_id=?", (sheet_id,)
                )
            ],
            row_ids=changed,
        )


def restore_derived_rows(db, *, op_id, sheet_id, row_ids):
    """Publish row restoration chosen by the same keyed field reconciliation."""
    from types import SimpleNamespace
    from .changes import ChangeStore
    from .purge import require_publication_targets
    from .structural_changes import record_structural_changes

    ChangeStore(SimpleNamespace(db=db)).require_publication(op_id)
    require_publication_targets(db, sheet_id=sheet_id, row_ids=row_ids)
    info = {"restored_rows": list(row_ids)}
    record_structural_changes(db, op_id, info)
    _refresh_membership_batch(db, op_id, sheet_id, info)


def absent_derived_batches(db, *, config, seen, batch_size=256):
    """Keyset-scan retained observations; scratch membership is disk-backed."""
    after = 0
    while True:
        rows = db.execute(
            "SELECT o.*,r.hidden FROM keyed_observations o JOIN rows r ON r.id=o.row_id "
            "WHERE o.relationship_id=? AND o.observed_present=1 AND o.row_id>? "
            "ORDER BY o.row_id LIMIT ?",
            (config["relationship_id"], after, batch_size),
        ).fetchall()
        if not rows:
            return
        after = rows[-1]["row_id"]
        present = seen.contains(
            keys=[row["key"] for row in rows], row_ids=[row["row_id"] for row in rows]
        )
        absent = [
            row
            for row in rows
            if (row["key"] if config["keyed"] else row["row_id"]) not in present
        ]
        if absent:
            yield absent


def complete_derived_membership(db, *, op_id, sheet_id, config, observed_rows):
    """Retire a bounded absent-members batch, only after successful EOF."""
    from types import SimpleNamespace
    from .changes import ChangeStore
    from .structural_changes import record_structural_changes

    ChangeStore(SimpleNamespace(db=db)).require_publication(op_id)
    hidden = [int(row["row_id"]) for row in observed_rows if not row["hidden"]]
    db.executemany("UPDATE rows SET hidden=1 WHERE id=?", ((row,) for row in hidden))
    db.executemany(
        "UPDATE keyed_observations SET observed_present=0 WHERE relationship_id=? AND row_id=?",
        ((config["relationship_id"], row["row_id"]) for row in observed_rows),
    )
    info = {"deleted_rows": hidden}
    record_structural_changes(db, op_id, info)
    _refresh_membership_batch(db, op_id, sheet_id, info)

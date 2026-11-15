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
    if not db.in_transaction:
        raise RuntimeError("derived identity requires a caller transaction")
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


def publish_refresh(
    db, *, sheet_id, config, columns, rows, policy="keep_changes", spec=None
):
    """Publish an entirely enumerated deterministic refresh in the caller txn.

    Only declared output columns and observed generated members are owned here.
    Other columns and user-added rows remain independent working data.
    """
    from types import SimpleNamespace

    from .changes import ChangeStore
    from .op_log import append_op

    if not db.in_transaction:
        raise RuntimeError("derived refresh requires a caller transaction")
    project = SimpleNamespace(db=db)
    op_id = append_op(
        project,
        "sheet.refresh",
        label=f"Refresh sheet {sheet_id}",
        spec={"sheet_id": sheet_id, "policy": policy, **(spec or {})},
        commit=False,
    )
    ChangeStore(project).start(op_id)
    output_ids = reconcile_into_open_change(
        db,
        op_id=op_id,
        sheet_id=sheet_id,
        config=config,
        columns=columns,
        rows=rows,
        policy=policy,
    )
    ChangeStore(project).close(op_id)
    return op_id, output_ids


def reconcile_into_open_change(
    db, *, op_id, sheet_id, config, columns, rows, policy="keep_changes"
):
    """Reconcile a complete enumeration into the caller's open Change.

    The owning execution seals results and closes publication after this call.
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
    from .op_log import set_undo_info
    from .structural_changes import note_row_membership_change

    if not db.in_transaction:
        raise RuntimeError("derived refresh requires a caller transaction")
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
            "SELECT o.*,r.hidden FROM keyed_observations o JOIN rows r ON r.id=o.row_id WHERE o.relationship_id=? AND (o.observed_present=1 OR ?)",
            (relationship_id, int(bool(config["keyed"]))),
        )
    }
    # Derivation-owned identity is immutable, rather than inferred from editable
    # destination key cells. A duplicate retained identity cannot be guessed.
    duplicates = {
        row[0]
        for row in db.execute(
            "SELECT key FROM keyed_observations WHERE relationship_id=? GROUP BY key HAVING COUNT(*)>1",
            (relationship_id,),
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
            "AND kind='sheet.refresh' AND json_extract(spec,'$.sheet_id')=?",
            (json.dumps(hidden_ids), sheet_id),
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
            row_id = int(cursor.lastrowid)
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
    for key, previous in observed.items():
        if config["keyed"] and key in incoming or key in conflicts:
            continue
        # Observing absence twice must not reverse a later manual Restore/Undo.
        if previous["observed_present"] and not previous["hidden"]:
            row_id = int(previous["row_id"])
            db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
            info["deleted_rows"].append(row_id)
        db.execute(
            "UPDATE keyed_observations SET observed_present=0 WHERE relationship_id=? AND row_id=?",
            (relationship_id, previous["row_id"]),
        )
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
    descriptor_info = json.loads(
        db.execute("SELECT undo_info FROM ops WHERE id=?", (op_id,)).fetchone()[0]
        or "{}"
    )
    set_undo_info(project, op_id, {**descriptor_info, **info}, commit=False)
    if any(info.values()):
        note_row_membership_change(db, [sheet_id])
    return output_ids

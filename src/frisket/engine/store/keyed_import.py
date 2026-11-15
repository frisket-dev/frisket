"""Keyed table pages use the ordinary import session's transaction and cursor."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from frisket.contracts.action import canonical_column_type

from .cell_writes import BaseCellWrite, replace_base_cells
from .changes import ChangePublicationClosed
from .import_sessions import ImportSessionConflict, ImportSessionStore
from .keyed_updates import (
    ValidatedKeyedInput,
    changed_fields,
    observation,
    observe,
    require_relationship,
    retained_hidden_targets,
    typed_equal,
    typed_key,
)
from .table_import_intake import TableImportPlan


def index_targets(
    project: Any, plan: TableImportPlan, staged: ValidatedKeyedInput
) -> None:
    """Index only current visible identities; historical aliases do not win."""
    assert plan.update is not None and plan.destination_sheet_id is not None
    key_columns = [
        column
        for column in plan.columns
        if column.source_index in plan.update.key_source_indices
    ]
    columns = {
        str(row["name"]): int(row["id"])
        for row in project.db.execute(
            "SELECT id,name FROM columns WHERE sheet_id=? AND hidden=0",
            (plan.destination_sheet_id,),
        )
    }
    cursor = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY id",
        (plan.destination_sheet_id,),
    )
    while batch := cursor.fetchmany(500):
        row_ids = [int(row[0]) for row in batch]
        values = {
            column.target_name: project.get_values(
                plan.destination_sheet_id, columns[column.target_name], row_ids
            )
            for column in key_columns
        }
        staged.add_targets(
            (
                typed_key(
                    [
                        (
                            canonical_column_type(column.output_type),
                            values[column.target_name].get(row_id),
                        )
                        for column in key_columns
                    ]
                ),
                row_id,
            )
            for row_id in row_ids
        )


def record_key(plan: TableImportPlan, values: Mapping[str, Any]) -> str | None:
    assert plan.update is not None
    return typed_key(
        [
            (canonical_column_type(column.output_type), values.get(column.target_name))
            for column in plan.columns
            if column.source_index in plan.update.key_source_indices
        ]
    )


def publish_keyed_page(
    writer: Any,
    plan: TableImportPlan,
    staged: ValidatedKeyedInput,
    records: Iterable[tuple[int, Mapping[str, Any]]],
    *,
    expected_cursor: int,
    next_cursor: int,
    committed_bytes: int = 0,
) -> int:
    """Commit source observations, source versions and progress exactly once.

    The complete input has already been validated. No source absence is inferred
    from this page (or from EOF): external imports never delete missing members.
    """
    assert plan.update is not None
    if next_cursor <= expected_cursor or committed_bytes < 0:
        raise ValueError("keyed page must advance its source cursor")
    project, db = writer.project, writer.project.db
    relationship = plan.update.relationship_id or f"import:{writer._session_id}"
    columns = writer._column_ids
    keys = [
        column
        for column in plan.columns
        if column.source_index in plan.update.key_source_indices
    ]
    updates = [
        column
        for column in plan.columns
        if column.source_index not in plan.update.key_source_indices
    ]
    matched = 0
    restored = []
    try:
        db.execute("BEGIN IMMEDIATE")
        session = ImportSessionStore(project).require_writer(
            writer._session_id, writer._writer_authority, expected_cursor
        )
        owner = db.execute(
            "SELECT status,change_order,publication_closed FROM ops WHERE id=?",
            (writer._op_id,),
        ).fetchone()
        if (
            owner is None
            or owner[0] != "applied"
            or (owner[1] is not None and owner[2])
        ):
            raise ChangePublicationClosed("keyed import publication is closed")
        require_relationship(
            db,
            relationship_id=relationship,
            sheet_id=writer.sheet_id,
            key_definition=[
                (columns[column.target_name], canonical_column_type(column.output_type))
                for column in keys
            ],
        )
        for ordinal, record in records:
            admitted = staged.first_record(ordinal)
            if admitted is None or admitted.conflicting or admitted.key is None:
                continue
            candidates = staged.targets(admitted.key)
            hidden = False
            if not candidates:
                candidates = retained_hidden_targets(db, relationship, admitted.key)
                hidden = bool(candidates)
            if len(candidates) != 1:
                continue
            row_id = candidates[0]
            membership = db.execute(
                "SELECT hidden FROM rows WHERE id=? AND sheet_id=?",
                (row_id, writer.sheet_id),
            ).fetchone()
            if membership is None or bool(membership[0]) != hidden:
                continue
            # Recheck admitted current keys, avoiding stale index redirection.
            if not hidden:
                current_key = typed_key(
                    [
                        (
                            canonical_column_type(column.output_type),
                            project.get_values(
                                writer.sheet_id, columns[column.target_name], [row_id]
                            ).get(row_id),
                        )
                        for column in keys
                    ]
                )
                if current_key != admitted.key:
                    continue
            incoming = {str(columns[name]): value for name, value in record.items()}
            current = {}
            for column in updates:
                column_id = columns[column.target_name]
                values, refs = project.get_values_with_refs(
                    writer.sheet_id, column_id, [row_id]
                )
                if refs.get(row_id, {}).get("kind") != "missing" and row_id in values:
                    current[str(column_id)] = values[row_id]
            baseline = observation(db, relationship, row_id)
            changes = changed_fields(
                current,
                {
                    str(columns[column.target_name]): record[column.target_name]
                    for column in updates
                    if column.target_name in record
                },
                baseline,
                plan.update.policy,
            )
            if hidden:
                changed_source = baseline is None or any(
                    name not in baseline or not typed_equal(value, baseline[name])
                    for name, value in incoming.items()
                )
                if plan.update.policy == "keep_changes" or not changed_source:
                    changes = {}
                else:
                    db.execute("UPDATE rows SET hidden=0 WHERE id=?", (row_id,))
                    restored.append(row_id)
            if changes:
                replace_base_cells(
                    db,
                    producer_id=writer._producer_id,
                    column_ids=[int(column_id) for column_id in changes],
                    row_ids=[row_id],
                    cells=[
                        BaseCellWrite(row_id, int(column_id), value)
                        for column_id, value in changes.items()
                    ],
                    import_writer_authority=writer._writer_authority,
                )
            observe(
                db,
                relationship_id=relationship,
                row_id=row_id,
                key=admitted.key,
                incoming=incoming,
            )
            matched += 1
        if restored:
            info = json.loads(
                db.execute(
                    "SELECT undo_info FROM ops WHERE id=?", (writer._op_id,)
                ).fetchone()[0]
                or "{}"
            )
            info["restored_rows"] = sorted(
                set(info.get("restored_rows", [])) | set(restored)
            )
            project.set_undo_info(writer._op_id, info, commit=False)
        result = db.execute(
            "UPDATE import_sessions SET cursor=?,committed_rows=committed_rows+?,committed_bytes=committed_bytes+?,updated_at=datetime('now') WHERE id=? AND writer_authority=? AND cursor=? AND state='active'",
            (
                next_cursor,
                matched,
                committed_bytes,
                session.id,
                writer._writer_authority,
                expected_cursor,
            ),
        )
        if result.rowcount != 1:
            raise ImportSessionConflict(
                "import session changed during keyed publication"
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    writer._row_count += matched
    return matched

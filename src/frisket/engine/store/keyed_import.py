"""Keyed table pages use the ordinary import session's transaction and cursor."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from frisket.contracts.action import canonical_column_type

from .changes import ChangePublicationClosed
from .import_sessions import ImportSessionConflict, ImportSessionStore
from .keyed_updates import ValidatedKeyedInput, typed_key
from .keyed_publication import index_keyed_targets, publish_keyed_records
from .table_import_intake import TableImportPlan
from .transactions import write_transaction


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
    index_keyed_targets(
        project,
        plan.destination_sheet_id,
        [
            (columns[column.target_name], canonical_column_type(column.output_type))
            for column in key_columns
        ],
        staged,
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
    with write_transaction(db):
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
        publication = publish_keyed_records(
            project,
            sheet_id=writer.sheet_id,
            relationship_id=relationship,
            key_definition=[
                (columns[column.target_name], canonical_column_type(column.output_type))
                for column in keys
            ],
            columns=columns,
            writable_columns={columns[column.target_name] for column in updates},
            staged=staged,
            records=records,
            policy=plan.update.policy,
            writer=lambda: (writer._op_id, writer._producer_id),
            keep_existing_on_blank=plan.update.keep_existing_on_blank,
            import_writer_authority=writer._writer_authority,
        )
        matched = publication.matched
        if publication.restored:
            info = json.loads(
                db.execute(
                    "SELECT undo_info FROM ops WHERE id=?", (writer._op_id,)
                ).fetchone()[0]
                or "{}"
            )
            info["restored_rows"] = sorted(
                set(info.get("restored_rows", [])) | set(publication.restored)
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
    writer._row_count += matched
    return matched

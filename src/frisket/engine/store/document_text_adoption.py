"""One-time adoption of fresh legacy prepared results as document Text."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)
from .document_text import (
    DocumentTextRole,
    _prepared_result_matches_source_sql,
    document_text_role,
    ensure_document_text_role,
    is_generation_free_document_text_target,
)
from .value_codec import PreparedContentRef


@dataclass(frozen=True, slots=True)
class DocumentTextAdoption:
    role: DocumentTextRole
    created: bool
    seeded_count: int


def adopt_document_text_role(
    project: Any, *, source_column_id: int, text_column_id: int
) -> DocumentTextAdoption:
    """Bind a role and seed only its unambiguous current prepared results.

    The caller owns the transaction. Candidates are current visible generated
    outputs with exact source provenance. Their compact prepared references
    become ordinary base cells under the immutable content set's existing
    producing operation; prepared page bodies are never read or copied.
    """

    db = project.db
    if not db.in_transaction:
        raise RuntimeError("document text adoption requires a caller transaction")
    existing = document_text_role(project, source_column_id=source_column_id)
    role = ensure_document_text_role(
        project,
        source_column_id=source_column_id,
        text_column_id=text_column_id,
        commit=False,
    )
    if existing is not None and (
        not is_generation_free_document_text_target(
            project,
            sheet_id=role.sheet_id,
            source_column_id=role.source_column_id,
            text_column_id=role.text_column_id,
        )
        or db.execute(
            "SELECT 1 FROM cells WHERE column_id=? LIMIT 1",
            (role.text_column_id,),
        ).fetchone()
        is not None
    ):
        return DocumentTextAdoption(role=role, created=False, seeded_count=0)

    matched = _prepared_result_matches_source_sql("candidate")
    candidates = db.execute(
        f"""WITH candidate AS MATERIALIZED (
          SELECT head.row_id,? AS column_id,legacy_result.value AS value,
                 content.producing_op_id
          FROM current_cells head
          JOIN results legacy_result
            ON legacy_result.run_id=head.origin_run_id
           AND legacy_result.row_id=head.row_id
           AND legacy_result.column_id=head.column_id
          JOIN columns legacy ON legacy.id=head.column_id
          JOIN rows source_row ON source_row.id=head.row_id
          JOIN prepared_content_refs ref ON ref.id=legacy_result.value
          JOIN prepared_content_sets content ON content.id=ref.set_id
          JOIN ops producer_op ON producer_op.id=content.producing_op_id
          WHERE legacy.sheet_id=? AND legacy.id<>?
            AND legacy.type='text' AND legacy.ai_generated=1
            AND legacy.active=1 AND legacy.hidden=0
            AND source_row.sheet_id=? AND source_row.hidden=0
            AND head.origin_kind='run_result' AND head.validity='valid'
            AND legacy_result.publication_effect='publish_value'
            AND legacy_result.value_kind='prepared_content_ref'
            AND producer_op.status='applied'
            AND NOT EXISTS (
              SELECT 1 FROM document_text_column_roles claimed
              WHERE claimed.text_column_id=legacy.id
            )
        ), eligible AS MATERIALIZED (
          SELECT row_id,CAST(value AS INTEGER) AS prepared_ref_id,
                 producing_op_id
          FROM candidate
          WHERE {matched}
        )
        SELECT row_id,MIN(prepared_ref_id),MIN(producing_op_id)
        FROM eligible
        GROUP BY row_id
        HAVING COUNT(DISTINCT prepared_ref_id)=1""",
        (
            role.text_column_id,
            role.sheet_id,
            role.text_column_id,
            role.sheet_id,
        ),
    ).fetchall()

    by_operation: dict[int, list[BaseCellWrite]] = defaultdict(list)
    for row_id, prepared_ref_id, producing_op_id in candidates:
        by_operation[int(producing_op_id)].append(
            BaseCellWrite(
                int(row_id),
                role.text_column_id,
                PreparedContentRef(int(prepared_ref_id)),
            )
        )
    seeded_count = 0
    for producing_op_id, writes in by_operation.items():
        producer_id = create_base_cell_producer(
            db,
            stage_id=f"op:{producing_op_id}",
            op_id=producing_op_id,
        )
        seeded_count += initialize_base_cells(
            db, producer_id=producer_id, cells=writes
        )
    return DocumentTextAdoption(
        role=role,
        created=existing is None,
        seeded_count=seeded_count,
    )


__all__ = ["DocumentTextAdoption", "adopt_document_text_role"]

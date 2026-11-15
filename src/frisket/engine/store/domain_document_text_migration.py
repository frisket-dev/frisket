"""Freeze pre-cutover implicit document text into explicit column roles once."""

from __future__ import annotations

import sqlite3

from .cell_writes import (
    BaseCellWrite,
    EditCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
    insert_edits,
)
from .document_text import ensure_document_text_role
from .value_codec import PreparedContentRef

# Pinned pre-cutover selection relation. Do not import the new runtime resolver:
# migration preserves the old effective choice before removing that fallback.
_LEGACY_READINESS = r"""current_prepared_outputs AS MATERIALIZED (
      SELECT DISTINCT output_column.sheet_id,live.row_id,
             live.column_id AS output_column_id,
             ref.id AS prepared_ref_id,ref.page_number,
             artifact.id AS artifact_id,artifact.blob_hash,
             artifact.page_count,
             (live.prepared_ref_id IS NULL) AS is_manual,
             length(trim(live.value,char(9,10,11,12,13,32)))>0 AS has_text,
             artifact.source_sheet_id,
             artifact.source_row_id,artifact.source_column_id
      FROM domain_document_source_scope AS readiness_scope
      JOIN columns AS output_column
        ON output_column.sheet_id=readiness_scope.sheet_id
       AND output_column.hidden=0
      JOIN current_cell_values AS live
        ON live.column_id=output_column.id
       AND live.row_id=readiness_scope.row_id
      LEFT JOIN cell_result_heads AS generated_head
        ON generated_head.column_id=live.column_id
       AND generated_head.row_id=live.row_id
       AND live.origin_kind='manual_edit'
      LEFT JOIN results AS generated
        ON generated.run_id=generated_head.run_id
       AND generated.row_id=generated_head.row_id
       AND generated.column_id=generated_head.column_id
       AND generated.publication_effect='publish_value'
       AND generated.value_kind='prepared_content_ref'
      JOIN prepared_content_refs AS ref
        ON ref.id=COALESCE(
          live.prepared_ref_id,
          CASE WHEN live.origin_kind='manual_edit'
               THEN CAST(generated.value AS INTEGER) END
        )
      JOIN prepared_content_sets AS content
        ON content.id=ref.set_id
      JOIN source_artifacts AS artifact
        ON artifact.id=content.source_artifact_id
      WHERE NOT EXISTS (
        SELECT 1 FROM document_text_column_roles role
        WHERE role.text_column_id=output_column.id
           OR role.source_column_id=artifact.source_column_id
      )
        AND live.value_kind='text'
        AND (live.prepared_ref_id IS NOT NULL
             OR length(trim(live.value,char(9,10,11,12,13,32)))>0)
    ), legacy_prepared_sources AS MATERIALIZED (
      SELECT prepared_ref_id,page_number,artifact_id,blob_hash,page_count,
             is_manual,has_text,output_column_id,source_sheet_id,source_row_id,source_column_id
      FROM current_prepared_outputs
      WHERE source_sheet_id IS NOT NULL
        AND source_row_id IS NOT NULL
        AND source_column_id IS NOT NULL
        AND sheet_id=source_sheet_id
        AND row_id=source_row_id
      UNION
      SELECT prepared.prepared_ref_id,prepared.page_number,
             prepared.artifact_id,prepared.blob_hash,prepared.page_count,
             prepared.is_manual,prepared.has_text,prepared.output_column_id,source_link.sheet_id,
             source_link.row_id,source_link.column_id
      FROM current_prepared_outputs AS prepared
      JOIN source_spans AS source_span
        ON source_span.artifact_id=prepared.artifact_id
       AND source_span.span_kind='page'
       AND source_span.page_start IS prepared.page_number
       AND source_span.page_end IS prepared.page_number
      JOIN evidence_link_spans AS source_linked
        ON source_linked.span_id=source_span.id
      JOIN evidence_links AS source_link
        ON source_link.id=source_linked.link_id
       AND source_link.status='active'
       AND source_link.link_role='primary_support'
       AND source_link.subject_kind='cell_value'
       AND source_link.sheet_id=prepared.sheet_id
       AND source_link.row_id=prepared.row_id
       AND source_link.sheet_id IS NOT NULL
       AND source_link.row_id IS NOT NULL
       AND source_link.column_id IS NOT NULL
    ), prepared_candidates AS MATERIALIZED (
      SELECT legacy.* FROM legacy_prepared_sources legacy
      WHERE NOT EXISTS (
        SELECT 1 FROM document_text_column_roles role
        WHERE role.source_column_id=legacy.source_column_id
      )
      UNION
      SELECT selected.prepared_ref_id,selected.source_page_number,
             selected.source_artifact_id,selected.source_blob_hash,
             selected.page_count,selected.prepared_ref_id IS NULL AS is_manual,
             EXISTS (SELECT 1 FROM current_cell_values text
                     WHERE text.row_id=selected.row_id
                       AND text.column_id=selected.text_column_id
                       AND length(trim(text.value,char(9,10,11,12,13,32)))>0) AS has_text,
             selected.text_column_id,
             selected.source_sheet_id,selected.row_id,selected.source_column_id
      FROM domain_document_source_scope readiness_scope
      JOIN current_document_text_selections selected
        ON selected.source_sheet_id=readiness_scope.sheet_id
       AND selected.row_id=readiness_scope.row_id
      WHERE selected.text_hidden=0
        AND (selected.prepared_ref_id IS NOT NULL OR EXISTS (
          SELECT 1 FROM current_cell_values text
          WHERE text.row_id=selected.row_id
            AND text.column_id=selected.text_column_id
            AND length(trim(text.value,char(9,10,11,12,13,32)))>0
        ))
    ), current_prepared_sources AS MATERIALIZED (
      SELECT candidate.*,
             CASE WHEN candidate.is_manual THEN 1
                  WHEN candidate.page_number IS NOT NULL THEN EXISTS (
                    SELECT 1 FROM prepared_content_set_pages member
                    WHERE member.set_id=ref.set_id
                      AND member.page_number=candidate.page_number
                  )
                  ELSE candidate.page_count IS NOT NULL AND (
                    SELECT count(*) FROM prepared_content_set_pages member
                    WHERE member.set_id=ref.set_id
                  )=candidate.page_count END AS coverage_complete
      FROM prepared_candidates candidate
      LEFT JOIN prepared_content_refs ref ON ref.id=candidate.prepared_ref_id
    )"""


def adopt_legacy_document_text(db: sqlite3.Connection) -> dict[int, int]:
    """Preserve selected current values; caller owns the cutover transaction.

    Existing explicit roles are untouched. A shared existing column is reused;
    mixed choices get a new ordinary Text column of compact prepared references.
    Only actual flat manual corrections copy text, with their genuine source pin.
    """
    if not db.in_transaction:
        raise RuntimeError("document text adoption requires a caller transaction")
    db.execute(
        "CREATE TEMP TABLE domain_document_choices("
        "source_column_id INTEGER,row_id INTEGER,sheet_id INTEGER,"
        "output_column_id INTEGER,prepared_ref_id INTEGER,is_manual INTEGER,"
        "PRIMARY KEY(source_column_id,row_id)) WITHOUT ROWID"
    )
    try:
        db.execute(
            "INSERT INTO domain_document_choices "
            "WITH domain_document_source_scope AS ("
            "SELECT r.sheet_id,r.id AS row_id FROM rows r JOIN sheets s ON s.id=r.sheet_id "
            "WHERE r.hidden=0 AND s.hidden=0), " + _LEGACY_READINESS + ", "
            "ranked AS (SELECT candidate.*,ROW_NUMBER() OVER ("
            "PARTITION BY candidate.source_column_id,candidate.source_row_id "
            "ORDER BY (candidate.has_text AND candidate.coverage_complete) DESC, "
            "candidate.prepared_ref_id DESC,candidate.output_column_id) AS preference "
            "FROM current_prepared_sources candidate "
            "JOIN columns source ON source.id=candidate.source_column_id "
            "JOIN current_cell_values live ON live.column_id=source.id "
            "AND live.row_id=candidate.source_row_id "
            "WHERE source.active=1 AND source.hidden=0 AND source.type IN ('file','image') "
            "AND live.validity='valid' AND live.value_kind='json' "
            "AND json_extract(live.value,'$.blob')=candidate.blob_hash "
            "AND candidate.page_number IS CASE WHEN json_type(live.value,'$.page')='integer' "
            "THEN json_extract(live.value,'$.page') ELSE NULL END "
            "AND NOT EXISTS(SELECT 1 FROM document_text_column_roles role "
            "WHERE role.source_column_id=source.id)) "
            "SELECT source_column_id,source_row_id,source_sheet_id,output_column_id,"
            "prepared_ref_id,is_manual FROM ranked WHERE preference=1"
        )
        adopted = {}
        sources = db.execute(
            "SELECT source_column_id,sheet_id,MIN(output_column_id),COUNT(DISTINCT output_column_id) "
            "FROM domain_document_choices GROUP BY source_column_id,sheet_id"
        ).fetchall()
        migration_op = None
        producer = None
        for source, sheet, common, count in sources:
            claimed = db.execute(
                "SELECT 1 FROM document_text_column_roles WHERE text_column_id=?",
                (common,),
            ).fetchone()
            if count == 1 and claimed is None:
                text_column = int(common)
                ensure_document_text_role(
                    db, source_column_id=source, text_column_id=text_column
                )
                # The existing column already owns its values and manual provenance.
                # Legacy manual corrections without a role need their explicit pin.
                db.execute(
                    "INSERT OR IGNORE INTO document_text_manual_pins "
                    "SELECT live.origin_op_id,choice.row_id,choice.output_column_id,choice.prepared_ref_id "
                    "FROM domain_document_choices choice JOIN current_cell_values live "
                    "ON live.row_id=choice.row_id AND live.column_id=choice.output_column_id "
                    "WHERE choice.source_column_id=? AND choice.is_manual=1 "
                    "AND live.origin_kind='manual_edit'",
                    (source,),
                )
            else:
                source_name = str(
                    db.execute(
                        "SELECT name FROM columns WHERE id=?", (source,)
                    ).fetchone()[0]
                )
                base_name = f"{source_name} text"
                name, suffix = base_name, 2
                while db.execute(
                    "SELECT 1 FROM columns WHERE sheet_id=? AND name=? AND active=1",
                    (sheet, name),
                ).fetchone():
                    name = f"{base_name}_{suffix}"
                    suffix += 1
                position = db.execute(
                    "SELECT COALESCE(MAX(position),0)+1 FROM columns WHERE sheet_id=?",
                    (sheet,),
                ).fetchone()[0]
                text_column = int(
                    db.execute(
                        "INSERT INTO columns(sheet_id,name,type,position) VALUES(?,?,'text',?)",
                        (sheet, name, position),
                    ).lastrowid
                )
                ensure_document_text_role(
                    db, source_column_id=source, text_column_id=text_column
                )
                if migration_op is None:
                    migration_op = int(
                        db.execute(
                            "INSERT INTO ops(kind,label,spec,undo_info,barrier) "
                            "VALUES('migration.document_text','Preserve selected document text','{}',NULL,1)"
                        ).lastrowid
                    )
                    producer = create_base_cell_producer(
                        db, stage_id=f"op:{migration_op}", op_id=migration_op
                    )
                cursor = db.execute(
                    "SELECT row_id,output_column_id,prepared_ref_id,is_manual "
                    "FROM domain_document_choices WHERE source_column_id=? ORDER BY row_id",
                    (source,),
                )
                while batch := cursor.fetchmany(256):
                    initialize_base_cells(
                        db,
                        producer_id=producer,
                        cells=[
                            BaseCellWrite(row, text_column, PreparedContentRef(ref))
                            for row, _, ref, _ in batch
                        ],
                    )
                    edits = []
                    for row, old_column, _, manual in batch:
                        if manual:
                            value = db.execute(
                                "SELECT value FROM current_cell_values WHERE row_id=? AND column_id=?",
                                (row, old_column),
                            ).fetchone()[0]
                            edits.append(EditCellWrite(row, text_column, value))
                    if edits:
                        insert_edits(db, op_id=migration_op, edits=edits)
            adopted[int(source)] = text_column
        return adopted
    finally:
        db.execute("DROP TABLE domain_document_choices")

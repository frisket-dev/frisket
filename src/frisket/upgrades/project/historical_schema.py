"""Pinned historical DDL used only while converting old projects."""

SCALAR_CURRENT_CELL_VALUES_SQL = """
CREATE VIEW IF NOT EXISTS current_cell_values AS
SELECT head.column_id,head.row_id,
       CASE WHEN head.inline_value_kind IS NOT NULL
       THEN head.inline_value_kind ELSE CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT source.value_kind FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE result.publication_effect
             WHEN 'publish_value' THEN result.value_kind
             WHEN 'publish_null' THEN 'null'
           END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT edit.value_kind FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS value_kind,
       CASE WHEN head.inline_value_kind IS NOT NULL
       THEN head.inline_value ELSE CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT source.value FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value'
             THEN result.value END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT edit.value FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS value,
       head.origin_kind,head.origin_op_id,head.origin_run_id,
       head.base_producer_id,head.validity
FROM current_cells AS head INDEXED BY idx_current_cells_column_row
"""

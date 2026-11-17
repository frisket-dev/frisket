"""Shared scalar/prepared decoder over an explicit current-head relation."""


def current_value_select_sql(heads: str = "effective_current_cells") -> str:
    return f"""
SELECT head.column_id,head.row_id,
       CASE WHEN head.inline_value_kind IS NOT NULL
       THEN head.inline_value_kind ELSE CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT CASE source.value_kind
             WHEN 'prepared_content_ref' THEN 'text' ELSE source.value_kind END
           FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE result.publication_effect
             WHEN 'publish_value' THEN CASE result.value_kind
               WHEN 'prepared_content_ref' THEN 'text' ELSE result.value_kind END
             WHEN 'publish_null' THEN 'null'
           END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id AND result.published=1
         )
         WHEN 'copied_result' THEN (
           SELECT CASE result.publication_effect WHEN 'publish_value' THEN CASE result.value_kind WHEN 'prepared_content_ref' THEN 'text' ELSE result.value_kind END WHEN 'publish_null' THEN 'null' END
           FROM copied_result_values result WHERE result.op_id=head.origin_op_id
             AND result.row_id=head.row_id AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT CASE edit.value_kind
             WHEN 'prepared_content_ref' THEN 'text' ELSE edit.value_kind END
           FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS value_kind,
       CASE WHEN head.inline_value_kind IS NOT NULL
       THEN head.inline_value ELSE CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT CASE source.value_kind WHEN 'prepared_content_ref' THEN (
             SELECT prepared.value FROM prepared_content_ref_values AS prepared
             WHERE prepared.ref_id=source.value
           ) ELSE source.value END FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value'
             THEN CASE result.value_kind WHEN 'prepared_content_ref' THEN (
               SELECT prepared.value FROM prepared_content_ref_values AS prepared
               WHERE prepared.ref_id=result.value
             ) ELSE result.value END END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id AND result.published=1
         )
         WHEN 'copied_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value' THEN CASE result.value_kind WHEN 'prepared_content_ref' THEN (SELECT prepared.value FROM prepared_content_ref_values prepared WHERE prepared.ref_id=result.value) ELSE result.value END END
           FROM copied_result_values result WHERE result.op_id=head.origin_op_id
             AND result.row_id=head.row_id AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT CASE edit.value_kind WHEN 'prepared_content_ref' THEN (
             SELECT prepared.value FROM prepared_content_ref_values AS prepared
             WHERE prepared.ref_id=edit.value
           ) ELSE edit.value END FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS value,
       head.origin_kind,head.origin_op_id,head.origin_run_id,
       head.base_producer_id,head.validity,head.outcome,
       CASE WHEN head.inline_value_kind IS NULL THEN CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT CASE source.value_kind WHEN 'prepared_content_ref'
             THEN source.value END FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value'
             AND result.value_kind='prepared_content_ref' THEN result.value END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id AND result.published=1
         )
         WHEN 'copied_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value' AND result.value_kind='prepared_content_ref' THEN result.value END
           FROM copied_result_values result WHERE result.op_id=head.origin_op_id
             AND result.row_id=head.row_id AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT CASE edit.value_kind WHEN 'prepared_content_ref'
             THEN edit.value END FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS prepared_ref_id
FROM {heads} AS head
"""


CURRENT_CELL_VALUES_SQL = (
    "CREATE VIEW IF NOT EXISTS current_cell_values AS\n"
    + current_value_select_sql()
    + ";"
)

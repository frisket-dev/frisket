"""Shared current-source readiness relation for prepared text outputs."""

from __future__ import annotations


def prepared_source_readiness_ctes(*, scope_relation: str, schema: str = "") -> str:
    """Return CTEs mapping visible prepared outputs to their exact source cells.

    A nonblank manual edit remains ready when the generation immediately beneath
    it published prepared content.  ``cell_result_heads`` is the authoritative
    current generated candidate, while the final source identity is still fenced
    by the prepared artifact's blob, page selector, and source-cell locator.
    """

    prefix = f"{schema}." if schema else ""
    return f"""current_prepared_outputs AS MATERIALIZED (
      SELECT DISTINCT output_column.sheet_id,live.row_id,
             live.column_id AS output_column_id,
             ref.id AS prepared_ref_id,ref.page_number,
             artifact.id AS artifact_id,artifact.blob_hash,
             artifact.page_count,artifact.source_sheet_id,
             artifact.source_row_id,artifact.source_column_id
      FROM {scope_relation} AS readiness_scope
      JOIN {prefix}columns AS output_column
        ON output_column.sheet_id=readiness_scope.sheet_id
       AND output_column.hidden=0
      JOIN {prefix}current_cell_values AS live
        ON live.column_id=output_column.id
       AND live.row_id=readiness_scope.row_id
      LEFT JOIN {prefix}cell_result_heads AS generated_head
        ON generated_head.column_id=live.column_id
       AND generated_head.row_id=live.row_id
       AND live.origin_kind='manual_edit'
      LEFT JOIN {prefix}results AS generated
        ON generated.run_id=generated_head.run_id
       AND generated.row_id=generated_head.row_id
       AND generated.column_id=generated_head.column_id
       AND generated.publication_effect='publish_value'
       AND generated.value_kind='prepared_content_ref'
      JOIN {prefix}prepared_content_refs AS ref
        ON ref.id=COALESCE(
          live.prepared_ref_id,
          CASE WHEN live.origin_kind='manual_edit'
               THEN CAST(generated.value AS INTEGER) END
        )
      JOIN {prefix}prepared_content_sets AS content
        ON content.id=ref.set_id
      JOIN {prefix}source_artifacts AS artifact
        ON artifact.id=content.source_artifact_id
      WHERE live.value_kind='text'
        AND length(trim(live.value))>0
    ), current_prepared_sources AS MATERIALIZED (
      SELECT prepared_ref_id,page_number,artifact_id,blob_hash,page_count,
             output_column_id,source_sheet_id,source_row_id,source_column_id
      FROM current_prepared_outputs
      WHERE source_sheet_id IS NOT NULL
        AND source_row_id IS NOT NULL
        AND source_column_id IS NOT NULL
        AND sheet_id=source_sheet_id
        AND row_id=source_row_id
      UNION
      SELECT prepared.prepared_ref_id,prepared.page_number,
             prepared.artifact_id,prepared.blob_hash,prepared.page_count,
             prepared.output_column_id,source_link.sheet_id,
             source_link.row_id,source_link.column_id
      FROM current_prepared_outputs AS prepared
      JOIN {prefix}source_spans AS source_span
        ON source_span.artifact_id=prepared.artifact_id
       AND source_span.span_kind='page'
       AND source_span.page_start IS prepared.page_number
       AND source_span.page_end IS prepared.page_number
      JOIN {prefix}evidence_link_spans AS source_linked
        ON source_linked.span_id=source_span.id
      JOIN {prefix}evidence_links AS source_link
        ON source_link.id=source_linked.link_id
       AND source_link.status='active'
       AND source_link.link_role='primary_support'
       AND source_link.subject_kind='cell_value'
       AND source_link.sheet_id=prepared.sheet_id
       AND source_link.row_id=prepared.row_id
       AND source_link.sheet_id IS NOT NULL
       AND source_link.row_id IS NOT NULL
       AND source_link.column_id IS NOT NULL
    )"""


__all__ = ["prepared_source_readiness_ctes"]

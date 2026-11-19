"""Ordered contributions and the shared current-head SQL construction."""

# A reject-clear edit yields to any later replacement of its cell; every other
# edit yields only to a later replacement that changed the column descriptor.
_REJECT_CLEAR_EDIT_SQL = (
    "{op}.kind='review.decision' "
    "AND json_extract({op}.spec,'$.params.decision')='reject_clear'"
)
_DESCRIPTOR_CHANGE_SQL = " OR ".join(
    f"json_type(op.undo_info,'$.{key}.\"'||generation.column_id||'\"') IS NOT NULL"
    for key in (
        "column_types_after",
        "column_formats_after",
        "column_semantic_types_after",
    )
)

# edit_supersession: on a run result, which earlier edits it supersedes (2 every
# edit, 1 reject-clear edits, 0 none); on an edit, the strength that supersedes
# it. Other contributions carry 0.
CELL_CONTRIBUTION_ARMS = (
    """SELECT cell.row_id,cell.column_id,'source_cell' AS origin_kind,
       NULL AS origin_op_id,NULL AS origin_run_id,cell.producer_id AS base_producer_id,
       op.change_order,1 AS publication_closed,0 AS edit_supersession
FROM cells cell JOIN base_cell_producers producer ON producer.id=cell.producer_id
JOIN ops op ON op.id=producer.op_id
WHERE op.status='applied' AND op.change_order IS NOT NULL""",
    """SELECT scope.row_id,generation.column_id,'run_result',run.op_id,run.id,NULL,
       op.change_order,op.publication_closed,
       CASE WHEN generation.write_mode!='replace_scope' THEN 0
            WHEN """
    + _DESCRIPTOR_CHANGE_SQL
    + """ THEN 2 ELSE 1 END
FROM run_output_generations generation JOIN runs run ON run.id=generation.run_id
JOIN ops op ON op.id=run.op_id JOIN run_rows scope ON scope.run_id=run.id
WHERE op.status='applied' AND op.change_order IS NOT NULL
AND generation.state!='staged'
AND NOT EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract(
 op.undo_info,'$.unpublished_generation_columns'),'[]')) unpublished
 WHERE CAST(unpublished.value AS INTEGER)=generation.column_id)
AND NOT EXISTS (SELECT 1 FROM results retained WHERE retained.run_id=run.id
 AND retained.row_id=scope.row_id AND retained.column_id=generation.column_id
 AND retained.publication_effect='retain')""",
    """SELECT edit.row_id,edit.column_id,'manual_edit',edit.op_id,NULL,NULL,
       op.change_order,1,CASE WHEN """
    + _REJECT_CLEAR_EDIT_SQL.format(op="op")
    + """ THEN 1 ELSE 2 END
FROM edits edit JOIN ops op ON op.id=edit.op_id
WHERE op.status='applied' AND op.change_order IS NOT NULL""",
    """SELECT copy.row_id,copy.column_id,'copied_result',copy.op_id,copy.source_run_id,NULL,
       op.change_order,1,0
FROM copied_result_outcomes copy JOIN ops op ON op.id=copy.op_id
WHERE op.status='applied' AND op.change_order IS NOT NULL""",
    """SELECT baseline.row_id,baseline.column_id,baseline.origin_kind,baseline.origin_op_id,
       baseline.origin_run_id,baseline.base_producer_id,0,1,
       CASE WHEN baseline.origin_kind!='manual_edit' THEN 0
            WHEN EXISTS (SELECT 1 FROM ops op WHERE op.id=baseline.origin_op_id AND """
    + _REJECT_CLEAR_EDIT_SQL.format(op="op")
    + """) THEN 1 ELSE 2 END
FROM cell_baseline baseline""",
)


def canonical_heads_sql(
    contributions_from: str = "cell_contributions contribution",
) -> str:
    """Resolve ordered winners from an internally scoped contribution relation.

    Later changes win, except that a human edit outranks every later run
    result on its cell: the run's value is offered for replay review instead.
    A later replacement that changes the column descriptor supersedes the
    edit, and any later replacement supersedes a reject-clear edit. A later
    source write or copied result supersedes the edit as well, so a run after
    it is again ordered by change alone.
    """
    return f"""
SELECT payload.column_id,payload.row_id,
       COALESCE(revision.revision,1) AS retrieval_revision,
       payload.origin_kind,payload.origin_op_id,payload.origin_run_id,payload.base_producer_id,
       CASE
         WHEN payload.value_kind IS NULL OR payload.value_kind='null' THEN 'missing'
         WHEN descriptor.type IN ('text','category','timestamped_transcript')
           AND payload.value_kind='text' THEN 'valid'
         WHEN descriptor.type='json' AND payload.value_kind='json' THEN 'valid'
         WHEN descriptor.type IN ('number','integer','boolean') AND payload.value_kind IN ('text','json')
           THEN 'invalid'
         ELSE frisket_cell_validity(descriptor.type,payload.value_kind,payload.value)
       END AS validity,
       CASE WHEN payload.value_kind IN ('null','integer','real','boolean')
         OR (payload.value_kind IN ('text','bigint') AND length(CAST(payload.value AS BLOB))<=512)
         THEN payload.value_kind END AS inline_value_kind,
       CASE WHEN payload.value_kind IN ('null','integer','real','boolean')
         OR (payload.value_kind IN ('text','bigint') AND length(CAST(payload.value AS BLOB))<=512)
         THEN payload.value END AS inline_value,
       payload.outcome
FROM (
  SELECT ranked.*,
       CASE ranked.origin_kind
         WHEN 'source_cell' THEN cell.value_kind
         WHEN 'copied_result' THEN CASE WHEN copy.outcome='completed' THEN copy.value_kind END
         WHEN 'manual_edit' THEN edit.value_kind
         WHEN 'run_result' THEN CASE result.publication_effect
           WHEN 'publish_value' THEN result.value_kind WHEN 'publish_null' THEN 'null' END END AS value_kind,
       CASE ranked.origin_kind
         WHEN 'source_cell' THEN cell.value WHEN 'manual_edit' THEN edit.value
         WHEN 'copied_result' THEN CASE WHEN copy.outcome='completed' THEN copy.value END
         WHEN 'run_result' THEN CASE WHEN result.publication_effect='publish_value' THEN result.value END END AS value,
       CASE WHEN ranked.origin_kind='copied_result' THEN copy.outcome
            WHEN ranked.origin_kind!='run_result' THEN 'completed'
            WHEN result.publication_effect IN ('publish_value','publish_null') THEN 'completed'
            WHEN result.outcome='not_completed' THEN 'not_completed'
            WHEN result.row_id IS NOT NULL THEN 'failed'
            WHEN ranked.publication_closed=0 THEN 'pending' ELSE 'not_completed' END AS outcome
  FROM (
    SELECT ordered.*,ROW_NUMBER() OVER (
      PARTITION BY ordered.column_id,ordered.row_id
      ORDER BY CASE WHEN ordered.origin_kind='manual_edit'
            AND COALESCE(ordered.last_overwrite,-1)<ordered.change_order
            AND COALESCE(ordered.last_descriptor_replacement,-1)<ordered.change_order
            AND (ordered.edit_supersession=2
              OR COALESCE(ordered.last_replacement,-1)<ordered.change_order)
          THEN MAX(ordered.change_order,COALESCE(ordered.last_run,-1))
          ELSE ordered.change_order END DESC,
        CASE ordered.origin_kind WHEN 'manual_edit' THEN 2 WHEN 'run_result' THEN 1 ELSE 0 END DESC,
        ordered.change_order DESC,
        ordered.base_producer_id DESC
    ) AS winner FROM (
      SELECT contribution.*,
        MAX(CASE WHEN contribution.origin_kind='run_result'
          THEN contribution.change_order END) OVER cell AS last_run,
        MAX(CASE WHEN contribution.origin_kind IN ('source_cell','copied_result')
          THEN contribution.change_order END) OVER cell AS last_overwrite,
        MAX(CASE WHEN contribution.origin_kind='run_result' AND contribution.edit_supersession>=1
          THEN contribution.change_order END) OVER cell AS last_replacement,
        MAX(CASE WHEN contribution.origin_kind='run_result' AND contribution.edit_supersession=2
          THEN contribution.change_order END) OVER cell AS last_descriptor_replacement
      FROM {contributions_from}
      WINDOW cell AS (PARTITION BY contribution.column_id,contribution.row_id)
    ) ordered
  ) ranked
  LEFT JOIN cells cell ON ranked.origin_kind='source_cell' AND cell.row_id=ranked.row_id
    AND cell.column_id=ranked.column_id AND cell.producer_id IS ranked.base_producer_id
  LEFT JOIN edits edit ON ranked.origin_kind='manual_edit' AND edit.row_id=ranked.row_id
    AND edit.column_id=ranked.column_id AND edit.op_id=ranked.origin_op_id
  LEFT JOIN results result ON ranked.origin_kind='run_result' AND result.row_id=ranked.row_id
    AND result.column_id=ranked.column_id AND result.run_id=ranked.origin_run_id AND result.published=1
  LEFT JOIN copied_result_values copy ON ranked.origin_kind='copied_result' AND copy.op_id=ranked.origin_op_id
    AND copy.row_id=ranked.row_id AND copy.column_id=ranked.column_id
  WHERE ranked.winner=1
) payload
JOIN columns descriptor ON descriptor.id=payload.column_id
JOIN rows row ON row.id=payload.row_id AND row.sheet_id=descriptor.sheet_id
LEFT JOIN cell_authority_revisions revision ON revision.column_id=payload.column_id
"""


def generated_candidates_sql(
    contributions_from: str = "cell_contributions contribution",
) -> str:
    """Top non-edit identity for replay, review, provenance and backfill."""
    heads = canonical_heads_sql(
        contributions_from + " WHERE contribution.origin_kind!='manual_edit'"
    )
    return (
        "SELECT column_id,row_id,origin_run_id AS run_id,origin_op_id AS op_id,"
        "outcome FROM (" + heads + ") WHERE origin_kind='run_result'"
    )


CHANGE_SCHEMA_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_change_order ON ops(change_order);
CREATE INDEX IF NOT EXISTS idx_run_rows_row ON run_rows(row_id,run_id);

-- One immutable reference per effective cell at the history cutover. Payloads
-- remain in their original version tables; old operation state is irrelevant.
CREATE TABLE IF NOT EXISTS cell_baseline (
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  origin_kind TEXT NOT NULL,
  origin_op_id INTEGER REFERENCES ops(id),
  origin_run_id INTEGER REFERENCES runs(id),
  base_producer_id INTEGER REFERENCES base_cell_producers(id),
  PRIMARY KEY(column_id,row_id,origin_kind)
);
CREATE TABLE IF NOT EXISTS cell_authority_revisions (
  column_id INTEGER PRIMARY KEY REFERENCES columns(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS current_cell_repair (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id_start INTEGER NOT NULL,
  row_id_end INTEGER NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0,
  CHECK(row_id_start<=row_id_end)
);
CREATE INDEX IF NOT EXISTS idx_cell_repair_column ON current_cell_repair(column_id,row_id_start,row_id_end);

"""
CHANGE_SCHEMA_SQL += (
    "CREATE VIEW IF NOT EXISTS cell_contributions AS\n"
    + "\nUNION ALL\n".join(CELL_CONTRIBUTION_ARMS)
    + ";\n"
)
CHANGE_SCHEMA_SQL += (
    "CREATE VIEW IF NOT EXISTS canonical_cell_heads AS\n"
    + canonical_heads_sql()
    + ";\n"
)
CHANGE_SCHEMA_SQL += (
    "CREATE VIEW IF NOT EXISTS dirty_cell_heads AS\n"
    + canonical_heads_sql(
        "current_cell_repair dirty CROSS JOIN cell_contributions contribution "
        "ON contribution.column_id=dirty.column_id "
        "AND contribution.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end"
    )
    + ";\n"
)
CHANGE_SCHEMA_SQL += (
    "CREATE VIEW IF NOT EXISTS generated_result_candidates AS\n"
    + "SELECT * FROM ("
    + generated_candidates_sql()
    + ") WHERE outcome IN ('completed','failed');\n"
)
CHANGE_SCHEMA_SQL += """
CREATE VIEW IF NOT EXISTS effective_current_cells AS
SELECT head.* FROM current_cells head WHERE NOT EXISTS (
  SELECT 1 FROM current_cell_repair dirty WHERE dirty.column_id=head.column_id
  AND head.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end
)
UNION ALL
SELECT head.* FROM dirty_cell_heads head WHERE EXISTS (
  SELECT 1 FROM current_cell_repair dirty WHERE dirty.column_id=head.column_id
  AND head.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end
);

-- Publication fences are separate from current values and candidate discovery.
CREATE VIEW IF NOT EXISTS published_result_bases AS
SELECT column_id,row_id,run_id FROM (
  SELECT result.column_id,result.row_id,result.run_id,
         ROW_NUMBER() OVER (
           PARTITION BY result.column_id,result.row_id
           ORDER BY COALESCE(op.change_order,0) DESC,run.id DESC
         ) AS winner
  FROM results result JOIN runs run ON run.id=result.run_id
  JOIN ops op ON op.id=run.op_id
  WHERE result.published=1 AND result.publication_effect IN ('publish_value','publish_null','publish_error')
    AND op.status='applied'
) WHERE winner=1;
"""

"""DDL for ordered contributions and their disposable current-cell projection."""

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

CREATE VIEW IF NOT EXISTS cell_contributions AS
SELECT cell.row_id,cell.column_id,'source_cell' AS origin_kind,
       NULL AS origin_op_id,NULL AS origin_run_id,cell.producer_id AS base_producer_id,
       op.change_order,1 AS publication_closed
FROM cells cell JOIN base_cell_producers producer ON producer.id=cell.producer_id
JOIN ops op ON op.id=producer.op_id
WHERE op.status='applied' AND op.change_order IS NOT NULL
UNION ALL
SELECT scope.row_id,generation.column_id,'run_result',run.op_id,run.id,NULL,
       op.change_order,op.publication_closed
FROM run_output_generations generation JOIN runs run ON run.id=generation.run_id
JOIN ops op ON op.id=run.op_id JOIN run_rows scope ON scope.run_id=run.id
WHERE op.status='applied' AND op.change_order IS NOT NULL
AND NOT EXISTS (SELECT 1 FROM results retained WHERE retained.run_id=run.id
 AND retained.row_id=scope.row_id AND retained.column_id=generation.column_id
 AND retained.publication_effect='retain')
UNION ALL
SELECT edit.row_id,edit.column_id,'manual_edit',edit.op_id,NULL,NULL,
       op.change_order,1
FROM edits edit JOIN ops op ON op.id=edit.op_id
WHERE op.status='applied' AND op.change_order IS NOT NULL
UNION ALL
SELECT copy.row_id,copy.column_id,'copied_result',copy.op_id,copy.source_run_id,NULL,
       op.change_order,1
FROM copied_result_outcomes copy JOIN ops op ON op.id=copy.op_id
WHERE op.status='applied' AND op.change_order IS NOT NULL
UNION ALL
SELECT row_id,column_id,origin_kind,origin_op_id,origin_run_id,base_producer_id,0,1
FROM cell_baseline;

CREATE VIEW IF NOT EXISTS canonical_cell_heads AS
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
    SELECT contribution.*,ROW_NUMBER() OVER (
      PARTITION BY contribution.column_id,contribution.row_id
      ORDER BY contribution.change_order DESC,
        CASE contribution.origin_kind WHEN 'manual_edit' THEN 2 WHEN 'run_result' THEN 1 ELSE 0 END DESC,
        contribution.base_producer_id DESC
    ) AS winner FROM cell_contributions contribution
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
LEFT JOIN cell_authority_revisions revision ON revision.column_id=payload.column_id;

-- The cache is never the authority while an intersecting scope is dirty. Both
-- arms are evaluated in the caller's one SQLite statement/read snapshot.
CREATE VIEW IF NOT EXISTS effective_current_cells AS
SELECT head.* FROM current_cells head WHERE NOT EXISTS (
  SELECT 1 FROM current_cell_repair dirty WHERE dirty.column_id=head.column_id
  AND head.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end
)
UNION ALL
SELECT head.* FROM canonical_cell_heads head WHERE EXISTS (
  SELECT 1 FROM current_cell_repair dirty WHERE dirty.column_id=head.column_id
  AND head.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end
);
"""

# Share the winner definition while driving dirty resolution from the compact
# worklist. With no dirty scopes this arm reads no contribution history.
_head_definition = CHANGE_SCHEMA_SQL.split(
    "CREATE VIEW IF NOT EXISTS canonical_cell_heads AS", 1
)[1].split(";", 1)[0]
_dirty_definition = _head_definition.replace(
    "FROM cell_contributions contribution",
    "FROM current_cell_repair dirty CROSS JOIN cell_contributions contribution "
    "ON contribution.column_id=dirty.column_id "
    "AND contribution.row_id BETWEEN dirty.row_id_start AND dirty.row_id_end",
)
CHANGE_SCHEMA_SQL = CHANGE_SCHEMA_SQL.replace(
    "CREATE VIEW IF NOT EXISTS effective_current_cells AS",
    "CREATE VIEW IF NOT EXISTS dirty_cell_heads AS" + _dirty_definition + ";\n"
    "CREATE VIEW IF NOT EXISTS effective_current_cells AS",
).replace(
    "SELECT head.* FROM canonical_cell_heads head WHERE EXISTS",
    "SELECT head.* FROM dirty_cell_heads head WHERE EXISTS",
)

# Saved-answer comparison excludes only ordinary edits; it uses the same
# ordered winner and pending-mask rules, never a column-wide run pointer.
CHANGE_SCHEMA_SQL += (
    "\nCREATE VIEW IF NOT EXISTS unmodified_cell_heads AS"
    + _head_definition.replace(
        "FROM cell_contributions contribution",
        "FROM cell_contributions contribution WHERE contribution.origin_kind!='manual_edit'",
    )
    + ";\n"
)

CHANGE_SCHEMA_SQL += """
CREATE VIEW IF NOT EXISTS current_result_heads AS
SELECT column_id,row_id,origin_run_id AS run_id FROM unmodified_cell_heads
WHERE origin_kind='run_result' AND outcome IN ('completed','failed');

-- Claim fences need the newest actually-published result even when a later
-- logical Change currently masks it with pending coverage. This is not a
-- canonical read view: readers continue to use current_result_heads.
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

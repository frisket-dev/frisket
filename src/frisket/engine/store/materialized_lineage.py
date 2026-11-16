"""Immutable contributor sets, projected from the latest applied owner per row."""

MATERIALIZED_LINEAGE_SCHEMA_SQL = """
-- MATERIALIZED_ROW_SOURCES_BEGIN
CREATE TABLE IF NOT EXISTS materialized_row_sources (
  materialized_row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  source_row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  source_sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (
    role IN (
      'edge_source', 'edge_target', 'aggregate_source', 'join_left', 'join_right'
    )
  ),
  PRIMARY KEY (materialized_row_id, source_row_id, role, op_id)
);
CREATE INDEX IF NOT EXISTS idx_materialized_row_sources_source
  ON materialized_row_sources(source_sheet_id, source_row_id, role);
CREATE INDEX IF NOT EXISTS idx_materialized_row_sources_materialized
  ON materialized_row_sources(materialized_row_id, role);
CREATE INDEX IF NOT EXISTS idx_materialized_row_sources_op
  ON materialized_row_sources(op_id);
-- MATERIALIZED_ROW_SOURCES_END
CREATE VIEW IF NOT EXISTS current_materialized_row_sources AS
SELECT m.materialized_row_id,m.source_row_id,m.source_sheet_id,m.op_id,m.role
FROM materialized_row_sources m
WHERE m.op_id = (
  SELECT candidate.op_id FROM materialized_row_sources candidate
  JOIN ops owner ON owner.id=candidate.op_id
  WHERE candidate.materialized_row_id=m.materialized_row_id AND owner.status='applied'
  ORDER BY COALESCE(owner.change_order,0) DESC,owner.id DESC LIMIT 1
);
"""

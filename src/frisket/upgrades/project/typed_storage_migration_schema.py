"""Frozen typed-storage endpoint DDL (c0894e62, schema 529f5f4e).

Later migrations depend on exactly these columns and triggers. Never derive
this historical destination from the evolving fresh-bundle schema.
"""

TYPED_STORAGE_STATEMENTS = {
    "CREATE TABLE IF NOT EXISTS cells": """
CREATE TABLE IF NOT EXISTS cells (
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  value_kind TEXT NOT NULL CHECK (value_kind IN (
    'null','text','integer','real','boolean','json','bigint','legacy_invalid'
  )),
  value,
  -- NULL is reserved for migrated history whose producer was never recorded.
  -- CELL_PRODUCER_ID_BEGIN
  producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  -- CELL_PRODUCER_ID_END
  UNIQUE (row_id, column_id),
  CHECK (
    (value_kind='null' AND value IS NULL)
    OR (value_kind='text' AND typeof(value)='text')
    OR (value_kind='integer' AND typeof(value)='integer')
    OR (value_kind='real' AND typeof(value)='real'
        AND value=value AND abs(value)<=1.7976931348623157e308)
    OR (value_kind='boolean' AND typeof(value)='integer' AND value IN (0,1))
    OR (value_kind='json' AND typeof(value)='text' AND json_valid(value))
    OR (value_kind='bigint' AND typeof(value)='text')
    OR (value_kind='legacy_invalid' AND typeof(value)='text')
  )
);
""",
    "CREATE TABLE IF NOT EXISTS results": """
CREATE TABLE IF NOT EXISTS results (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_kind TEXT CHECK (value_kind IN (
    'null','text','integer','real','boolean','json','bigint','legacy_invalid'
  )),
  value,
  tokens_in INTEGER,
  tokens_out INTEGER,
  confidence REAL,
  justification TEXT,
  error TEXT,
  -- run-row-error-observability-v1: the classifier code behind `error`
  -- (frisket.llm.remediation's RemediatedError.code, e.g. model_error,
  -- missing_provider_key, invalid_output — or a batch-recipe error code),
  -- kept separate from `outcome`'s coarser ok/empty/model_error/... bucket so
  -- the run-detail error summary can group rows by their EXACT message while
  -- still carrying a stable code alongside it. Null when `error` is null.
  error_code TEXT,
  review_state TEXT NOT NULL DEFAULT 'unreviewed',
  -- The human's exact decision about the ORIGINAL generated value. Kept
  -- separate from review_state: a corrected edit is usable/verified data but
  -- is still a failed grade for the original output. NULL is honest for
  -- pre-migration reviewed rows whose historical decision is unknown.
  review_decision TEXT CHECK (
    review_decision IN ('accept', 'reject', 'reject_clear', 'edit')
  ),
  review_note TEXT,
  -- typed classification of the cell's state (the single source of truth for
  -- failed/done/pending; `error` is a display message only). See
  -- the public schema boundary.
  -- ok | empty | model_error | invalid_output | withheld_unverified | unverified_memory
  outcome TEXT NOT NULL DEFAULT 'ok',
  -- Explicit publication semantics for generation-managed results. NULL is
  -- the legacy/unmanaged contract; managed writers must choose one terminal
  -- effect after every payload transform has completed.
  publication_effect TEXT,
  PRIMARY KEY (run_id, row_id, column_id),
  CHECK (
    (value_kind IS NULL AND value IS NULL)
    OR (value_kind='null' AND value IS NULL)
    OR (value_kind='text' AND typeof(value)='text')
    OR (value_kind='integer' AND typeof(value)='integer')
    OR (value_kind='real' AND typeof(value)='real'
        AND value=value AND abs(value)<=1.7976931348623157e308)
    OR (value_kind='boolean' AND typeof(value)='integer' AND value IN (0,1))
    OR (value_kind='json' AND typeof(value)='text' AND json_valid(value))
    OR (value_kind='bigint' AND typeof(value)='text')
    OR (value_kind='legacy_invalid' AND typeof(value)='text')
  ),
  CHECK (
    publication_effect IS NULL
    OR (
      publication_effect = 'publish_value'
      AND value_kind IS NOT NULL
      AND value_kind <> 'null'
      AND value IS NOT NULL
      AND error IS NULL
    )
    OR (
      publication_effect = 'publish_null'
      AND value_kind = 'null'
      AND value IS NULL
      AND error IS NULL
    )
    OR (
      publication_effect = 'publish_error'
      AND value_kind IS NULL
      AND value IS NULL
      AND error IS NOT NULL
    )
  )
) WITHOUT ROWID;
""",
    "CREATE TABLE IF NOT EXISTS edits": """
CREATE TABLE IF NOT EXISTS edits (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_kind TEXT NOT NULL CHECK (value_kind IN (
    'null','text','integer','real','boolean','json','bigint','legacy_invalid'
  )),
  value,
  PRIMARY KEY (op_id, row_id, column_id),
  CHECK (
    (value_kind='null' AND value IS NULL)
    OR (value_kind='text' AND typeof(value)='text')
    OR (value_kind='integer' AND typeof(value)='integer')
    OR (value_kind='real' AND typeof(value)='real'
        AND value=value AND abs(value)<=1.7976931348623157e308)
    OR (value_kind='boolean' AND typeof(value)='integer' AND value IN (0,1))
    OR (value_kind='json' AND typeof(value)='text' AND json_valid(value))
    OR (value_kind='bigint' AND typeof(value)='text')
    OR (value_kind='legacy_invalid' AND typeof(value)='text')
  )
) WITHOUT ROWID;
""",
    "CREATE TABLE IF NOT EXISTS current_cells": """
CREATE TABLE IF NOT EXISTS current_cells (
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('source_cell', 'run_result', 'manual_edit')
  ),
  origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,
  origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  base_producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),
  CHECK (
    (
      origin_kind='source_cell'
      AND origin_op_id IS NULL
      AND origin_run_id IS NULL
    )
    OR (
      origin_kind='run_result'
      AND origin_op_id IS NOT NULL
      AND origin_run_id IS NOT NULL
      AND base_producer_id IS NULL
    )
    OR (
      origin_kind='manual_edit'
      AND origin_op_id IS NOT NULL
      AND origin_run_id IS NULL
      AND base_producer_id IS NULL
    )
  )
);
""",
    "CREATE TRIGGER IF NOT EXISTS trg_results_semantic_update_open_generation": """
CREATE TRIGGER IF NOT EXISTS trg_results_semantic_update_open_generation
BEFORE UPDATE OF value_kind, value, tokens_in, tokens_out, confidence, justification,
  error, error_code, outcome, publication_effect ON results
WHEN NEW.publication_effect IS NOT NULL
  AND (
    NEW.run_id IS NOT OLD.run_id
    OR NEW.row_id IS NOT OLD.row_id
    OR NEW.column_id IS NOT OLD.column_id
    OR NEW.value_kind IS NOT OLD.value_kind
    OR NEW.value IS NOT OLD.value
    OR NEW.tokens_in IS NOT OLD.tokens_in
    OR NEW.tokens_out IS NOT OLD.tokens_out
    OR NEW.confidence IS NOT OLD.confidence
    OR NEW.justification IS NOT OLD.justification
    OR NEW.error IS NOT OLD.error
    OR NEW.error_code IS NOT OLD.error_code
    OR NEW.outcome IS NOT OLD.outcome
    OR NEW.publication_effect IS NOT OLD.publication_effect
  )
  AND NOT EXISTS (
    SELECT 1
    FROM run_output_generations generation
    JOIN runs run ON run.id = generation.run_id
    JOIN ops op ON op.id = run.op_id
    WHERE generation.run_id = NEW.run_id
      AND generation.column_id = NEW.column_id
      AND generation.state IN ('active', 'staged')
      AND op.status = 'applied'
  )
BEGIN
  SELECT RAISE(ABORT, 'publication effect requires an open applied generation');
END;
""",
    "CREATE TRIGGER IF NOT EXISTS trg_results_published_semantics_immutable": """
CREATE TRIGGER IF NOT EXISTS trg_results_published_semantics_immutable
BEFORE UPDATE ON results
WHEN OLD.publication_effect IS NOT NULL
  AND (
    NEW.run_id IS NOT OLD.run_id
    OR NEW.row_id IS NOT OLD.row_id
    OR NEW.column_id IS NOT OLD.column_id
    OR (
      EXISTS (
        SELECT 1 FROM run_output_generations generation
        WHERE generation.run_id = OLD.run_id
          AND generation.column_id = OLD.column_id
          AND (
            generation.state = 'sealed'
            OR EXISTS (
              SELECT 1 FROM cell_result_heads head
              WHERE head.run_id = OLD.run_id
                AND head.row_id = OLD.row_id
                AND head.column_id = OLD.column_id
            )
          )
      )
      AND (
        NEW.value_kind IS NOT OLD.value_kind
        OR NEW.value IS NOT OLD.value
        OR NEW.tokens_in IS NOT OLD.tokens_in
        OR NEW.tokens_out IS NOT OLD.tokens_out
        OR NEW.confidence IS NOT OLD.confidence
        OR NEW.justification IS NOT OLD.justification
        OR NEW.error IS NOT OLD.error
        OR NEW.error_code IS NOT OLD.error_code
        OR NEW.outcome IS NOT OLD.outcome
        OR NEW.publication_effect IS NOT OLD.publication_effect
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'published result semantics are immutable');
END;
""",
    "CREATE VIEW IF NOT EXISTS current_cell_values": """
CREATE VIEW IF NOT EXISTS current_cell_values AS
SELECT head.column_id,head.row_id,
       CASE head.origin_kind
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
       END AS value_kind,
       CASE head.origin_kind
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
       END AS value,
       head.origin_kind,head.origin_op_id,head.origin_run_id,
       head.base_producer_id,head.validity
FROM current_cells AS head INDEXED BY idx_current_cells_column_row;
""",
}

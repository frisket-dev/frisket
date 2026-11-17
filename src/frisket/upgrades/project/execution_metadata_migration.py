"""Add execution telemetry and successful-result warnings without rewriting history."""

from __future__ import annotations
import sqlite3
from frisket.upgrades.project.retrieval_lifecycle_migration import (
    RETRIEVAL_LIFECYCLE_TO_DIGEST,
)
from frisket.upgrades.project._step import SchemaStep


EXECUTION_METADATA_FROM_DIGEST = RETRIEVAL_LIFECYCLE_TO_DIGEST
EXECUTION_METADATA_TO_DIGEST = "frisket.schema.v1:f6a34262a8b43d56cd6e19afab76f67f"


_SEMANTIC_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER trg_results_semantic_update_open_generation
BEFORE UPDATE OF value_kind, value, tokens_in, tokens_out, confidence, justification,
  warnings, error, error_code, outcome, publication_effect ON results
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
    OR NEW.warnings IS NOT OLD.warnings
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
"""

_PUBLISHED_IMMUTABILITY_TRIGGER_SQL = """
CREATE TRIGGER trg_results_published_semantics_immutable
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
        OR NEW.warnings IS NOT OLD.warnings
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
"""


def _apply_execution_metadata(db: sqlite3.Connection) -> None:
    """Add nullable progress and empty warnings, preserving inputs and write fences."""
    run_columns = {str(info[1]) for info in db.execute("PRAGMA table_info(runs)")}
    if "work_progress" not in run_columns:
        db.execute(
            "ALTER TABLE runs ADD COLUMN work_progress TEXT CHECK ("
            "work_progress IS NULL OR (json_valid(work_progress) "
            "AND json_type(work_progress)='object'))"
        )
    columns = {str(info[1]) for info in db.execute("PRAGMA table_info(results)")}
    if "warnings" not in columns:
        db.execute(
            "ALTER TABLE results ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]' "
            "CHECK (json_valid(warnings) AND json_type(warnings)='array')"
        )
    db.execute("DROP TRIGGER IF EXISTS trg_results_semantic_update_open_generation")
    db.execute(_SEMANTIC_UPDATE_TRIGGER_SQL)
    db.execute("DROP TRIGGER IF EXISTS trg_results_published_semantics_immutable")
    db.execute(_PUBLISHED_IMMUTABILITY_TRIGGER_SQL)


EXECUTION_METADATA_STEP: SchemaStep = (
    EXECUTION_METADATA_FROM_DIGEST,
    EXECUTION_METADATA_TO_DIGEST,
    _apply_execution_metadata,
)


__all__ = [
    "EXECUTION_METADATA_FROM_DIGEST",
    "EXECUTION_METADATA_TO_DIGEST",
    "EXECUTION_METADATA_STEP",
]

"""Frozen DDL for the prepared-content migration endpoint.

These statements belong to the digest stamped by ``prepared_content_migration``.
They must not follow later additions to the fresh bundle schema.
"""

from __future__ import annotations


PREPARED_AUTHORITY_TABLE_SQL = {
    "cells": """
CREATE TABLE cells_prepared_new (
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  value_kind TEXT NOT NULL CHECK (value_kind IN (
    'null','text','integer','real','boolean','json','bigint','legacy_invalid',
    'prepared_content_ref'
  )),
  value,
  producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
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
    OR (value_kind='prepared_content_ref' AND typeof(value)='integer' AND value>0)
  )
);
""",
    "results": """
CREATE TABLE results_prepared_new (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_kind TEXT CHECK (value_kind IN (
    'null','text','integer','real','boolean','json','bigint','legacy_invalid',
    'prepared_content_ref'
  )),
  value,
  tokens_in INTEGER,
  tokens_out INTEGER,
  confidence REAL,
  justification TEXT,
  error TEXT,
  error_code TEXT,
  review_state TEXT NOT NULL DEFAULT 'unreviewed',
  review_decision TEXT CHECK (
    review_decision IN ('accept', 'reject', 'reject_clear', 'edit')
  ),
  review_note TEXT,
  outcome TEXT NOT NULL DEFAULT 'ok',
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
    OR (value_kind='prepared_content_ref' AND typeof(value)='integer' AND value>0)
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
    "edits": """
CREATE TABLE edits_prepared_new (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_kind TEXT NOT NULL CHECK (value_kind IN (
    'null','text','integer','real','boolean','json','bigint','legacy_invalid',
    'prepared_content_ref'
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
    OR (value_kind='prepared_content_ref' AND typeof(value)='integer' AND value>0)
  )
) WITHOUT ROWID;
""",
}


PREPARED_CONTENT_SCHEMA_SQL = (
    """
CREATE TABLE IF NOT EXISTS prepared_page_versions (
  id INTEGER PRIMARY KEY,
  source_artifact_id INTEGER REFERENCES source_artifacts(id) ON DELETE SET NULL,
  page_number INTEGER NOT NULL CHECK (page_number>0),
  prepared_text TEXT NOT NULL,
  content_hash TEXT NOT NULL CHECK (
    length(content_hash)=71 AND substr(content_hash,1,7)='sha256:'
  ),
  positions_json TEXT CHECK (positions_json IS NULL OR json_valid(positions_json)),
  producing_op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE RESTRICT
);
""",
    """
CREATE INDEX IF NOT EXISTS idx_prepared_page_versions_artifact_page
  ON prepared_page_versions(source_artifact_id,page_number,id);
""",
    """
CREATE TABLE IF NOT EXISTS prepared_content_sets (
  id INTEGER PRIMARY KEY,
  source_artifact_id INTEGER REFERENCES source_artifacts(id) ON DELETE SET NULL,
  producing_op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE RESTRICT
);
""",
    """
CREATE TABLE IF NOT EXISTS prepared_content_set_pages (
  set_id INTEGER NOT NULL REFERENCES prepared_content_sets(id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL CHECK (page_number>0),
  version_id INTEGER NOT NULL REFERENCES prepared_page_versions(id) ON DELETE RESTRICT,
  PRIMARY KEY (set_id,page_number)
) WITHOUT ROWID;
""",
    """
CREATE INDEX IF NOT EXISTS idx_prepared_content_set_pages_version
  ON prepared_content_set_pages(version_id,set_id,page_number);
""",
    """
CREATE TABLE IF NOT EXISTS prepared_content_refs (
  id INTEGER PRIMARY KEY,
  set_id INTEGER NOT NULL REFERENCES prepared_content_sets(id) ON DELETE CASCADE,
  page_number INTEGER CHECK (page_number IS NULL OR page_number>0)
);
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_prepared_content_refs_document
  ON prepared_content_refs(set_id) WHERE page_number IS NULL;
""",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_prepared_content_refs_page
  ON prepared_content_refs(set_id,page_number) WHERE page_number IS NOT NULL;
""",
    """
CREATE VIEW IF NOT EXISTS prepared_content_ref_values AS
SELECT ref.id AS ref_id,ref.set_id,set_record.source_artifact_id,
       ref.page_number,'text' AS value_kind,
       CASE WHEN ref.page_number IS NULL THEN (
         SELECT group_concat(ordered.prepared_text, char(10)||char(10))
         FROM (
           SELECT version.prepared_text
           FROM prepared_content_set_pages AS member
           JOIN prepared_page_versions AS version ON version.id=member.version_id
           WHERE member.set_id=ref.set_id
           ORDER BY member.page_number
         ) AS ordered
       ) ELSE (
         SELECT version.prepared_text
         FROM prepared_content_set_pages AS member
         JOIN prepared_page_versions AS version ON version.id=member.version_id
         WHERE member.set_id=ref.set_id AND member.page_number=ref.page_number
       ) END AS value
FROM prepared_content_refs AS ref
JOIN prepared_content_sets AS set_record ON set_record.id=ref.set_id;
""",
)


PREPARED_CURRENT_CELL_VALUES_SQL = """
CREATE VIEW IF NOT EXISTS current_cell_values AS
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
             AND result.column_id=head.column_id
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
             AND result.column_id=head.column_id
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
       head.base_producer_id,head.validity,
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
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT CASE edit.value_kind WHEN 'prepared_content_ref'
             THEN edit.value END FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS prepared_ref_id
FROM current_cells AS head INDEXED BY idx_current_cells_column_row;
"""


__all__ = [
    "PREPARED_AUTHORITY_TABLE_SQL",
    "PREPARED_CONTENT_SCHEMA_SQL",
    "PREPARED_CURRENT_CELL_VALUES_SQL",
]

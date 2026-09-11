"""SQLite schema for a frisket project.

Run-based versioning: the provenance envelope lives once per run; results
hold per-cell deltas; a column's live state is a pointer to its latest run
plus a sparse manual-edit overlay. Undo/redo moves pointers, copies nothing.

This module is AUTHORITATIVE. Known prior schemas upgrade atomically in
``bundle_open.py``; unrecognized bundles are refused by
:func:`require_current_schema` without replacing their data.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

FORMAT_VERSION = 1

#: ``meta`` key carrying the digest of the DDL a bundle was created with.
SCHEMA_DIGEST_META_KEY = "schema_digest"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheets (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  position INTEGER NOT NULL DEFAULT 0,
  parent_sheet_id INTEGER REFERENCES sheets(id),
  parent_op_id INTEGER,
  hidden INTEGER NOT NULL DEFAULT 0,
  -- Derived-sheet staleness watermark (lazy pull idiom, cf. fts_state.indexed_at_op).
  -- last_verified_op_cursor is the op_cursor a derived sheet was last
  -- materialized/refreshed against; NULL means "never refreshed since creation"
  -- and the resolver falls back to parent_op_id. stale_reason/stale_at are an
  -- observability cache computed lazily at read time -- the resolver NEVER trusts
  -- them across cursor rewinds (undo/redo move op_cursor); it recomputes from the
  -- watermark every read.
  last_verified_op_cursor INTEGER,
  stale_reason TEXT,
  stale_at TEXT,
  -- sheet.refresh claim/lease (mirrors embedding_indexes): a non-null token
  -- past its lease is a crashed refresh and is reclaimable; two live refreshes
  -- of one sheet conflict on sheet_refresh_busy.
  refresh_claim_token TEXT,
  refresh_lease_expires_at TEXT,
  -- Sheet-level row-title override
  -- item 22): the column '...' menu's "Use as row title" sets this to a
  -- columns(id); NULL means "no explicit override" and every reader falls
  -- back to the frontend's default (first column per the grid's current
  -- drag order, falling back to canonical column order) via the shared
  -- web/src/workbench/rowTitle.ts helper. Not FK-constrained: a stale id
  -- (its column deleted/hidden-by-undo) is treated as unset by every reader,
  -- same idiom as parent_op_id.
  title_column_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS columns (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'text',
  position INTEGER NOT NULL DEFAULT 0,
  current_run_id INTEGER,
  ai_generated INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0,
  default_hidden INTEGER NOT NULL DEFAULT 0,
  format TEXT,  -- display hint: filesize | currency | percent | null
  -- Explicit semantic marker for columns whose contents follow a named
  -- contract (currently only 'entity_mentions', written by map.ner) — as
  -- distinct from `type`, which is the column's physical storage type. NEVER
  -- inferred by duck-typing cell contents; a column is only eligible for
  -- contract-consuming features (e.g. the Mentions panel) when this is set.
  semantic_type TEXT,

  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(sheet_id, name)
);

CREATE TABLE IF NOT EXISTS rows (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  parent_row_id INTEGER REFERENCES rows(id),
  hidden INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rows_sheet ON rows(sheet_id, position);
CREATE INDEX IF NOT EXISTS idx_rows_parent ON rows(parent_row_id);

-- One compact ownership record per logical source/base write. ``stage_id`` is
-- the durable admitted identity available before a streamed write publishes;
-- publication binds ``op_id`` once instead of rewriting every staged cell.
-- The operation remains the provenance envelope -- this row is only linkage.
-- BASE_CELL_PRODUCERS_BEGIN
CREATE TABLE IF NOT EXISTS base_cell_producers (
  id INTEGER PRIMARY KEY,
  stage_id TEXT NOT NULL UNIQUE CHECK (length(trim(stage_id)) > 0),
  op_id INTEGER REFERENCES ops(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- BASE_CELL_PRODUCERS_END

-- Source/static cell values (imported data). JSON-encoded.
CREATE TABLE IF NOT EXISTS cells (
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  value TEXT,
  -- NULL is reserved for migrated history whose producer was never recorded.
  -- CELL_PRODUCER_ID_BEGIN
  producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  -- CELL_PRODUCER_ID_END
  PRIMARY KEY (row_id, column_id)
) WITHOUT ROWID;
-- CELL_COLUMN_INDEX_BEGIN
CREATE INDEX IF NOT EXISTS idx_cells_column ON cells(column_id, row_id);
-- CELL_COLUMN_INDEX_END

-- Append-only operation log. spec is declarative JSON. undo_info captures
-- the pointer state needed to step back (e.g. prior current_run_id per column).
CREATE TABLE IF NOT EXISTS ops (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT,
  spec TEXT NOT NULL DEFAULT '{}',
  undo_info TEXT,
  status TEXT NOT NULL DEFAULT 'applied',  -- applied | undone | barrier
  barrier INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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
  PRIMARY KEY (materialized_row_id, source_row_id, role)
);
CREATE INDEX IF NOT EXISTS idx_materialized_row_sources_source
  ON materialized_row_sources(source_sheet_id, source_row_id, role);
CREATE INDEX IF NOT EXISTS idx_materialized_row_sources_materialized
  ON materialized_row_sources(materialized_row_id, role);
CREATE INDEX IF NOT EXISTS idx_materialized_row_sources_op
  ON materialized_row_sources(op_id);
-- MATERIALIZED_ROW_SOURCES_END

-- Run = one execution of an op over a sheet. The provenance envelope,
-- stored once. cost figures in USD.
CREATE TABLE IF NOT EXISTS runs (
  -- AUTOINCREMENT is intentional: paid row-effect checkpoints outlive a
  -- compacted run and retain its numeric id as their durable group key.  A
  -- reused ROWID would make that orphan look attached to an unrelated future
  -- run and could replay/refuse the wrong provider effect.
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id INTEGER NOT NULL REFERENCES ops(id),
  sheet_id INTEGER NOT NULL REFERENCES sheets(id),
  -- Canonical action identity. There is deliberately no persisted runner
  -- implementation alias or compatibility column.
  action_kind TEXT NOT NULL,
  action_version TEXT NOT NULL DEFAULT '1',
  model TEXT,
  prompt_hash TEXT,
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'running',  -- running|completed|failed|cancelled
  -- Durable cooperative cancellation INTENT. This is deliberately separate
  -- from ``status``: a live writer keeps the run ``running`` until its fenced
  -- terminal transaction closes the run, receipt, attempt, and claim together.
  cancel_requested_at TEXT,
  total_rows INTEGER NOT NULL DEFAULT 0,
  completed_rows INTEGER NOT NULL DEFAULT 0,
  failed_rows INTEGER NOT NULL DEFAULT 0,
  cost_estimate REAL,
  -- Projection of this run's model_calls facts (RunResultStore.
  -- _project_cost_actual, the one writer): SUM of live provider_cost_usd,
  -- cache facts excluded. NULL means at least one live call's provider cost
  -- is unknown — an honest cannot-say, never rendered as $0.
  cost_actual REAL DEFAULT 0,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  -- Code identity of the worker process that claimed and executed this run
  -- (worker-version-guard-v1: frisket.worker_version.code_version(), stamped
  -- by the project.run/action.run handlers). NULL for runs never claimed off
  -- a queue (synchronous/local runs) or written before this column existed.
  worker_version TEXT,
  -- The attempt this run last claimed, written inside the
  -- owned claim transaction. Reconsent reads it under the same lock and
  -- refuses while it names a live 'dispatching' attempt, which is what makes
  -- "one attempt = one authorization decision" structural rather than
  -- advisory. NULL until a first claim commits.
  current_attempt_id TEXT REFERENCES execution_attempts(id),
  -- Halt state as columns rather than keys inside `params`.
  -- Relocation, not retirement: `begin_run_resume` clears them, a refused
  -- resume restores them verbatim (`revert_run_resume`), and
  -- `halted_reason`-without-`halted_code` stays representable so the legacy
  -- circuit breaker's halts are not reclassified as typed invocation halts.
  halted_code TEXT,
  halted_reason TEXT,
  -- The hosted funding/edition context the action
  -- boundary attaches after run creation, off `params` so `start_run`'s
  -- INSERT is the spec's writer.
  edition_run_context TEXT,
  consent_principal TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_op ON runs(op_id);
CREATE INDEX IF NOT EXISTS idx_runs_started_id ON runs(started_at DESC, id DESC);

-- Immutable row scope for a run. A full-sheet run records the rows visible at
-- launch time so later row additions/hides do not change run inspection.
CREATE TABLE IF NOT EXISTS run_scopes (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  row_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_rows (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY (run_id, row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_run_rows_position ON run_rows(run_id, position);

-- Per-cell deltas stay slim so result storage scales with changed cells.
CREATE TABLE IF NOT EXISTS results (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value TEXT,
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
    publication_effect IS NULL
    OR (
      publication_effect = 'publish_value'
      AND value IS NOT NULL
      AND error IS NULL
    )
    OR (
      publication_effect = 'publish_null'
      AND value IS NULL
      AND error IS NULL
    )
    OR (
      publication_effect = 'publish_error'
      AND value IS NULL
      AND error IS NOT NULL
    )
  )
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_results_rowcol ON results(column_id, row_id);
CREATE INDEX IF NOT EXISTS idx_results_run_col_row ON results(run_id, column_id, row_id);
CREATE INDEX IF NOT EXISTS idx_results_column_run ON results(column_id, run_id);

-- Normalized model/provider usage facts. Results keep the user-facing cell
-- values; model_calls keeps the neutral execution facts that produced those
-- values, across local, hosted API, sidecar, and serverless providers:
-- provider identity, units, provider cost, cost source, cache evidence, and the
-- credential provenance of the call. Tariffs, billability and credit charges are
-- external settlement outputs and are NOT columns here
-- (public schema boundary).
CREATE TABLE IF NOT EXISTS model_calls (
  id TEXT PRIMARY KEY,
  fact_version TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER,
  column_id INTEGER,
  capability TEXT NOT NULL,
  engine TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_kind TEXT NOT NULL,
  model_ids TEXT NOT NULL DEFAULT '[]',
  credential_source TEXT NOT NULL DEFAULT 'none',
  provider_reported_cost_usd REAL,
  provider_cost_usd REAL,
  cost_source TEXT NOT NULL DEFAULT 'unknown',
  units TEXT NOT NULL DEFAULT '{}',
  cache TEXT NOT NULL DEFAULT '{}',
  request_id TEXT,
  warnings TEXT NOT NULL DEFAULT '[]',
  -- Execution routes: the binding epoch this
  -- fact was observed under (binding_epochs.id). NULL only on legacy rows;
  -- the fact writer asserts non-NULL whenever a route is in
  -- scope, so legacy NULL stays distinguishable from a writer defect.
  epoch_id TEXT,
  -- The settlement join: the execution_attempts row that
  -- AUTHORIZED this call. "What did it cost" = the sum of the rated calls
  -- carrying an attempt's id, under that attempt's pinned settlement terms —
  -- authorization joined to settlement on attempt_id. Deliberately NOT a
  -- settlement table and not a second write path: a measured quantity only
  -- exists at this grain, so the attempt-level answer is a receipt-time JOIN.
  -- NULL on unrouted/legacy work (no attempt authorized it), the same honest
  -- absence epoch_id uses.
  attempt_id TEXT,
  -- How long THIS call took, in milliseconds, measured monotonically at the
  -- site that brackets the provider request (the LLM path: ModelRouter's own
  -- per-attempt clock). Purely descriptive: nothing branches on it, no gate
  -- reads it, and it is never an input to cost. Run-level wall time already
  -- lives on runs.started_at/finished_at; this is the per-call grain that
  -- answers "which call was slow" without a second timing store.
  -- NULL is honest and common: a cache hit made no provider call, and a
  -- transport whose bracket has not been wired yet does not guess.
  duration_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_rowcol ON model_calls(row_id, column_id);

-- THE shared reserve->returned->consume/refuse lifecycle for paid external
-- effects (engine/store/effect_checkpoints.py).  One table, one state
-- machine; live families:
--   row_effect              MapRunner row effects (group = run id, unit =
--                           row id; retire-on-consume).  Deliberately no
--                           rows FK anywhere here: a source refresh may
--                           replace physical row ids while a returned/
--                           ambiguous external effect remains financial
--                           truth, so the checkpoint must survive that
--                           compaction and keep refusing/reconciling.
--   model_call              single provider-call actions (cluster.values;
--                           group = action kind, unit = checkpoint id;
--                           retired by the owning action's result txn).
--   reduce_group_summary    reduce-family group summaries (reduces.py).
--   embedding_index_refresh embedding refresh batches (embeddings.py).
-- ``family`` plus caller-owned group/unit keys give each family its
-- logical-unit uniqueness; ``identity`` is an opaque caller string compared
-- verbatim (drift refuses); ``payload`` is canonical JSON whose semantics
-- stay caller-owned (a ``reserved`` row may carry caller recovery context).
-- ``reserved`` is deliberately ambiguous and refuses automatic retry;
-- ``returned`` is safe to replay without egress; ``consumed`` is a
-- caller-selected terminal shape; retire and proven-no-egress discard delete
-- the row. ``authorized_attempt_id`` permanently owns the unit's
-- provider facts even when a later attempt consumes the returned unit.
CREATE TABLE IF NOT EXISTS effect_checkpoints (
  id TEXT PRIMARY KEY,
  family TEXT NOT NULL,
  group_key TEXT NOT NULL,
  unit_key TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  identity TEXT NOT NULL,
  authorized_attempt_id TEXT REFERENCES execution_attempts(id),
  state TEXT NOT NULL CHECK (state IN ('reserved', 'returned', 'consumed')),
  payload TEXT,
  accounting_persisted INTEGER NOT NULL DEFAULT 0 CHECK (accounting_persisted IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (
    (state='reserved' AND accounting_persisted=0) OR
    (state IN ('returned', 'consumed') AND payload IS NOT NULL)
  ),
  UNIQUE (family, group_key, unit_key)
);

-- Durable v1 action receipts. Debug traces are optional sidecars; receipts are
-- queryable product/audit data.
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  action_kind TEXT NOT NULL,
  action_id TEXT,
  idempotency_key TEXT,
  params_hash TEXT,
  status TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '{}',
  -- Opaque edition context for a runless queued action. This is deliberately
  -- outside the public receipt body, matching runs.edition_run_context: the
  -- composing edition owns its schema and the open worker only carries it to
  -- terminal settlement.
  edition_run_context TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_receipts_run ON receipts(run_id);
-- Newest-first receipt scans tiebreak on insertion order (rowid), not the
-- random receipt id: created_at has second granularity, so two receipts in
-- one second ordered by id was a coin flip for "current".
CREATE INDEX IF NOT EXISTS idx_receipts_created_id ON receipts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_backfill_activity_created
  ON receipts(created_at DESC, id DESC)
  WHERE action_kind='run.backfill'
    AND status='completed'
    AND json_extract(body, '$.outputs[0].ref.kind')='run_backfill'
    AND json_extract(body, '$.outputs[0].ref.sheet_id') IS NOT NULL
    AND json_extract(body, '$.outputs[0].ref.column_id') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_backfill_activity_column_created
  ON receipts(CAST(json_extract(body, '$.outputs[0].ref.column_id') AS INTEGER), created_at DESC, id DESC)
  WHERE action_kind='run.backfill'
    AND status='completed'
    AND json_extract(body, '$.outputs[0].ref.kind')='run_backfill'
    AND json_extract(body, '$.outputs[0].ref.sheet_id') IS NOT NULL
    AND json_extract(body, '$.outputs[0].ref.column_id') IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_idempotency
  ON receipts(idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- Project-scoped workbench plugin ENABLEMENT state. Package
-- IDENTITY (validated source, manifest/package digests, plugin.load manifest
-- evidence) is workspace-owned in <root>/.frisket/plugin_packages.db, keyed by
-- plugin_id -- exactly one durable package per plugin per workspace. This row
-- records ONLY what a project owns: its enablement decision, the capabilities
-- it accepted, and its per-project executable trust grant. No package identity
-- column lives here; a green-field reset, no migration bridge.
CREATE TABLE IF NOT EXISTS workbench_plugin_installs (
  plugin_id TEXT PRIMARY KEY,
  install_state TEXT NOT NULL,
  activation TEXT NOT NULL,
  permissions_accepted TEXT NOT NULL DEFAULT '[]',
  arbitrary_package_load_allowed INTEGER NOT NULL DEFAULT 0,
  disabled_reason TEXT,
  executable_handlers_allowed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Project-scoped workbench plugin env vars. Values are write-only through
-- HTTP routes: encrypted stores the envelope ciphertext, hint is redacted.
-- The (plugin_id, name) primary key scopes every secret to one plugin: the
-- execution read path (_project_plugin_env) resolves a plugin's declared env
-- from THIS table alone, never the global project_secrets namespace, so two
-- plugins declaring the same name get isolated values and a plugin cannot
-- read or overwrite a core-owned credential.
CREATE TABLE IF NOT EXISTS workbench_plugin_env_vars (
  plugin_id TEXT NOT NULL,
  name TEXT NOT NULL,
  encrypted TEXT NOT NULL,
  hint TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (plugin_id, name)
);

-- Shared project secrets. Values are write-only; consumers resolve a declared
-- name through project secrets before falling back to hosted organization env.
CREATE TABLE IF NOT EXISTS project_secrets (
  name TEXT PRIMARY KEY,
  encrypted TEXT NOT NULL,
  hint TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS project_secret_consumers (
  kind TEXT NOT NULL,
  consumer_id TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (kind, consumer_id, name)
);

CREATE TABLE IF NOT EXISTS project_secret_migration_conflicts (
  plugin_id TEXT NOT NULL,
  name TEXT NOT NULL,
  hint TEXT,
  status TEXT NOT NULL DEFAULT 'needs_resolution',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (plugin_id, name)
);

-- Project-level provider keys override organization provider keys for the same
-- provider. They stay separate from plugin secrets and funding policy.
CREATE TABLE IF NOT EXISTS project_provider_keys (
  provider TEXT PRIMARY KEY,
  encrypted TEXT NOT NULL,
  hint TEXT NOT NULL,
  spend_cap_micro INTEGER,
  -- Accrued PROVIDER spend against THIS key, summed from model_calls facts
  -- whose credential_source is 'project_key' (store/credentials.py
  -- accrue_provider_spend is the only writer). This is the sum of calls
  -- whose price is KNOWN.
  spent_micro INTEGER NOT NULL DEFAULT 0,
  -- Live provider calls on this key whose price is NOT known (an unpriced
  -- model: pricing.cost_of returned None). Absence of a price is NOT zero
  -- spend, so it is counted here instead of being folded into spent_micro
  -- as 0. A capped key with unmetered calls has undetermined spend and the
  -- launch guard fails closed on it.
  --
  -- Both counters SURVIVE a key save (editing the cap does not forgive the
  -- spend accrued against it); only a new key VALUE starts them over, since
  -- the provider-side bill starts over with the credential.
  unmetered_calls INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Non-secret host-rendered plugin settings. Secret-looking values are rejected
-- before storage; plugin-owned arbitrary settings UI is not admitted.
CREATE TABLE IF NOT EXISTS workbench_plugin_settings (
  plugin_id TEXT NOT NULL,
  setting_id TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (plugin_id, setting_id)
);

-- Semantic claims for long-running generated-column actions. These are not
-- SQLite locks: they protect column visibility/promotion while work runs.
CREATE TABLE IF NOT EXISTS output_column_claims (
  id TEXT PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  column_id INTEGER REFERENCES columns(id) ON DELETE CASCADE,
  output_name TEXT NOT NULL,
  run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  op_id INTEGER REFERENCES ops(id) ON DELETE SET NULL,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  job_id INTEGER,
  action_kind TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'replace',
  expected_current_run_id INTEGER,
  claim_token TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  renewed_at TEXT,
  lease_expires_at TEXT,
  released_at TEXT,
  details TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_claims_active_name
  ON output_column_claims(sheet_id, output_name)
  WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_claims_active_column
  ON output_column_claims(column_id)
  WHERE status='active' AND column_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_output_claims_claim_token
  ON output_column_claims(claim_token);
CREATE INDEX IF NOT EXISTS idx_output_claims_run ON output_column_claims(run_id);
CREATE INDEX IF NOT EXISTS idx_output_claims_receipt ON output_column_claims(receipt_id);
CREATE INDEX IF NOT EXISTS idx_output_claims_status_expiry
  ON output_column_claims(status, lease_expires_at);

-- One immutable output-generation declaration per run/output. A run may
-- contain both a progressively visible fresh output (`create`) and an atomic
-- replacement (`replace_scope`), so publication state belongs here rather
-- than on runs. The (run_id, column_id) pair is the generation identity; no
-- surrogate or global generation sequence is minted.
CREATE TABLE IF NOT EXISTS run_output_generations (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE RESTRICT,
  output_role TEXT NOT NULL CHECK (length(trim(output_role)) > 0),
  compatibility_key TEXT NOT NULL CHECK (length(trim(compatibility_key)) > 0),
  write_mode TEXT NOT NULL CHECK (write_mode IN ('create', 'replace_scope')),
  state TEXT NOT NULL CHECK (state IN ('active', 'staged', 'sealed')),
  claim_token TEXT NOT NULL CHECK (length(claim_token) > 0),
  expected_base_run_id INTEGER REFERENCES runs(id) ON DELETE RESTRICT,
  terminal_disposition TEXT CHECK (
    terminal_disposition IN ('completed', 'partial', 'failed', 'cancelled')
  ),
  declared_at TEXT NOT NULL DEFAULT (datetime('now')),
  sealed_at TEXT,
  PRIMARY KEY (run_id, column_id),
  UNIQUE (run_id, output_role),
  CHECK (
    (write_mode = 'create' AND state IN ('active', 'staged', 'sealed'))
    OR (write_mode = 'replace_scope' AND state IN ('staged', 'sealed'))
  ),
  CHECK (
    (state = 'sealed' AND terminal_disposition IS NOT NULL AND sealed_at IS NOT NULL)
    OR (state <> 'sealed' AND terminal_disposition IS NULL AND sealed_at IS NULL)
  )
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_run_output_generations_column
  ON run_output_generations(column_id, run_id);

-- Declaration is the boundary between legacy/unmanaged result rows and the
-- explicit publication contract.  The complete output set must precede every
-- result for this run; adopting an already-written legacy sibling would leave
-- a mixed managed/unmanaged run and make terminal sealing ambiguous.
CREATE TRIGGER IF NOT EXISTS trg_run_output_generations_precede_results
BEFORE INSERT ON run_output_generations
WHEN
  EXISTS (
    SELECT 1 FROM results result
    WHERE result.run_id = NEW.run_id
  )
  OR EXISTS (
    SELECT 1 FROM run_output_generations sibling
    WHERE sibling.run_id = NEW.run_id
      AND sibling.state = 'sealed'
  )
BEGIN
  SELECT RAISE(ABORT, 'complete output generation set must precede publication');
END;

-- Rebuildable per-cell publication projection. The first FK proves that a
-- head names an exact attempted result; the second proves that result belongs
-- to a declared output generation. Authoritative history lives in ops,
-- generations, and results -- never in this table.
CREATE TABLE IF NOT EXISTS cell_result_heads (
  column_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  PRIMARY KEY (column_id, row_id),
  FOREIGN KEY (run_id, row_id, column_id)
    REFERENCES results(run_id, row_id, column_id) ON DELETE RESTRICT,
  FOREIGN KEY (run_id, column_id)
    REFERENCES run_output_generations(run_id, column_id) ON DELETE RESTRICT
) WITHOUT ROWID;

-- An effect is assigned only after the owning generation is declared and
-- while its journal op is still applied. Sealed generations cannot acquire
-- late results.
CREATE TRIGGER IF NOT EXISTS trg_results_managed_insert_requires_effect
BEFORE INSERT ON results
WHEN EXISTS (
    SELECT 1 FROM run_output_generations generation
    WHERE generation.run_id = NEW.run_id
  )
  AND (
    NEW.publication_effect IS NULL
    OR NOT EXISTS (
      SELECT 1 FROM run_output_generations generation
      WHERE generation.run_id = NEW.run_id
      AND generation.column_id = NEW.column_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'generation-managed run result requires a declared output and publication effect');
END;

CREATE TRIGGER IF NOT EXISTS trg_results_managed_update_requires_effect
BEFORE UPDATE ON results
WHEN EXISTS (
    SELECT 1 FROM run_output_generations generation
    WHERE generation.run_id = NEW.run_id
  )
  AND (
    NEW.publication_effect IS NULL
    OR NOT EXISTS (
      SELECT 1 FROM run_output_generations generation
      WHERE generation.run_id = NEW.run_id
      AND generation.column_id = NEW.column_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'generation-managed run result requires a declared output and publication effect');
END;

CREATE TRIGGER IF NOT EXISTS trg_results_effect_insert_open_generation
BEFORE INSERT ON results
WHEN NEW.publication_effect IS NOT NULL
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

CREATE TRIGGER IF NOT EXISTS trg_results_effect_update_open_generation
BEFORE UPDATE OF publication_effect ON results
WHEN OLD.publication_effect IS NULL
  AND NEW.publication_effect IS NOT NULL
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

-- An exact idempotent upsert of an existing effect still has to belong to an
-- open applied generation. Review-only updates are intentionally outside the
-- trigger column list and remain valid after publication.
CREATE TRIGGER IF NOT EXISTS trg_results_semantic_update_open_generation
BEFORE UPDATE OF value, tokens_in, tokens_out, confidence, justification,
  error, error_code, outcome, publication_effect ON results
WHEN NEW.publication_effect IS NOT NULL
  AND (
    NEW.run_id IS NOT OLD.run_id
    OR NEW.row_id IS NOT OLD.row_id
    OR NEW.column_id IS NOT OLD.column_id
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

-- An effect-bearing result is immutable while its authoritative generation
-- exists. Exact idempotent upserts are allowed, and review_state deliberately
-- remains mutable. Discard GC removes the generation before deleting results.
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
        NEW.value IS NOT OLD.value
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

CREATE TRIGGER IF NOT EXISTS trg_results_published_no_delete
BEFORE DELETE ON results
WHEN OLD.publication_effect IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM run_output_generations generation
    WHERE generation.run_id = OLD.run_id
      AND generation.column_id = OLD.column_id
  )
BEGIN
  SELECT RAISE(ABORT, 'published result is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_run_output_generations_immutable_declaration
BEFORE UPDATE ON run_output_generations
WHEN NEW.run_id IS NOT OLD.run_id
  OR NEW.column_id IS NOT OLD.column_id
  OR NEW.output_role IS NOT OLD.output_role
  OR NEW.compatibility_key IS NOT OLD.compatibility_key
  OR NEW.write_mode IS NOT OLD.write_mode
  OR NEW.claim_token IS NOT OLD.claim_token
  OR NEW.expected_base_run_id IS NOT OLD.expected_base_run_id
  OR NEW.declared_at IS NOT OLD.declared_at
BEGIN
  SELECT RAISE(ABORT, 'output generation declaration is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_run_output_generations_forward_only
BEFORE UPDATE OF state ON run_output_generations
WHEN NEW.state IS NOT OLD.state
  AND NOT (OLD.state IN ('active', 'staged') AND NEW.state = 'sealed')
BEGIN
  SELECT RAISE(ABORT, 'output generation state is forward-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_run_output_generations_terminal_immutable
BEFORE UPDATE OF terminal_disposition, sealed_at ON run_output_generations
WHEN OLD.state = 'sealed' AND NEW.state = 'sealed'
  AND (
    NEW.terminal_disposition IS NOT OLD.terminal_disposition
    OR NEW.sealed_at IS NOT OLD.sealed_at
  )
BEGIN
  SELECT RAISE(ABORT, 'sealed output generation is immutable');
END;

-- Output generations are authoritative redo history, not a cache.  GC may
-- remove one only after its op has become permanently unredoable; head/result
-- foreign keys then enforce projection-first deletion order.
CREATE TRIGGER IF NOT EXISTS trg_run_output_generations_delete_discarded_only
BEFORE DELETE ON run_output_generations
WHEN EXISTS (
  SELECT 1
  FROM runs run
  JOIN ops op ON op.id = run.op_id
  WHERE run.id = OLD.run_id
    AND op.status <> 'discarded'
)
BEGIN
  SELECT RAISE(ABORT, 'output generation can be deleted only after op discard');
END;

CREATE TRIGGER IF NOT EXISTS trg_cell_result_heads_require_publishable_result
BEFORE INSERT ON cell_result_heads
WHEN NOT EXISTS (
  SELECT 1
  FROM results result
  JOIN run_output_generations generation
    ON generation.run_id = result.run_id
   AND generation.column_id = result.column_id
  JOIN runs run ON run.id = generation.run_id
  JOIN ops op ON op.id = run.op_id
  WHERE result.run_id = NEW.run_id
    AND result.row_id = NEW.row_id
    AND result.column_id = NEW.column_id
    AND result.publication_effect IS NOT NULL
    AND generation.state IN ('active', 'sealed')
    AND op.status = 'applied'
)
BEGIN
  SELECT RAISE(ABORT, 'cell head requires an applied publishable result');
END;

CREATE TRIGGER IF NOT EXISTS trg_cell_result_heads_update_publishable_result
BEFORE UPDATE ON cell_result_heads
WHEN NOT EXISTS (
  SELECT 1
  FROM results result
  JOIN run_output_generations generation
    ON generation.run_id = result.run_id
   AND generation.column_id = result.column_id
  JOIN runs run ON run.id = generation.run_id
  JOIN ops op ON op.id = run.op_id
  WHERE result.run_id = NEW.run_id
    AND result.row_id = NEW.row_id
    AND result.column_id = NEW.column_id
    AND result.publication_effect IS NOT NULL
    AND generation.state IN ('active', 'sealed')
    AND op.status = 'applied'
)
BEGIN
  SELECT RAISE(ABORT, 'cell head requires an applied publishable result');
END;

-- Sparse manual-edit overlay. An edit batch is an op in the log.
CREATE TABLE IF NOT EXISTS edits (
  op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value TEXT,
  PRIMARY KEY (op_id, row_id, column_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_edits_rowcol ON edits(column_id, row_id, op_id);

-- Rebuildable visible-cell projection. Values stay in their existing JSON
-- encoding; null/error result heads and explicit edit clears still occupy a
-- row so an older layer can never bleed through. Source-cell public refs keep
-- op_id=NULL; base_producer_id is separate internal provenance enrichment.
-- Validity is derived from the winning value and current column descriptor;
-- the original JSON and its provenance remain untouched.
-- CURRENT_CELLS_BEGIN
CREATE TABLE IF NOT EXISTS current_cells (
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  value TEXT,
  origin_kind TEXT NOT NULL CHECK (
    origin_kind IN ('source_cell', 'run_result', 'manual_edit')
  ),
  origin_op_id INTEGER REFERENCES ops(id) ON DELETE CASCADE,
  origin_run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  base_producer_id INTEGER REFERENCES base_cell_producers(id) ON DELETE RESTRICT,
  validity TEXT NOT NULL CHECK (validity IN ('valid', 'missing', 'invalid')),
  PRIMARY KEY (column_id, row_id),
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
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_current_cells_row ON current_cells(row_id);
-- CURRENT_CELLS_END

-- Replay preserve+surface (replay-accept-surface-v1,
-- public schema boundary): a durable, PROJECT-GLOBAL
-- acknowledgement that a human reaffirmed their edit against a specific fresh
-- generated value. Keyed by value identity (row_id, column_id,
-- generated_value_hash) so an identical future value never re-nags while a
-- genuinely-newer/different generated value re-surfaces; run_id is stored for
-- provenance only. One editorial decision per project (no user scoping in v1).
CREATE TABLE IF NOT EXISTS replay_edit_dismissals (
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  generated_value_hash TEXT NOT NULL,
  run_id INTEGER,
  dismissed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (row_id, column_id, generated_value_hash)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_replay_dismissals_col
  ON replay_edit_dismissals(column_id, row_id);

-- Content-addressed blob references (files live in blobs/, never here).
CREATE TABLE IF NOT EXISTS blobs (
  hash TEXT PRIMARY KEY,
  filename TEXT,
  mime TEXT,
  size INTEGER,
  source_url TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Blob-to-blob derivation lineage (provenance-derived-audio-lineage-v1): a
-- derived blob (an ffmpeg-cut clip, or a future audio-extracted-from-video
-- track) records its source blob + the params used to derive it, so
-- provenance can trace a derived clip back to the recording it came from.
-- One row per derivation edge; a derived blob can itself be recorded as a
-- SOURCE for a further derivation (clip-of-a-clip), so the read side walks
-- the chain hop by hop rather than this table modeling a tree directly.
CREATE TABLE IF NOT EXISTS blob_derivations (
  id INTEGER PRIMARY KEY,
  derived_hash TEXT NOT NULL REFERENCES blobs(hash) ON DELETE CASCADE,
  source_hash TEXT NOT NULL REFERENCES blobs(hash) ON DELETE RESTRICT,
  op TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_blob_derivations_derived
  ON blob_derivations(derived_hash);
CREATE INDEX IF NOT EXISTS idx_blob_derivations_source
  ON blob_derivations(source_hash);

-- Canonical investigative evidence substrate. Artifacts and spans are source
-- objects; evidence_links attach product subjects to one or more spans.
CREATE TABLE IF NOT EXISTS source_artifacts (
  id INTEGER PRIMARY KEY,
  stable_id TEXT NOT NULL UNIQUE,
  artifact_kind TEXT NOT NULL,
  media_type TEXT NOT NULL,
  blob_hash TEXT,
  source_url TEXT,
  canonical_url TEXT,
  title TEXT,
  filename TEXT,
  page_count INTEGER,
  duration_ms INTEGER,
  source_sheet_id INTEGER,
  source_row_id INTEGER,
  source_column_id INTEGER,
  external_ref_json TEXT NOT NULL DEFAULT '{}',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_blob
  ON source_artifacts(blob_hash);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_source_cell
  ON source_artifacts(source_sheet_id, source_row_id, source_column_id);

-- ARTIFACT_TIMELINE_SEGMENTS_BEGIN
-- Canonical artifact-local time transform. V1 writers record exactly one
-- rate-1 row for an ordinary extracted clip.
CREATE TABLE IF NOT EXISTS artifact_timeline_segments (
  id INTEGER PRIMARY KEY,
  derived_artifact_id INTEGER NOT NULL
    REFERENCES source_artifacts(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  derived_start_ms INTEGER NOT NULL,
  derived_end_ms INTEGER NOT NULL,
  source_artifact_id INTEGER NOT NULL
    REFERENCES source_artifacts(id) ON DELETE RESTRICT,
  source_start_ms INTEGER NOT NULL,
  source_end_ms INTEGER NOT NULL,
  rate_num INTEGER NOT NULL DEFAULT 1,
  rate_den INTEGER NOT NULL DEFAULT 1,
  precision TEXT NOT NULL,
  producer_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE (derived_artifact_id, ordinal),
  CHECK (ordinal >= 0),
  CHECK (derived_start_ms >= 0 AND derived_end_ms > derived_start_ms),
  CHECK (source_start_ms >= 0 AND source_end_ms > source_start_ms),
  CHECK (rate_num > 0 AND rate_den > 0),
  CHECK (precision IN ('frame_accurate', 'sample_accurate', 'exact')),
  CHECK (derived_artifact_id <> source_artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_timeline_source_time
  ON artifact_timeline_segments(
    source_artifact_id, source_start_ms, source_end_ms
  );
-- ARTIFACT_TIMELINE_SEGMENTS_END

-- Host-owned runtime projection artifacts. Plugin backend code may propose
-- rows through a subprocess, but only the host stores artifacts here.
CREATE TABLE IF NOT EXISTS runtime_projection_artifacts (
  artifact_id TEXT PRIMARY KEY,
  projection_kind TEXT NOT NULL,
  plugin_id TEXT NOT NULL,
  target_key TEXT NOT NULL,
  source_generation TEXT NOT NULL,
  target_json TEXT NOT NULL,
  params_json TEXT NOT NULL,
  body_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_runtime_projection_artifacts_target
  ON runtime_projection_artifacts(projection_kind, target_key, updated_at);

-- TEXT_SURFACES_BEGIN
-- The coordinate surface a text span's char offsets
-- index. Distinct from source_artifacts (provenance): a span references ONE
-- text surface. 'cell' = one stored cell's exact string (renderable); 'composite'
-- = a synthesized string (NER join/template) that equals no cell and never draws
-- an inline layer. Append-only (a correction is a new row). No FK from the cell
-- locator to sheets/rows/columns: like source_artifacts.source_* it is historical
-- audit identity, checked for visibility/ownership at read time.
CREATE TABLE IF NOT EXISTS text_surfaces (
  id INTEGER PRIMARY KEY,
  stable_id TEXT NOT NULL UNIQUE,
  surface_kind TEXT NOT NULL CHECK (surface_kind IN ('cell', 'composite')),
  text_sheet_id INTEGER,
  text_row_id INTEGER,
  text_column_id INTEGER,
  value_ref_json TEXT CHECK (value_ref_json IS NULL OR json_valid(value_ref_json)),
  content_hash TEXT NOT NULL CHECK (
    length(content_hash) = 71
    AND substr(content_hash, 1, 7) = 'sha256:'
    AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  offset_unit TEXT NOT NULL
    CHECK (offset_unit IN ('unicode_codepoint', 'utf16_code_unit')),
  surface_ref_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(surface_ref_json)),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (
    (surface_kind = 'cell'
       AND text_sheet_id IS NOT NULL AND text_row_id IS NOT NULL
       AND text_column_id IS NOT NULL)
    OR
    (surface_kind = 'composite'
       AND text_sheet_id IS NULL AND text_row_id IS NULL AND text_column_id IS NULL
       AND value_ref_json IS NULL)
  )
);
CREATE INDEX IF NOT EXISTS idx_text_surfaces_cell
  ON text_surfaces(text_sheet_id, text_row_id, text_column_id)
  WHERE surface_kind = 'cell';
CREATE TRIGGER IF NOT EXISTS trg_text_surfaces_no_update
BEFORE UPDATE ON text_surfaces
BEGIN
  SELECT RAISE(ABORT, 'text surfaces are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_text_surfaces_no_delete
BEFORE DELETE ON text_surfaces
BEGIN
  SELECT RAISE(ABORT, 'text surfaces are append-only');
END;
-- TEXT_SURFACES_END

CREATE TABLE IF NOT EXISTS source_spans (
  id INTEGER PRIMARY KEY,
  stable_id TEXT NOT NULL UNIQUE,
  artifact_id INTEGER NOT NULL REFERENCES source_artifacts(id) ON DELETE CASCADE,
  span_kind TEXT NOT NULL,
  page_start INTEGER,
  page_end INTEGER,
  start_ms INTEGER,
  end_ms INTEGER,
  char_start INTEGER,
  char_end INTEGER,
  bbox_json TEXT NOT NULL DEFAULT '[]',
  selector_json TEXT NOT NULL DEFAULT '{}',
  quote TEXT,
  snippet TEXT,
  text_layer_hash TEXT,
  -- The coordinate surface these char offsets index.
  text_surface_id INTEGER REFERENCES text_surfaces(id) ON DELETE RESTRICT,
  preview_json TEXT NOT NULL DEFAULT '{}',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  -- Character offsets are meaningful only
  -- as a complete, ordered range over one explicitly named, content-addressed text
  -- surface. Page/bbox/timestamp selectors are independent and do not define the
  -- string indexed by char_start/char_end.
  CHECK (
    (char_start IS NULL AND char_end IS NULL)
    OR (
      typeof(char_start) = 'integer'
      AND typeof(char_end) = 'integer'
      AND char_start >= 0
      AND char_end > char_start
      AND text_surface_id IS NOT NULL
      AND text_layer_hash IS NOT NULL
    )
  )
);
CREATE INDEX IF NOT EXISTS idx_source_spans_artifact_kind
  ON source_spans(artifact_id, span_kind);
CREATE INDEX IF NOT EXISTS idx_source_spans_artifact_pages
  ON source_spans(artifact_id, page_start, page_end);
CREATE INDEX IF NOT EXISTS idx_source_spans_artifact_time
  ON source_spans(artifact_id, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS idx_source_spans_text_surface
  ON source_spans(text_surface_id, span_kind)
  WHERE text_surface_id IS NOT NULL;

-- The char-offset invariant as triggers rather than a CHECK: it spans two
-- tables (a span's offsets must match its text_surface), which a CHECK
-- constraint cannot see. Only inserts and coordinate-changing updates run it.
CREATE TRIGGER IF NOT EXISTS trg_source_spans_char_integrity_insert
BEFORE INSERT ON source_spans
WHEN
  ((NEW.char_start IS NULL) <> (NEW.char_end IS NULL))
  OR (
    NEW.char_start IS NOT NULL
    AND (
      typeof(NEW.char_start) <> 'integer'
      OR typeof(NEW.char_end) <> 'integer'
      OR NEW.char_start < 0
      OR NEW.char_end <= NEW.char_start
      OR NEW.text_surface_id IS NULL
      OR NEW.text_layer_hash IS NULL
      OR NOT EXISTS (
        SELECT 1 FROM text_surfaces AS ts
        WHERE ts.id = NEW.text_surface_id
          AND ts.content_hash = NEW.text_layer_hash
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'character offsets require a valid range and matching text surface');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_spans_char_integrity_update
BEFORE UPDATE OF char_start, char_end, text_surface_id, text_layer_hash ON source_spans
WHEN
  ((NEW.char_start IS NULL) <> (NEW.char_end IS NULL))
  OR (
    NEW.char_start IS NOT NULL
    AND (
      typeof(NEW.char_start) <> 'integer'
      OR typeof(NEW.char_end) <> 'integer'
      OR NEW.char_start < 0
      OR NEW.char_end <= NEW.char_start
      OR NEW.text_surface_id IS NULL
      OR NEW.text_layer_hash IS NULL
      OR NOT EXISTS (
        SELECT 1 FROM text_surfaces AS ts
        WHERE ts.id = NEW.text_surface_id
          AND ts.content_hash = NEW.text_layer_hash
      )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'character offsets require a valid range and matching text surface');
END;

CREATE TABLE IF NOT EXISTS evidence_links (
  id INTEGER PRIMARY KEY,
  stable_id TEXT NOT NULL UNIQUE,
  subject_kind TEXT NOT NULL,
  subject_ref_json TEXT NOT NULL DEFAULT '{}',
  sheet_id INTEGER,
  row_id INTEGER,
  column_id INTEGER,
  run_id INTEGER,
  op_id INTEGER,
  receipt_id TEXT,
  link_role TEXT NOT NULL DEFAULT 'support',
  status TEXT NOT NULL DEFAULT 'active',
  confidence REAL,
  pinned INTEGER NOT NULL DEFAULT 0,
  producer_json TEXT NOT NULL DEFAULT '{}',
  -- Writer-enforced on old bundles: the stable semantic family of an
  -- annotation layer, e.g. 'entities'. NULL for
  -- ordinary support/citation links, which never render as a reader layer.
  layer_family TEXT CHECK (
    layer_family IS NULL
    OR (length(layer_family) BETWEEN 1 AND 64
        AND substr(layer_family, 1, 1) GLOB '[a-z]'
        AND layer_family NOT GLOB '*[^a-z0-9_.-]*')
  ),
  stale_reason TEXT,
  stale_at TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_links_cell_status
  ON evidence_links(row_id, column_id, status);
CREATE INDEX IF NOT EXISTS idx_evidence_links_subject
  ON evidence_links(subject_kind, subject_ref_json);
CREATE INDEX IF NOT EXISTS idx_evidence_links_run_cell
  ON evidence_links(run_id, row_id, column_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_receipt
  ON evidence_links(receipt_id);
-- answers-view-cited-columns-signal-v1: the cited-column-availability probe
-- (SheetMeta.citedColumnIds, DISTINCT active column_ids for a sheet) filters
-- on sheet_id+status before touching column_id; idx_evidence_links_cell_status
-- leads on row_id so a sheet_id-scoped query would otherwise scan.
CREATE INDEX IF NOT EXISTS idx_evidence_links_sheet_status
  ON evidence_links(sheet_id, status, column_id);

CREATE TABLE IF NOT EXISTS evidence_link_spans (
  link_id INTEGER NOT NULL REFERENCES evidence_links(id) ON DELETE CASCADE,
  span_id INTEGER NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL DEFAULT 0,
  span_role TEXT NOT NULL DEFAULT 'support',
  required INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  PRIMARY KEY (link_id, span_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_evidence_link_spans_span
  ON evidence_link_spans(span_id);
-- The inverse-query path (surface -> span -> annotation
-- association -> link) filters junction rows by span_role='annotation'.
CREATE INDEX IF NOT EXISTS idx_evidence_link_spans_annotation
  ON evidence_link_spans(span_id, span_role, link_id);

-- Live sources, scheduling, and monitoring.
-- A source is a recurring fetch (URL feed, search, etc.) that lands new rows
-- on a target sheet. `schedule` is a cron-style cadence; `cursor` records
-- what was last seen for new-row detection; runs are tracked in source_runs.
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'url',      -- url | search | rss | api
  url TEXT,
  config TEXT NOT NULL DEFAULT '{}',     -- JSON: kind-specific params
  sheet_id INTEGER REFERENCES sheets(id) ON DELETE SET NULL,
  schedule TEXT,                         -- cron expression, NULL = manual
  enabled INTEGER NOT NULL DEFAULT 1,
  cursor TEXT,                           -- last-seen marker (new-row detection)
  last_checked_at TEXT,
  last_status TEXT,                      -- ok | error | never
  new_rows_total INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sources_sheet ON sources(sheet_id);

-- One poll of a source: how many rows were new, plus any error.
CREATE TABLE IF NOT EXISTS source_runs (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  op_id INTEGER REFERENCES ops(id) ON DELETE SET NULL,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'ok',     -- ok | error
  new_rows INTEGER NOT NULL DEFAULT 0,
  skipped_rows INTEGER NOT NULL DEFAULT 0,
  changed_rows INTEGER NOT NULL DEFAULT 0,
  revisions INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  cursor_before TEXT,
  cursor_after TEXT,
  duration_ms INTEGER,
  warning_count INTEGER NOT NULL DEFAULT 0,
  cost_micro INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_id);
CREATE INDEX IF NOT EXISTS idx_source_runs_source_started
  ON source_runs(source_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_source_runs_receipt ON source_runs(receipt_id);

CREATE TABLE IF NOT EXISTS source_items (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  source_item_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  item_hash TEXT NOT NULL,
  row_id INTEGER REFERENCES rows(id) ON DELETE SET NULL,
  first_seen_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
  last_seen_run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  raw_ref_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(source_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_source_items_source_dedupe
  ON source_items(source_id, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_source_items_run
  ON source_items(last_seen_run_id);

-- Saved views. A view is a named,
-- reusable filter/sort over a sheet. Creating or updating one is logged
-- as an op (ops.kind='view'), so sort/filter becomes provenance-bearing
-- instead of an ephemeral, unlogged grid setting (the Cheatsheet "sort the
-- sheet" provenance gap). `spec` is JSON: {filter, sort, columns, ...}.
CREATE TABLE IF NOT EXISTS views (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  spec TEXT NOT NULL DEFAULT '{}',       -- JSON: filter / sort / columns
  op_id INTEGER REFERENCES ops(id),      -- the op that created/last-saved it
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_views_sheet ON views(sheet_id);

-- Saved lenses: a named LensSpec — a normalized QuerySpec (e.g.
-- a row-anchor embedding_similarity) + presentation. Unlike a `views` row (a
-- presentation-only filter/sort blob) a lens carries the query KIND, so it can be
-- re-resolved on demand into a row-set with distance/score through the query spine.
CREATE TABLE IF NOT EXISTS lenses (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER REFERENCES sheets(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  spec TEXT NOT NULL DEFAULT '{}',       -- JSON: frisket.lens.v1 {query, presentation}
  op_id INTEGER REFERENCES ops(id),      -- the op that created/last-saved it
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lenses_sheet ON lenses(sheet_id);

-- Watchlists promote saved queries into recurring checks. A watch is a
-- saved query with its own cursor and manual run history. Notification
-- channels/scheduler hooks are intentionally not part of this MVP schema.
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'project', -- project | sheet
  sheet_id INTEGER REFERENCES sheets(id) ON DELETE CASCADE,
  query TEXT NOT NULL,
  query_version TEXT,
  query_hash TEXT,
  detection_policy TEXT NOT NULL DEFAULT '{"kind":"new_matches"}',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_evaluated_op INTEGER NOT NULL DEFAULT 0,
  last_run_id INTEGER,
  last_status TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_watches_sheet ON watches(sheet_id);

CREATE TABLE IF NOT EXISTS watch_runs (
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'ok',
  op_cursor_before INTEGER NOT NULL,
  op_cursor_after INTEGER NOT NULL,
  matched_rows INTEGER NOT NULL DEFAULT 0,
  new_rows INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  resolved_query_hash TEXT,
  resolved_query TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_runs_watch_started
  ON watch_runs(watch_id, started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS watch_run_hits (
  run_id INTEGER NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  column_id INTEGER REFERENCES columns(id) ON DELETE SET NULL,
  rank INTEGER NOT NULL,
  snippet TEXT,
  is_new INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, rank)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_watch_run_hits_run_rank
  ON watch_run_hits(run_id, rank);

CREATE TABLE IF NOT EXISTS watch_run_events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  event_kind TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_ref TEXT NOT NULL DEFAULT '{}',
  before_json TEXT,
  after_json TEXT,
  delta_json TEXT,
  severity TEXT NOT NULL DEFAULT 'info',
  rank INTEGER NOT NULL,
  snippet TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_watch_run_events_run_rank
  ON watch_run_events(run_id, rank, id);
CREATE INDEX IF NOT EXISTS idx_watch_run_events_watch_id
  ON watch_run_events(watch_id, id);
CREATE INDEX IF NOT EXISTS idx_watch_run_events_kind
  ON watch_run_events(event_kind, run_id, rank);

CREATE TABLE IF NOT EXISTS watch_seen_rows (
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  first_seen_run_id INTEGER NOT NULL,
  last_seen_run_id INTEGER NOT NULL,
  PRIMARY KEY (watch_id, sheet_id, row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_watch_seen_rows_watch
  ON watch_seen_rows(watch_id);

CREATE TABLE IF NOT EXISTS watch_row_field_state (
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  sheet_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  value_hash TEXT NOT NULL,
  value_json TEXT,
  last_observed_run_id INTEGER NOT NULL REFERENCES watch_runs(id) ON DELETE CASCADE,
  PRIMARY KEY (watch_id, sheet_id, row_id, column_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_watch_row_field_state_watch
  ON watch_row_field_state(watch_id, last_observed_run_id);

-- Generic notification core (Stage 2 lane 4). Domain facts stay in their
-- owning tables; notification_items are attention projections over those
-- facts with actor-local read/ack state and channel attempt records.
CREATE TABLE IF NOT EXISTS notification_items (
  id INTEGER PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL DEFAULT '{}',
  dedupe_key TEXT NOT NULL UNIQUE,
  source_event_ids TEXT NOT NULL DEFAULT '[]',
  event_count INTEGER NOT NULL DEFAULT 1,
  event_kinds TEXT NOT NULL DEFAULT '[]',
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  deep_link TEXT NOT NULL DEFAULT '{}',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notification_items_created
  ON notification_items(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_notification_items_source_kind
  ON notification_items(source_kind, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_notification_items_severity
  ON notification_items(severity, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS notification_actor_state (
  notification_id INTEGER NOT NULL REFERENCES notification_items(id) ON DELETE CASCADE,
  actor_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'unseen',
  seen_at TEXT,
  read_at TEXT,
  acknowledged_at TEXT,
  acknowledged_by TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (notification_id, actor_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_notification_actor_state_actor
  ON notification_actor_state(actor_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS notification_channels (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  secret_ref TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notification_channels_kind_enabled
  ON notification_channels(kind, enabled);

CREATE TABLE IF NOT EXISTS notification_routes (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  owner_kind TEXT NOT NULL DEFAULT 'project',
  owner_ref TEXT NOT NULL,
  recipient_actor_id TEXT,
  channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
  source_kind TEXT,
  source_ref_match_json TEXT NOT NULL DEFAULT '{}',
  event_kinds_json TEXT NOT NULL DEFAULT '[]',
  severity_min TEXT NOT NULL DEFAULT 'info',
  delivery_mode TEXT NOT NULL DEFAULT 'immediate',
  digest_cadence TEXT,
  digest_timezone TEXT NOT NULL DEFAULT 'UTC',
  digest_anchor_time TEXT,
  template_key TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notification_routes_enabled_mode
  ON notification_routes(enabled, delivery_mode);
CREATE INDEX IF NOT EXISTS idx_notification_routes_channel
  ON notification_routes(channel_id, enabled);

CREATE TABLE IF NOT EXISTS notification_digest_runs (
  id INTEGER PRIMARY KEY,
  route_id INTEGER NOT NULL REFERENCES notification_routes(id) ON DELETE CASCADE,
  channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
  cadence TEXT NOT NULL,
  window_key TEXT NOT NULL,
  window_start_at TEXT NOT NULL,
  window_end_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'composed',
  item_count INTEGER NOT NULL DEFAULT 0,
  delivery_request_id INTEGER REFERENCES notification_delivery_requests(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(route_id, window_key)
);
CREATE INDEX IF NOT EXISTS idx_notification_digest_runs_status
  ON notification_digest_runs(status, window_end_at, id);
CREATE INDEX IF NOT EXISTS idx_notification_digest_runs_route
  ON notification_digest_runs(route_id, window_end_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS notification_digest_items (
  digest_run_id INTEGER NOT NULL REFERENCES notification_digest_runs(id) ON DELETE CASCADE,
  notification_id INTEGER NOT NULL REFERENCES notification_items(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (digest_run_id, notification_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_notification_digest_items_notification
  ON notification_digest_items(notification_id, digest_run_id);

CREATE TABLE IF NOT EXISTS notification_delivery_requests (
  id INTEGER PRIMARY KEY,
  route_id INTEGER REFERENCES notification_routes(id) ON DELETE SET NULL,
  channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
  notification_id INTEGER REFERENCES notification_items(id) ON DELETE CASCADE,
  digest_run_id INTEGER REFERENCES notification_digest_runs(id) ON DELETE CASCADE,
  delivery_kind TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'queued',
  job_id INTEGER,
  available_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_error TEXT,
  provider_ref TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_requests_status
  ON notification_delivery_requests(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_requests_route
  ON notification_delivery_requests(route_id, status);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_requests_item
  ON notification_delivery_requests(notification_id, route_id);

CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
  id INTEGER PRIMARY KEY,
  delivery_request_id INTEGER REFERENCES notification_delivery_requests(id) ON DELETE SET NULL,
  notification_id INTEGER REFERENCES notification_items(id) ON DELETE CASCADE,
  channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  provider_ref TEXT,
  error TEXT,
  response_meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_attempts_item_channel
  ON notification_delivery_attempts(notification_id, channel_id, attempt);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_attempts_status
  ON notification_delivery_attempts(status, created_at);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_attempts_request
  ON notification_delivery_attempts(delivery_request_id, attempt);

-- Native embeddings. Canonical
-- definitions + provenance live here; raw vectors live in the rebuildable
-- project.embeddings.db sidecar, never in project.db.
--
-- An embedding_space is a strict comparability contract: vectors in different
-- spaces must not be compared/clustered/watched together. `id` (space_id) and
-- `descriptor_hash` are both generated from the strict descriptor — every
-- parameter that can change vector values or distance semantics. Labels are UI
-- sugar and live on the index (name), never in space identity.
CREATE TABLE IF NOT EXISTS embedding_spaces (
  id TEXT PRIMARY KEY,
  descriptor_json TEXT NOT NULL,
  descriptor_hash TEXT NOT NULL UNIQUE,
  provider_id TEXT NOT NULL,
  provider_kind TEXT NOT NULL,
  requested_model TEXT,
  actual_model_id TEXT NOT NULL,
  model_revision TEXT,
  modality TEXT NOT NULL,
  dimension INTEGER NOT NULL CHECK (dimension > 0),
  dtype TEXT NOT NULL,
  distance_metric TEXT NOT NULL,
  normalization TEXT NOT NULL,
  vector_options_hash TEXT NOT NULL,
  source_payload_contract TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- An embedding_index is the project artifact: which source values are embedded,
-- under what source/extraction + maintenance + provider policy, in which space.
CREATE TABLE IF NOT EXISTS embedding_indexes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  space_id TEXT NOT NULL REFERENCES embedding_spaces(id),
  sheet_id INTEGER,
  source_query_json TEXT NOT NULL,
  source_columns_json TEXT NOT NULL,
  source_policy_json TEXT NOT NULL,
  source_policy_hash TEXT NOT NULL,
  maintenance_policy_json TEXT NOT NULL,
  provider_policy_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',
  total_items INTEGER NOT NULL DEFAULT 0,
  ready_items INTEGER NOT NULL DEFAULT 0,
  stale_items INTEGER NOT NULL DEFAULT 0,
  error_items INTEGER NOT NULL DEFAULT 0,
  last_refresh_receipt_id TEXT,
  last_refresh_job_id TEXT,
  last_refreshed_at TEXT,
  -- index-level refresh claim/lease (NOT output_column_claims: refresh writes no
  -- visible column). A non-null token past its lease is a crashed refresh and is
  -- reclaimable; two live refreshes of one index conflict on embedding_index_busy.
  refresh_claim_token TEXT,
  refresh_lease_expires_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embedding_indexes_space
  ON embedding_indexes(space_id);
CREATE INDEX IF NOT EXISTS idx_embedding_indexes_sheet
  ON embedding_indexes(sheet_id);

-- Execution-route state:
-- consent artifacts, promise-set chains, resolved routes, binding epochs, and
-- route violations. Chains are linear per subject (head = MAX(seq)); the seq
-- UNIQUEs are the CAS backstop for the BEGIN IMMEDIATE chain-write protocol
-- (frisket.engine.store.execution_routes).

CREATE TABLE IF NOT EXISTS consents (
  id TEXT PRIMARY KEY,               -- "consent_" + ulid
  subject_kind TEXT CHECK (subject_kind IN ('run','receipt')), -- NULL for standing consents
  subject_id TEXT,
  action_identity_hash TEXT,          -- canonical params hash; NULL for standing
  promise_set_hash TEXT,              -- content hash; NULL for standing
  standing_policy TEXT,               -- e.g. 'cost_under_gate_v1' (§5.1); NULL for per-action
  actor TEXT NOT NULL,               -- principal precedence: the
                                     -- authenticated user/service subject when one
                                     -- exists, else 'deployment:<instance-id>'
                                     -- (a stable installation principal minted at
                                     -- init); never a bare shared role label
  granted_at TEXT NOT NULL,
  policy_params_json TEXT,           -- F2: persisted standing-policy params
                                     -- ({currency, threshold_usd,
                                     -- policy_content_id}); NULL for
                                     -- per-action consents. Makes standing
                                     -- authority reproducible from persisted
                                     -- artifacts alone — coverage never
                                     -- reads the env knob.
  grant_basis TEXT,                  -- why this per-action row exists
                                     -- exact-match asymmetry fix):
                                     -- 'user_confirmation' = the user was
                                     -- shown uncovered claims and confirmed
                                     -- them; 'exact_match_derived' = the
                                     -- validation gate found an IDENTICAL
                                     -- already-consented action (same
                                     -- identity + set hash + actor) and this
                                     -- run carries its own artifact of that
                                     -- derivation, so the subject-scoped
                                     -- worker fence and its future
                                     -- backfills/reconsents can read it.
                                     -- NULL for standing consents.
  quote_json TEXT,                   -- THE quote this consent approved, as the
                                     -- dumped ConsentQuote
                                     -- (engine/runner/confirmation_context).
                                     -- Written by the unrouted 402 path, which
                                     -- is the only one that mints a
                                     -- ConsentQuote; NULL for standing
                                     -- consents and for routed/claims consents,
                                     -- whose authority is the promise-set chain.
                                     -- Dispatch reads the RATED projection back
                                     -- out of it (billed_cost, policy_id) and
                                     -- recomputes every other fact live: the
                                     -- pricing policy that produced the billed
                                     -- figure belongs to the deployment and is
                                     -- deliberately not wired into dispatch, so
                                     -- the figure the user approved has to be
                                     -- persisted rather than re-derived.
  CHECK ((standing_policy IS NULL) != (action_identity_hash IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_consents_subject
  ON consents(subject_kind, subject_id);
-- Coverage predicate lookup (§5.3).
CREATE INDEX IF NOT EXISTS idx_consents_subject_hash
  ON consents(subject_kind, subject_id, promise_set_hash);

CREATE TABLE IF NOT EXISTS promise_sets (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('run','receipt')),
  subject_id TEXT NOT NULL,
  seq INTEGER NOT NULL,              -- linear chain: head = MAX(seq)
  consent_id TEXT REFERENCES consents(id),  -- NULL = no consent event (O1)
  promises_json TEXT NOT NULL,
  promise_set_hash TEXT NOT NULL,
  -- (A coverage_envelope_json cache lived here as a per-append high-water
  -- (field, basis) fold. Deleted because its one reachable reader
  -- walked the consented chain two lines later regardless, so the cache
  -- bought nothing. Coverage is recomputed from the chain. Bundles created
  -- before deletion keep the column; nothing reads or writes it.)
  created_at TEXT NOT NULL,
  UNIQUE (subject_kind, subject_id, seq)    -- CAS: concurrent successors race on seq
);

CREATE TABLE IF NOT EXISTS routes (
  id TEXT PRIMARY KEY,
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('run','receipt')),
  subject_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  predecessor_id TEXT REFERENCES routes(id),  -- successor path (§6)
  promise_set_id TEXT NOT NULL REFERENCES promise_sets(id),
  engine TEXT NOT NULL,
  options_json TEXT NOT NULL,
  target_snapshot_json TEXT NOT NULL,  -- exact dispatch target/capability/transport/scope
  route_fact_hash TEXT NOT NULL,       -- canonical hash of the resolved fact columns
  operator TEXT NOT NULL,              -- resolved route facts (not target constants, §3.2)
  egress_class TEXT NOT NULL,
  region TEXT,
  credential_source TEXT NOT NULL,     -- PINNED at resolution (§0.3): dispatch
                                       -- uses exactly this; unavailability halts,
                                       -- never falls back — so this column is the
                                       -- dispatch fact, not an aspiration
  cost_posture TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (subject_kind, subject_id, seq),
  UNIQUE (predecessor_id, route_fact_hash)  -- successor dedupe
);

CREATE TABLE IF NOT EXISTS binding_epochs (
  id TEXT PRIMARY KEY,
  route_id TEXT NOT NULL REFERENCES routes(id),
  provenance_hash TEXT NOT NULL,     -- hash of the typed, versioned
                                     -- provenance shape (canonical JSON), the
                                     -- identity for "distinct observed provenance"
  observed_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (route_id, provenance_hash) -- lazy epoch creation races resolve here
);

CREATE TABLE IF NOT EXISTS route_violations (
  id TEXT PRIMARY KEY,
  route_id TEXT NOT NULL REFERENCES routes(id),
  promise_set_id TEXT NOT NULL REFERENCES promise_sets(id),
  promise_fingerprint TEXT NOT NULL, -- hash of the full promise row
                                     -- (field+op+value+basis+order_ref), not just field
  observed_json TEXT NOT NULL,
  -- No status column: a violation row is the record. Cleanup trimmed the
  -- vocabulary to the single value any writer produced ('recorded'); a
  -- one-valued column is a column that answers nothing, so it is gone.
  dedupe_key TEXT NOT NULL UNIQUE,   -- (promise_fingerprint, observed hash)
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_violations_route
  ON route_violations(route_id);

-- The ExecutionAttempt — one authorized dispatch of one run
-- over one row set under one verified route head.
-- It deliberately carries no mutable per-row progress or result columns:
-- ordinary completion stays owned by results/run_rows. Quoted row settlement
-- is the one narrower consumer and lives in the
-- append-only authorization child below, not on this attempt row.
-- `evaluation_json` exists because a consent pointer alone proves a consent
-- row existed, not which claims were evaluated.
CREATE TABLE IF NOT EXISTS execution_attempts (
  id TEXT PRIMARY KEY,                      -- "attempt_" + ulid
  -- Consent records outlive the data. This
  -- was NOT NULL ... ON DELETE CASCADE, so undoing a paid action and
  -- compacting deleted the run row and took the record that the user
  -- consented and was charged with it — "did I approve it, what did it cost"
  -- went unanswerable for exactly the runs a user is most likely to ask
  -- about. Nullable + SET NULL, the `receipts.run_id` shape: compaction
  -- still reclaims the run's DATA, the authorization survives it with an
  -- honest NULL rather than a dangling id. Every reader keys by `run_id=?`
  -- (a live run's attempts) or by attempt id (the receipt), so an orphan is
  -- invisible to the former and complete to the latter. NULLs are distinct
  -- to both UNIQUE(run_id, seq) and uq_execution_attempts_one_dispatching
  -- below, so orphans from many pruned runs coexist and neither fence
  -- weakens for a live run.
  run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  seq INTEGER NOT NULL,                     -- per-owner monotone
  -- Accounted previews own no run/output columns. Their running receipt is
  -- the alternative owner; both NULL means the original owner was reclaimed.
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  state TEXT NOT NULL CHECK (state IN
    ('created','admitted','dispatching','effected','halted','superseded','abandoned')),
  action_identity_hash TEXT NOT NULL,
  scope_json TEXT NOT NULL,                 -- THE rows this attempt executes
  head_route_id TEXT REFERENCES routes(id),          -- NULL for UnroutedAdmission
  head_promise_set_id TEXT REFERENCES promise_sets(id),
  admitted_by_consent_id TEXT REFERENCES consents(id),
  cost_basis_json TEXT,
  price_card_version TEXT,
  evaluation_json TEXT,                     -- the receipt's evidence, written once
  created_at TEXT NOT NULL,
  UNIQUE (run_id, seq),
  UNIQUE (receipt_id, seq),
  CHECK (run_id IS NULL OR receipt_id IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_execution_attempts_run
  ON execution_attempts(run_id, seq DESC);
-- ONE dispatch per run, ENFORCED. UNIQUE(run_id, seq) alone only stops a
-- retry from reusing a sequence number; nothing stopped two ADMITTED attempts
-- on one run from both winning their `state='admitted'` CAS and both
-- dispatching -- two workers transcribing the same rows against the same paid
-- route, the user billed twice. The claim transaction now asks `NOT EXISTS (a
-- dispatching attempt on this run)` inside its write lock and this partial
-- UNIQUE is the structural backstop, exactly as `uq_jobs_active_dedupe`
-- backstops the analogous check-then-insert race on the run queue: the
-- predicate keeps the index to at most one row per live run, and a claim that
-- reaches it anyway loses at write time with a typed refusal rather than
-- dispatching.
CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_attempts_one_dispatching
  ON execution_attempts(run_id) WHERE state='dispatching';
CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_execution_attempts_one_dispatching
  ON execution_attempts(receipt_id) WHERE state='dispatching';
-- The bounded-age detector's scan (§1.5): stale 'dispatching' rows -> abandoned.
CREATE INDEX IF NOT EXISTS idx_execution_attempts_state
  ON execution_attempts(state, created_at);

-- Consent-bound retail allocation joined to the terminal outcome produced by
-- THIS attempt.  `row_id` deliberately has no rows-table FK: authorization and
-- charge evidence outlive compaction just like execution_attempts itself.
-- `terminal_outcome` is write-once (same-value replay is allowed) and is not a
-- progress state; results remains the product's ordinary row-result authority.
CREATE TABLE IF NOT EXISTS attempt_row_authorizations (
  attempt_id TEXT NOT NULL REFERENCES execution_attempts(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  quoted_quantity TEXT NOT NULL,
  terminal_outcome TEXT CHECK (
    terminal_outcome IN ('succeeded','failed','cancelled')
  ),
  PRIMARY KEY (attempt_id, row_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_model_calls_epoch ON model_calls(epoch_id);
-- The settlement join.
CREATE INDEX IF NOT EXISTS idx_model_calls_attempt ON model_calls(attempt_id);
"""

# The run queue (jobs + worker_heartbeats) lives in its OWN database, never a
# project bundle: `<workspace>/.queue.db` locally, the hosted run-queue Postgres
# in production. Its schema is owned entirely by frisket.jobs.queue as a single
# SQLAlchemy MetaData (jobs_metadata) provisioned through the ordered version
# ledger (frisket.jobs.queue_migrations), so both backends share one definition.


class BundleSchemaMismatch(Exception):
    """A bundle's DDL is not the DDL this build creates, so it is not opened.

    Deliberately NOT a ``ValueError``: several callers wrap project opens in
    broad ``except ValueError`` handlers meaning "bad project id", and a
    stale bundle silently becoming a 404 is the mangling this refusal exists
    to prevent.
    """


def _normalized_ddl(schema: str) -> str:
    """``schema`` with ``--`` comments and whitespace runs removed.

    Comments and indentation are how this file explains itself; making them
    digest-relevant would invalidate every dev bundle on a typo fix while
    proving nothing about the DDL. No ``--`` occurs inside a string literal
    in ``SCHEMA`` (they all follow a closed quote), so the naive strip is
    exact here.
    """
    without_comments = re.sub(r"--[^\n]*", " ", schema)
    return " ".join(without_comments.split())


def schema_digest(schema: str = SCHEMA) -> str:
    """Stable fingerprint of a DDL text.

    Derived, never hand-maintained: editing ``SCHEMA`` moves this value in
    the same commit, so nobody has to remember to bump a version number and
    no bundle can be created by one DDL and opened under another. That is
    the whole fence -- there is nothing else to enumerate.
    """
    body = _normalized_ddl(schema).encode("utf-8")
    return f"frisket.schema.v1:{hashlib.sha256(body).hexdigest()[:32]}"


#: The digest every bundle this build creates is stamped with.
SCHEMA_DIGEST = schema_digest()


def require_current_schema(
    db: sqlite3.Connection, *, bundle_path: Path | None = None
) -> None:
    """Refuse a bundle whose DDL is not this build's, before anything reads it.

    A bundle from another build can be missing a column a query names, or -- far
    worse -- missing an index whose absence is silent, like
    ``uq_execution_attempts_one_dispatching``, the structural backstop
    against billing a user twice for one run. So the check is fail-closed on
    every open, and it names what the reader can actually do about it.

    It reads the STAMP, not the live DDL. That is exact as long as the stamp
    and the DDL are written together: creation and each known upgrade write
    them in one transaction. Fingerprinting ``sqlite_master`` would also catch
    a hand-edited bundle, but nothing in this codebase can produce one, so
    that check would be a mechanism without a failure to prevent.
    """
    stamped: str | None = None
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()
    except sqlite3.DatabaseError:
        # No `meta` table at all, or not a frisket database: same refusal.
        row = None
    if row is not None:
        # Positional, so a caller's plain connection (no ``sqlite3.Row``
        # factory) is checked rather than raising past the fence.
        stamped = str(row[0])
    if stamped == SCHEMA_DIGEST:
        return
    # The bundle's NAME, never its path: this message is served to an HTTP
    # client, and on the hosted tier the path spells out the data root's
    # per-org directory layout. The name is what identifies the project to
    # the person reading it anyway.
    where = f" ({Path(bundle_path).name})" if bundle_path is not None else ""
    raise BundleSchemaMismatch(
        f"this project{where} was created by a different build of frisket and "
        f"cannot be opened by this one without a supported migration. "
        f"Keep the project intact and use a compatible build or migration. "
        f"(bundle schema {stamped or 'unstamped'}; this build expects "
        f"{SCHEMA_DIGEST})"
    )

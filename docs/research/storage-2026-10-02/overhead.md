# Storage overhead audit

Audit revision: `aa5201b2fc2abe26a03f1547721ca715ba7baaf9` in the clean shared checkout `/home/dev/Development/frisket/actor-worktrees/storage-audit-next-20261002`. This audit read source and the approved Phase 2 report at fixture revision `efd49a4fccdb4218a72c96ee0aa9b4b17bda9360`. It did not open project data, run a fixture or benchmark, hydrate dependencies, or change code.

## Decision

The smallest credible storage fix is to remove two exact duplicate secondary indexes after a bounded migration proof:

1. `idx_execution_attempts_run(run_id, seq DESC)` duplicates the autoindex created by `UNIQUE(run_id, seq)`. SQLite can scan the UNIQUE index backward for `ORDER BY seq DESC`. No source uses `INDEXED BY idx_execution_attempts_run`.
2. `idx_source_items_source_dedupe(source_id, dedupe_key)` duplicates the autoindex created by `UNIQUE(source_id, dedupe_key)`. The authoritative upsert already relies on the UNIQUE constraint rather than the named index.

The Phase 2 fixture allocated one 4,096-byte page to each member of both duplicate pairs. The attempt pair each held the same 10 payload bytes; the source-item pair were both empty. The immediate fixture saving is only 8 KiB, but removing them also removes one redundant b-tree update per affected insert/update and scales with those ledgers. This is a simple migration with no data-model change.

Do not combine that cleanup with current-value indirection, receipt normalization, FTS removal, delayed feature-schema creation, or a WAL policy change. Those have different failure modes and no measured net win here.

## Measured overhead

The retained Phase 2 fixture measured:

| Item | Allocated | Payload | Observation |
|---|---:|---:|---|
| `project.db` | 1,216,512 B | 278,699 B | 926,657 B unused within live pages; freelist was zero, so ordinary VACUUM cannot remove most fixed live b-tree cost |
| Explicit index b-trees | 405,504 B | 11,305 B | 99 indexes; 63 had no payload in this small fixture |
| Table b-trees | 491,520 B | 176,567 B | 74 tables; 48 were empty |
| `sqlite_schema` | 102,400 B | 86,114 B | fixed schema text and b-tree cost |
| Search sidecar | 176,128 B | 105,015 B | includes deliberate FTS content; no WAL remained |
| Primary/search SHM while measured | 32,768 B each | n/a | ephemeral WAL coordination files, not durable payload |

The exact current schema still declares 75 tables, 99 indexes, and 26 triggers; its stored `sqlite_master` SQL is about 72 KiB. Much of the small-project slack is the one-page minimum for live schema objects. Deferring feature DDL could reduce an empty project's footprint, but it would complicate schema identity, upgrades, backup, and feature activation for a sub-megabyte fixed saving. That machinery is disproportionate unless a many-project fleet measurement shows fixed schema pages dominate real disk use.

## Index candidates

### High confidence: exact duplicates

`execution_attempts` declares `UNIQUE(run_id, seq)` and then creates `idx_execution_attempts_run` with the same keys and reverse sort declaration. Current queries use equality on `run_id`, `MAX(seq)`, and ascending or descending sequence order. SQLite b-trees are reversible, so the constraint index supplies those access paths. Preserve the partial unique dispatching indexes: they enforce a different state predicate and safety contract.

`source_items` declares `UNIQUE(source_id, dedupe_key)` and then creates an explicit index on exactly the same columns. Its point reader and upsert use the same equality pair; the constraint autoindex is the enforcement and lookup structure.

The smallest proof before implementation is one migration test from the prior schema plus public store tests that:

- inspect `PRAGMA index_list/index_xinfo` and confirm only the redundant named indexes disappear;
- preserve duplicate-insert refusal for both UNIQUE constraints;
- assert `EXPLAIN QUERY PLAN` uses the autoindexes for attempt ordering and source-item lookup; and
- compare `dbstat` bytes and bounded insert/read latency on a modest generated ledger.

### Measurement candidates, not approved drops

- `idx_source_runs_source(source_id)` is a strict prefix of `idx_source_runs_source_started(source_id, started_at DESC, id DESC)`. All observed readers filter by `source_id`; most also request newest-first order. The narrower index may still make `COUNT(*) WHERE source_id=?` cheaper, so compare count, status-filtered latest, pagination, and insert cost before dropping it.
- `idx_consents_subject(subject_kind, subject_id)` is a prefix of the subject/hash index. Subject-only chain reads are common, and the narrow index can be materially smaller at scale. Keep it until a representative consent ledger shows the composite index is equal or better.
- `idx_evidence_link_spans_span(span_id)` is a prefix of the annotation index. Citation and annotation reads are correctness-sensitive and frequent. Keep it until representative span fan-out and query plans prove redundancy.
- `idx_current_cells_column_row` repeats the `WITHOUT ROWID` primary-key order deliberately as a narrow covering index. Search invalidation forces this index by name to avoid pulling large current values and overflow pages. Preserve it.

## Oversized and repeated records

The fixture's largest `results` and `current_cells` records were about 8.4 KiB. Five document-text values appeared in durable results, the rebuildable current projection, and FTS; one reviewed value appeared in edits, current projection, and FTS. That is the already-measured 79,464 plus 15,288 repeated logical bytes.

These copies have separate contracts: results/edits preserve history, current values keep scalar and sort operations simple, and FTS provides search. The prior pure-head experiment saved space but made aggregates 1.8–6.3x and sorts 2.5–7.7x slower; the inline variant still made long-value sort 2.18x slower. Preserve all three representations. No codec or pointer redesign is justified.

Receipt rows also repeat queryable identity/status fields inside their portable JSON body. That repetition preserves a self-contained receipt while indexed columns support ordinary queries. Do not normalize it away. There is, however, one narrow runtime opportunity: `ReceiptStore.recent_metadata_page()` reads full bodies, while the provenance summary caller uses only receipt id, action kind, status, run id, and creation time. A dedicated summary projection could avoid fetching multi-kilobyte overflow bodies on that endpoint without changing storage or receipt semantics. Measure this with large valid receipt bodies and retain the full-body path used by embedding export discovery.

## WAL, checkpoint, and temporary lifetime

The fixture finished with zero-byte WALs for both databases. The primary store, search sidecar, point/vector stores, inventory DB, job queue, and plugin catalog all enable WAL, generally with SQLite's default autocheckpoint. Explicit primary checkpoints occur for export (`PASSIVE`) and compaction (`TRUNCATE`). No evidence shows durable WAL leakage.

`ProjectReadSnapshot` begins a pinned read transaction and can intentionally live across response iteration. A long reader can prevent checkpoint progress while writers continue, so WAL size can temporarily exceed the normal autocheckpoint target. Changing checkpoint mode or forcing truncation on ordinary close could add writer latency and is not justified without a reproduction.

The smallest measurement is one public-boundary streaming read held open while bounded writes commit. Record main/WAL/SHM bytes, `PRAGMA wal_checkpoint(PASSIVE)` busy/log/checkpointed counts, reader correctness, write latency, and sizes after the response-owned snapshot closes and after one later write. Run the same check for the search sidecar only if a real long-lived search reader exists. A fix is warranted only if WAL fails to become reusable after readers close or exceeds an explicit operational bound.

The Phase 2 analyzer's temporary digest DB peaked at 86,016 bytes and was purged. The later storage probes also purged their generated databases. There is no temp-file retention finding.

## Recommended order

1. Measure and, if confirmed, migrate away the two exact duplicate indexes in one small schema change.
2. Separately measure the `source_runs` prefix index; drop it only if count and latest/status queries remain acceptable.
3. Measure a narrow receipt-summary query if provenance pages are observed reading many large bodies.
4. Add the bounded pinned-reader WAL test as an operational guard, without changing checkpoint policy first.

This keeps history, evidence, current-value behavior, search, undo/redo, and receipt portability intact while targeting overhead with direct evidence.

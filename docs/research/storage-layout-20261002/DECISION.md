# Storage layout decision — measured changes and next step

The local fresh/migrated qualifications and analytical comparison have passed.
This is not a recommendation to replace SQLite. Larger-host,
out-of-RAM, 30-million-row and 100-GB testing remains explicitly deferred.

## What is being tested

Production candidate `b4e5906e97aeaa9a677a827b04a9605fad9052cc` combines three
bounded changes in draft PR #243 (issue #242): ordinary rowid cell tables,
compressed exact search text, and consolidated analytics population scans.
Values remain ordinary SQL-readable values. No pointer representation or second
production database engine is introduced.

The source authority, current-value projection, and search index serve different
contracts. `cells` retains imported values; `current_cells` resolves the current
value and its provenance; keyword search has postings plus a literal copy of
the exact indexed text for native snippets/highlights. The latter copy is an
architectural choice, not an inherent requirement of text search. Compression
preserves the existing asynchronous search boundary. Eliminating that copy would
instead require synchronous indexing against source content, or different
snippet/ranking and indexed-version handling. See `search-size.md`.

## Confirmed baseline and smaller proofs

The new 500,000-row old-layout run completed using the actual Project APIs and
the reviewed retained-bundle harness. Its closed main database is 6,330,556,416
bytes; keyword search is 3,022,413,824 bytes. The fixture includes seven columns,
3.45 million populated current cells, and two million searchable cells. These
are synthetic investigative-record-shaped data, not a representative sample of
all PDF/audio/video collections.

At 10,000 rows, native FTS content accounts for 75.5% of its file; postings account
for 16.1%. Compression reduced the synthetic file substantially, while separate
less-repetitive text measurements showed smaller savings. Exact phrase, prefix,
ranking, snippets, scoped search and edit/rebuild behavior remain tested.

The 10,000-row long-text layout proof saved about 22% with ordinary rowid tables
by reducing overflow-page slack. A narrow-cell fixture was larger with rowid:
this is not a universal advantage. The actual 500,000-row fresh and migrated
results must decide whether the production change earns its migration cost.

The small in-place migration preserved every streamed cell/provenance digest.
It temporarily used 3.49 times the original main-file size for main/WAL/SHM and
left reusable free pages. Separately exercising existing Project compaction
reclaimed those pages. Opening a project does not automatically VACUUM it.

The authentic 500k migration has now also passed. All 3,450,000 base rows and
3,450,007 current rows retained identical full-field SHA-256 digests. Foreign-key
and integrity checks passed. The open-time rewrite took 229.032 seconds; the
whole verification took about 651 seconds including before/after scans and
checkpointing. Peak bundle/work usage was 25,229,583,410 bytes, with at least
26,034,536,448 host bytes free. The checkpointed main file was 11,082,084,352
bytes before explicit compaction. Source:
`results/project-migration-500k.json`.

The separate reclaim proof has passed too: main becomes 4,934,074,368 bytes and
search becomes 1,323,393,024 bytes. Together that is 6,257,467,392 bytes, compared
with 9,352,970,240 before migration (about 33% less). This is a measured synthetic
fixture saving, not a prediction for every collection. Main compaction took
60.229 seconds, old-index reset/rebuild 288.831 seconds, and search reclamation
9.028 seconds. The first rebuild batch includes 100.987 seconds resetting the
old index; compare fresh indexing separately before attributing all rebuild time
to compression. Full cell digests, search integrity/documents, snippets, edited
marker, history and completed receipts passed. Exact searches remained around
7–8 ms and the five-result bounded query around 16.6 ms. Source:
`results/project-migration-finished-500k.json`.

Independent review of the 101-second initial keyword reset found no lease or
consistency blocker: the worker heartbeats from a separate thread/store while
the transactional reset runs, and dirty work is durable before the reset starts.
Cancellation is checked between batches, however, so this first maintenance
batch can delay cooperative cancellation/shutdown by roughly two minutes on
this fixture. Force-killing it can roll back and retry the reset. Record this
one-time upgrade cost; it does not justify a new lease/cancellation framework.

## Fresh 500k qualification

The fresh candidate has passed its full import/readback, indexing, query,
edit/undo/redo, history and reopen workload. Closed main/search files are
4,963,090,432 and 1,337,077,760 bytes respectively (6.30 GB combined, about 32.6%
less than the old fresh project). This post-edit fresh footprint is distinct
from the explicitly compacted migrated footprint above.

Matched warm-cache observations from the two complete runs:

| Operation | Old layout | Candidate |
| --- | ---: | ---: |
| 0.1% filter, median | 4,232.79 ms | 2,851.46 ms |
| 10% filter, median | 1,378.86 ms | 605.42 ms |
| Amount ascending sort, median | 2,778.62 ms | 1,308.75 ms |
| Amount descending sort, median | 2,814.30 ms | 1,295.23 ms |
| Broad grouped analytics, median | 17,360.85 ms | 4,040.69 ms |
| 10% filtered analytics, median | 9,276.48 ms | 821.63 ms |
| Ordinary edit, median | 7.15 ms | 4.06 ms |
| 1,000-cell batch action | 413.97 ms | 317.81 ms |
| Undo action / FTS catch-up | 144.36 / 688.16 ms | 125.56 / 711.28 ms |
| Redo action / FTS catch-up | 153.43 / 633.60 ms | 128.03 / 731.94 ms |
| Initial indexing | 153.83 s | 179.50 s |
| Import phase | 424.81 s | 405.17 s |

Grid timings use 20 samples, analytics 10, and ordinary edits 200. Individual
batch/undo/redo actions are single observations, not distributions. Ordinary
page medians rose by roughly 3–9 ms (68/94/127 ms to 72/104/130 ms); unique
search medians stayed around 7–8 ms, and the five-result search went from 15.58
to 17.45 ms. Initial indexing costs about 17% more. These modest foreground and
background tradeoffs must remain visible alongside the large analytical wins.
The overall mutation phase includes 1,200 fixture-value lookups, repeated
projection/provenance assertions and expected-sort recomputation beyond the
separately timed action/index calls. Those unindexed fixture-value scans are
sensitive to the physical layout. Independent review confirmed that the phase's
55.13-to-96.62-second change is not an undo/action duration; the timed public
actions include projection maintenance and improved. No additional pre-landing
performance harness is justified by these small foreground tradeoffs.

Source: `results/project-qualification-500k-candidate.json`, SHA-256
`0b751d3139ea0baeefc81c3fd210230276ec340b81451524f9bf31ff7e2da297`;
matched baseline `results/project-qualification-500k-old-layout.json`.
The runtime source is `b4e5906e97aeaa9a677a827b04a9605fad9052cc` and harness
`36f92b8f58741fce03ed8d2ba5834e97e9412182`. PR head
`48a4d147c3e6422674dd65a4354e99430022ed39` is its direct comment-only descendant;
the benchmark source remained frozen throughout execution.

## Landing gates

Correctness is a hard gate: exact value/provenance equality, integrity and foreign
keys, history/receipts, edit/undo/redo/reopen, complete search and native snippets.
CI passed on the composed candidate; the final comment-only descendant has a
fresh CI run. Independent review cleared the actual 500k results. Final CI and
landing status are recorded below when complete.

Measure fresh, migrated and explicitly compacted files separately. Do not report
free pages as already-reclaimed storage. Record one-time migration/rebuild time
and temporary disk usage separately from normal edits and searches. Investigate
material repeatable latency regressions rather than averaging them away with
unrelated wins. Advisory review thresholds are investigation triggers, not new
product SLAs or reasons to build extra benchmark infrastructure.

## Architectural comparison

The actual-source comparison includes a typed wide SQLite control, native
DuckDB, and Parquet queried through DuckDB. All use the same eight analytical
fields and exact-answer checks. Long body/title text is excluded because the
tested calculations do not read it. These are alternative projections, not
three stores the product would retain together.

Separate Frisket API measurements from direct SQL kernels. In particular, a
wide relation eliminates EAV joins; that benefit must not be credited solely to
DuckDB. Prefer the simplest option that reaches the required interactive latency
while preserving post-edit correctness. A second engine needs a demonstrable
benefit beyond denormalizing within SQLite.

Account for authority + FTS + one projection, source-to-projection build time,
old/new replacement peak, and 1/200/1,000-cell synchronization. The wide SQLite
control measures build/read/storage only; its incremental synchronization is
not implemented or measured. None of these experiments is a production adapter.

## Actual analytical comparison

All five paths returned equivalent answers on the retained fresh project.
The following are matched direct SQL kernels, not predicted application latency:

| Operation, warm median | Wide SQLite | Native DuckDB | DuckDB over Parquet |
| --- | ---: | ---: | ---: |
| 0.1% filter + total | 0.32 ms | 1.03 ms | 1.82 ms |
| Amount ascending top 50 | 0.063 ms | 9.84 ms | 45.95 ms |
| Amount descending top 50 | 0.098 ms | 8.02 ms | 36.17 ms |
| Middle 50-row read | 10.13 ms | 6.29 ms | 68.42 ms |
| Broad grouped analytics | 1,296.77 ms | 15.62 ms | 25.60 ms |
| 10% filtered analytics | 133.99 ms | 5.77 ms | 11.91 ms |

Wide SQLite uses ordinary typed fields and suitable value/sort indexes. Its
excellent filter/sort results show that the EAV layout and indexing are central
to the current latency; SQLite itself is not intrinsically slow. Native DuckDB's
large advantage over this control is in analytical scans/grouping, where it is
about 83 times faster on the broad kernel. These timings exclude the additional
application adapter, synchronization and freshness checks that production would
need. Current Frisket public-path observations are retained separately in the
JSON and `columnar.md`.

The narrow projection builds were 4.87 seconds / 59.69 MB for wide SQLite,
4.95 seconds / 8.66 MB for DuckDB, and 4.25 seconds / 2.53 MB for Parquet. These
are eight typed fields, not copies of all project content. Including authority,
search, manifest and the retained SHM, total footprints were respectively
6.360 GB, 6.309 GB and 6.303 GB; compare the unchanged 6.300 GB retained bundle.

Simulated DuckDB catch-up for 1/200/1,000 cells took 18.62/16.82/14.24 ms. These
are single warm observations of projection updates, not end-to-end action or
queue timings. Full DuckDB replacement took 5.10 seconds; old+new projection
files occupied 17.33 MB, making total replacement footprint 6.318 GB. Parquet
rewrote the entire projection in 4.25–4.57 seconds even for one changed cell;
its maximum old+new projection footprint was 5.07 MB. Wide SQLite incremental
synchronization was deliberately not implemented or measured.

The experiment completed in 167.19 seconds, using at most 325.54 MB recursive
RSS and 91.64 MB owned output, below the admitted 1 GiB limits. The retained
source's file metadata did not change, source mounts were read-only, and every
result/update assertion passed. Source: `results/actual-500k/results.json` plus
the accompanying process-monitor record; probe revision
`e2ca7fbd15d47bf739b2e58852aefa8e7c1c0bd6`.

## Recommendation and exact next steps

Land the bounded SQLite/search/analytics changes. They save about 3.05 GB on the
fresh fixture and substantially improve the slow queries without changing data
ownership or adding a production engine. Preserve the existing central writers,
transactions, receipts and current-value/provenance API.

The next implementation experiment should be a **narrow native DuckDB analytical
projection behind one existing Project Ask analytics path**, not a wholesale
database migration or a DuckLake deployment. Its broad-query gain is material
even against well-indexed typed SQLite; its additional bytes are small here.
Do not add both wide SQLite and DuckDB projections by default. Keep ordinary
edits and identity/page reads on SQLite. Prefer Parquet for export or immutable
snapshots if needed later; the measured mutable workload does not justify it as
the live analytical store.

Before widening that prototype:

1. Define the selected sheet/column schema using current typed values, explicit
   validity and stable row identities. Carry the source revision alongside the
   projection; SQLite remains authoritative. No large-text pointers are needed.
2. Use the existing mutation/dirty-work transaction boundary and job ownership
   to publish changes durably. Prove a consistent source snapshot and fresh
   post-edit answers for edit/undo/redo, visibility/schema changes and restart.
   Choose one owner for the DuckDB writer; do not rely on unrelated processes
   concurrently opening it for writes. Implement only the invalidation needed
   for this one path before generalizing it.
3. Route the existing validated analytics request into the projection, preserving
   filter, missing/invalid, numeric overflow, mean/median, grouping, totals and
   sorting semantics. Return existing row identities for evidence/results; do
   not build a second query API or UI. Expose pending freshness using the
   existing query readiness conventions rather than silently answering stale
   data.
4. Compare that actual application route against current SQLite, including
   end-to-end edit visibility, build/rebuild peaks and failure recovery. Reuse
   focused public tests and the archived fixture; no permanent benchmark
   framework is needed. This is the next engine-adoption gate, not a commitment
   that direct 16 ms kernels will become 16 ms UI responses.
5. Only after that works, consider broader filter/sort routing and more columns.
   Keep the larger-host/out-of-RAM/30M-row/100-GB gate deferred until an
   appropriate host is available. Do not extrapolate this fixture's compression,
   cardinality or cache behavior to that scale.

Whole-store columnar replacement, DuckLake, custom contentless-search snippets,
and a general storage abstraction are not justified by this investigation.
They remain possible future designs, not prerequisites for the measured fixes.

## Landing / archival status

Runtime qualification and independent review are complete. PR checks passed on `48a4d147c3e6422674dd65a4354e99430022ed39`
(CI run `37069818426`; 16 successful checks and 6 scope/event skips across all
workflows). Temporary project bundles, analytical projections and the disposable
DuckDB runtime have been removed after evidence capture. Source and JSON
reports are retained. Merge and main CI are pending at this revision.

Source/report locations and process ownership are maintained in `PLAN.md`.

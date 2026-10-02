# Storage preparation and decision checkpoint

Status: production changes merged; 300k and 500k qualification passed; main CI passed. This is the authoritative closeout for issue [#239](https://github.com/frisket-dev/frisket/issues/239); the earlier framework and initial 300k report are intermediate evidence.

The implementation keeps SQLite and ordinary inline current values. It improves the ownership of writes and removes measured waste. It does not introduce a generic ORM, a second authoritative database, text-pointer indirection, or a new checkpoint service.

## Changes

- [#240](https://github.com/frisket-dev/frisket/pull/240), merged at `33fee60a2b7dba26883e7c05dedae73b51509c3d`: maintenance refuses pending caller transactions, nested row writes preserve their caller's transaction, and retention inspection no longer commits writes as a side effect of a read.
- [#241](https://github.com/frisket-dev/frisket/pull/241), merged at `1b8cde5c843aedee3fb1702db413a91f77c9231a` from reviewed candidate `8e77c701039d314a8863f2230e3c7a2bf0aa6c69`: shared edit persistence and citation status journaling, shared batch-result publication, exact-cell undo/redo refresh, bounded deletion and receipt-summary reads, atomic removal of three redundant indexes, and smaller aggregate/clustering records. The landed production tree is identical to that candidate; the qualification runner contains the candidate as an ancestor.

The edit owner now records the operation, overlay and exact citation changes together. Undo restores only the citation statuses changed by that operation. Immutable per-item extraction citations stay available to strict historical/item readers; edited current-cell views still hide them through their value references. Other reprocessing rules are unchanged.

An initial actual-Project run found that undoing one cell rebuilt its entire 300,000-row column and caused broad search work. The combined edit/undo/redo phase took 590.73 seconds. Edit-only undo now refreshes its exact row/column pairs. Whole generated-column and schema transitions retain the broader rebuild they require. A runtime regression test fails on the old code and observes only the edited coordinates on the new code.

Clustering had a separate row-per-cluster scan. Building one surface-to-row lookup removes that quadratic work. Its full operation payload remains available during computation and crash recovery; successful publication moves authority to the existing receipt atomically and removes the duplicate staging payload. Receipt validation reads source/output values in bounded pages.

## Measured savings

| Workload | Before | After | Meaning |
| --- | ---: | ---: | --- |
| Three redundant indexes, 20k-row ledger fixture | Three extra b-trees | 868,352 fewer allocated bytes | UNIQUE enforcement, data digests and relevant access paths preserved |
| Aggregate receipt, 10k memberships | 1,198,539 B | About 150,000 B | Existing normalized membership relation remains authoritative and hash/count verified |
| Clustering operation record, 10k rows | 1,556,911 B | 59,671 B | Terminal staging duplicate removed; receipt and recovery behavior preserved |
| Clustering action, same 10k diagnostic | 7.177 s | 2.967 s | Tmpfs, zero provider calls; an action diagnostic, not disk-throughput evidence |

These reductions do not remove the copies serving current-value queries, historical values or FTS. A smaller receipt is not evidence that the whole project is smaller by the same percentage. The new receipt-summary query avoids reading large bodies but does not change their stored format.

## Actual-Project qualification

Both final workloads passed. The reviewed runner at `c4a4c25903086ef29dcd726a7e7180a866224cf9` contains the exact production composition as an ancestor and uses the real streaming import, current-value, query, analytics, search, edit, undo/redo and history APIs. It performs a full persisted readback, long-document search and snippet checks, 200 ordinary edits, a legal 1,000-cell batch, and checkpoint/reopen verification. No model or provider calls are made.

The initial 300k run was useful diagnosis but was narrower than the approved workload. Its 16m31s duration and 5.970 GB final files are baseline observations, not the final qualification result. Its 590.73-second phase included one edit and both undo/redo search catch-ups. The final workload uses a larger edit batch; those durations must not be presented as a like-for-like speedup ratio.

The host preserves 15 GiB free disk and bounds scratch, process RSS and host available memory. Corpus tiers run serially and generated bundles are removed after reporting. The 500k tier is conditional on measured headroom, including global free-space minima rather than just visible file sizes.

### Completed results

Both corpora use seven columns with mixed-length text, including 96–192 KiB
documents. Full readback matched every row and persisted/current cell, including
valid, invalid and missing numeric values. Search had no dirty scopes remaining
and matching source/index revisions. All search, mutation, history, checkpoint
and reopen assertions passed.

| Measurement | 300k | 500k |
| --- | ---: | ---: |
| Persisted and current cells, each | 2,070,000 | 3,450,000 |
| Body text bytes | 1,233,345,554 | 2,052,990,612 |
| Canonical NDJSON input bytes | 1,286,747,113 | 2,142,115,192 |
| Indexed searchable cells | 1,200,000 | 2,000,000 |
| Import phase | 193.44 s | 419.25 s |
| Initial FTS build | 77.61 s | 148.57 s |
| Keyword search, slowest measured call | 16.51 ms | 17.84 ms |
| Grid sort/filter/page, slowest measured call | 1.690 s | 4.581 s |
| Grouped/filtered analytics, slowest measured call including reopen | 6.683 s | 20.645 s |
| Ordinary single-cell edit, median action time | 4.43 ms | 5.90 ms |
| Ordinary single-cell edit, slowest action | 833.92 ms | 1.065 s |
| 1,000-cell batch edit, action / FTS index catch-up | 0.317 / 0.553 s | 0.338 / 0.612 s |
| Undo that batch, action / FTS index catch-up | 0.136 / 0.550 s | 0.202 / 0.664 s |
| Redo that batch, action / FTS index catch-up | 0.140 / 0.532 s | 0.167 / 0.762 s |
| Closed database + search + manifest, excluding shared-memory files | 5,623,607,709 B | 9,352,970,653 B |
| Sampled peak watched scratch, including temporary files | 5,668,680,637 B | 9,400,387,594 B |
| Peak process RSS | 207.02 MiB | 216.50 MiB |
| Minimum observed host free disk | 28.02 GiB | 24.50 GiB |

Timings are repeated runtime calls on a shared Linux/ext4 host with warm caches;
they are not cold-cache or concurrent-user latency guarantees. Python was 3.12.3
and SQLite reported 3.45.1. The sum of phases was 600.48 s at 300k and 1,517.01 s
at 500k, including correctness checks and repeated queries. The latter took
25m46s wall-clock. Process RSS excludes the operating system's filesystem cache
and is not the total memory cost of the workload. Resource peaks are sampled,
so brief higher peaks can be missed.

The 500k tier passed an independent admission review using observed disk minima,
memory, time and query bounds. Both runs stayed within their runtime guards,
completed exact checks and removed their owned generated bundles. No further
tier is claimed.

The roughly 9.35 GB bundle for 2.14 GB of NDJSON remains significant: the database
is 6.33 GB and search sidecar 3.02 GB. Current values, history and full-text search
serve distinct operations; this wave removes proven unnecessary copies without
pretending the remaining storage amplification is solved.

## Storage choice

Retain SQLite as the application authority for now. The measured undo and
clustering problems were application work-amplification bugs, corrected without
an engine migration. Point edits, their index updates, and selective FTS search
remain responsive in these fixtures. This does not make the unchanged design
suitable for the 30M-row/100GB target.

The next performance work has concrete targets: at 500k, the selective filter
reached 4.58 s, repeated sorted-page medians reached 2.79 s, and broad grouped analytics
had a 16.85 s median and reached 20.65 s after reopen. Passing the resource and
correctness gates is not a claim that those latencies are good interactive UX.
First inspect those exact application query plans and value/index layout. Then
compare a typed columnar candidate on the same operations, including its full
storage and synchronization costs. The retained inline-value APIs make that
experiment possible without committing to fragile text indirection now.

DuckDB with compressed columnar storage is the next analytical candidate, not a
proven replacement for the application authority. A rebuildable analytical copy
might speed scans but adds storage; it cannot be sold as a disk-saving solution
unless total authority, history, search, projection and rebuild scratch actually
shrink. A primary-store replacement would additionally need Frisket's edit,
undo, claim, atomic receipt/evidence publication, migration and bundle contracts.
No production adapter was implemented or compared here. DuckLake or another
lake format remains a future candidate, not a dependency to add at this stage.

DuckDB is designed around bulk analytical work; small transactions are not its primary target, and its concurrency model differs from a general multi-process transactional server. That supports evaluating it for a concrete analytical workload while separately preserving Frisket's publication, edit and recovery contracts. See the official [concurrency documentation](https://duckdb.org/docs/current/connect/concurrency) and [workload tuning guidance](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads).

SQLite WAL can grow while an old reader holds a snapshot. Our pinned-reader probe observed that growth and subsequent checkpoint/reuse after the reader closed; it did not justify another checkpoint subsystem. See [SQLite's WAL documentation](https://sqlite.org/wal.html).

This qualification does not establish 30-million-row or 100-GB capacity, media-blob throughput, concurrent hosted-writer capacity, desktop packaging, power-loss recovery, or a production DuckDB adapter. It exercises parsed input through the streaming store, not large source-file parsing, OCR or model execution. PDFs/audio/video remain in the existing blob store; the large synthetic workload here measures extracted text and structured values. Earlier typed-store SQLite/DuckDB experiments remain separate from actual application results.

## Evidence and validation

All production slices and their final composition received independent review. The composed focused suite passed 123 tests; all 33 changed Python files passed Ruff and formatting. Required PR checks passed before merge. All five main workflows passed at `1b8cde5c843aedee3fb1702db413a91f77c9231a`, including [the complete main CI run](https://github.com/frisket-dev/frisket/actions/runs/37046822587). Both actual-Project qualifications, independent result review, final goal-coverage review and final report review passed.

The research branch retains the opt-in runner under `scripts/storage_slice/`
and this note with the machine-readable evidence under
`docs/research/storage-2026-10-02/`. No generated multi-gigabyte database or shared
dependency tree is part of that archive. The runner has no standing production
or CI role. The earlier typed SQLite/DuckDB prototype is separate from these
actual-Project workloads and must not be placed in the same comparison table.

Report digests:

- `project-qualification-300k-final.json`: `2672166180e7b29410cc6a67f4401f4af2f7d9fd5bf5fcafeace8c657bfd4044`
- `project-qualification-500k-final.json`: `81f9563d1fc6400e073f5682d780a8b5652cbaf8be11ebb17eff737974115353`

The adjacent `project-qualification-300k.json` is the narrower diagnostic baseline,
not a second final result. Aggregate/index/cluster/WAL measurements are targeted
probes with their own smaller fixtures and limits.

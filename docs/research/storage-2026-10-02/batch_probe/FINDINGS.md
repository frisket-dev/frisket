# Whole-scope cluster findings

Source inspected and measured: `aa5201b2fc2abe26a03f1547721ca715ba7baaf9`.
The three retained runs used the real `cluster.values` fingerprint action and
real `MapRunner`, with no provider calls. Each ran in a fresh, cleared,
networkless process against a tmpfs project. Timings therefore show relative
end-to-end scaling for this fixture, not disk throughput.

Implementation checkpoint (`57c9b83fc169c5ed2438b7a2abf4216ad129892e`):
the data flow remains source snapshot -> reviewed clusters -> staged op fact ->
atomic terminal receipt -> receipt-backed entity resolution. Realistic failures
are changed membership/order from indexing, prematurely deleting recovery
staging, accepting malformed old ops, or leaving receipt/op changes split by a
rollback. Direct helper behavior plus completed, legacy, replay, and rollback
tests cover those boundaries. One local index and one marked terminal compaction
are smaller than the measured quadratic scan and duplicate payload; no schema,
generic storage abstraction, cap, or benchmark framework is warranted.

| rows | action seconds | sampled RSS delta | cluster fact | result value bytes |
|---:|---:|---:|---:|---:|
| 1,000 | 0.699 | 20,496 KiB | 132,216 B | 15,000 B |
| 10,000 | 7.177 | 40,220 KiB | 1,347,219 B | 150,000 B |
| 50,000 | 241.420 | 147,772 KiB | 6,867,219 B | 750,000 B |

The fact grows about 10.2x from 1k to 10k and 5.1x from 10k to 50k. Sampled
action RSS delta grows from about 20 MiB to 39 MiB to 144 MiB. These points do
not justify a product row cap or an extrapolation beyond 50k. They do show a
bounded but material whole-scope live set, consistent with the existing
whole-scope handler contract: `MapRunner._run_batch_recipe` owns all input rows
and the returned result mapping at once (`map_runner.py:1915-1968`), while the
typed adapter also creates immutable `Row` wrappers (`map_rows_action.py:1347-1374`,
`actions/types.py:513-533`). Clustering is inherently global, and this evidence
does not isolate generic MapRunner copies as the dominant memory cost. A new
streaming or spilling protocol is not justified by these measurements.

## The time growth has a specific quadratic scan

The 10k-to-50k row increase is 5x, but action time increases 33.6x. This is
explained by `recompute_cluster_coverage` in
`engine/executor/value_cluster.py:122-153`: for every cluster it scans every
source row. The fixture deliberately creates one independent two-surface
cluster per case-variant pair, so the scan count is approximately 50 million
at 10k rows and 1.25 billion at 50k rows.

The smallest semantics-preserving repair is local to that helper. Build one
`surface -> [row_id]` index from `row_ids` and `values_by_row`, using the exact
current `None` and `str(...).strip()` normalization, then obtain each surviving
surface's rows from that index. Keep the existing surface ordering, global
`sorted(member_row_ids)`, count calculation, and returned shape. Complexity
becomes O(rows + cluster surfaces + returned membership), with one O(rows)
index. This is proportionate because it removes the measured nested scan and
does not change clustering, review, storage, or execution APIs.

The proof should stay focused:

- retain the existing reviewed-group behavior test for exclusions, whitespace,
  nulls, canonical overrides, membership ordering, and counts;
- add a counting mapping regression with many independent clusters and assert
  source lookup grows with rows plus membership rather than clusters times rows;
- rerun the 1k/10k points only after the behavior tests pass. A new large
  benchmark harness or a timing threshold is unnecessary.

## Terminal fact ownership

`cluster_program.persist_cluster_fact` writes the full fact into
`ops.spec.value_clusters_result` while the batch is running
(`cluster_program.py:168-200`). That copy is required staging: the finalizer
uses it to check complete row coverage, publish the generation, compare actual
canonical values, and consume embedding checkpoints
(`cluster_action.py:286-377`). Crash recovery of a terminal run with a still
running receipt calls the same finalizer (`cluster_action.py:586-630`), so the
op fact must survive until terminal publication.

After publication, the receipt is the actual capability owner:

- `AdmittedClusterReceiptReader` reads groups from receipt evidence to power
  `resolve.entities` (`cluster_receipt_read.py:175-280`);
- completed idempotency replay calls `_published_clusters`, which checks the
  receipt against the applied op, source identity/hash, output association,
  output replay hash, and canonical values (`cluster_receipt_read.py:21-139`);
- the receipt lookup endpoint returns the receipt body; ordinary `ActionResult`
  omits evidence (`action_receipts.py:340-377`);
- undo and redo use `ops.undo_info`, generation pointers, and op status, not
  `value_clusters_result` (`store/op_log.py:139-212`);
- the history and work-log payloads omit op specs. The local debug endpoint can
  display a spec, but it does not consume the cluster fact as a behavior
  contract.

The only completed-state reader of the op copy is `_published_clusters` itself,
which currently requires byte-equivalent decoded facts in both owners. That
check creates the duplication; it is not an independent source of truth because
both values are mutable JSON in the same database, and the reader already
validates the receipt against the actual source and output.

The narrow cleanup is to retain the full op fact during execution and recovery,
then, in the same finalizer transaction that writes the completed receipt,
replace it with an explicit representation discriminator such as
`value_clusters_result_storage: "receipt_evidence_v1"`. `_published_clusters`
should accept a missing op fact only with that exact marker; legacy full facts
remain supported and must equal the receipt, while an unmarked missing fact
remains malformed. If receipt finalization fails, the transaction rollback also
restores staging. No table, resolver, or reference protocol is needed.

At 50k the compact-serialized fact alone is 6.87 MB and is exactly equal in the
op and receipt. Removing the terminal op copy eliminates at least that logical
payload from the 7.96 MB op spec; actual SQLite page savings need a post-change
measurement. The receipt's `clusters` remain necessary for entity resolution.
Its `canonical_values` remain useful for binding claimed groups to actual
published values and are part of the exposed receipt evidence, so changing that
shape is outside this narrow cleanup.

Focused behavior proof for the ownership cutover:

- a completed compact op replays idempotently and feeds `resolve.entities`;
- a rowless/unmarked old op still fails as stale, while a legacy full duplicate
  remains readable and a divergent legacy copy still fails;
- a failure before terminal receipt publication rolls back the discriminator
  change and leaves the full staging fact available for recovery;
- undo/redo of the cluster output and receipt lookup retain existing behavior.

This removes demonstrated terminal duplication while preserving the fact where
recovery and the downstream domain action actually need it.

## Implemented candidate

The bounded repair was implemented from
`57c9b83fc169c5ed2438b7a2abf4216ad129892e` in worktree
`storage-cluster-overhead-20261002`. RED is
`8203099676540f3a6bca8141e0f7430921058c90`; GREEN is
`1b8d65ee06a7ebe958cfa9d8411e94c5f9bef2fd`. The final reader also derives
expected canonical output from the receipt groups and actual source values, so
the pre-existing malformed-group refusal remains meaningful after removing the
second stored fact. Focused networkless validation passed 28 cluster
value/action/reader tests and six atomic semantic receipt/recovery variants;
Ruff check and format passed for every touched file. Review repair
`34b8e2ba964e4a9bda86b0cf1edbd9847d36e180` replaces the new full-column
receipt validation reads with 128-row source/output pages and builds the
surface-to-canonical map once. Its paging regression and independent rereview
are clear.

Fresh post-fix 1k and 10k diagnostics are retained in
`results-postfix-*.json`; no 50k rerun was made. At 10k, action time fell from
7.177 to 2.967 seconds and sampled RSS delta was essentially unchanged (40,220
to 39,964 KiB). The op spec fell from 1,556,911 to 59,671 bytes and the `ops`
dbstat allocation from 1,626,112 to 126,976 bytes; the receipt body was
unchanged. These are tmpfs, single-run diagnostics rather than disk-throughput
or general benchmark claims. The action probe exercises compute and terminal
compaction; bounded receipt-reader behavior is established by the focused
paging regression, not by these action timings.

# Focused columnar analytical comparison

Status: the guarded comparison against the retained actual 500k Project passed
with exact answers. Native DuckDB is feasible as a narrow broad-analytics
projection, but this research does not implement or qualify a production
adapter or synchronization protocol. SQLite remains the authority.

## Decision boundary

SQLite remains Frisket's authority for base values, generated results, manual
edits, history, receipts, and the rebuildable `current_cells` projection. The
existing FTS database remains the keyword-search sidecar. A columnar candidate
would be a third, rebuildable projection used only for broad typed filters,
sorts, and aggregates.

The candidate therefore helps only if lower analytical latency is worth another
copy and synchronization path. It is not a storage-saving design while the
6.33 GB SQLite authority and 3.02 GB FTS sidecar from the 500k qualification
remain. The accounting must show these options separately:

1. SQLite authority + FTS, the retained baseline.
2. SQLite authority + FTS + one wide SQLite analytical projection.
3. SQLite authority + FTS + one native DuckDB analytical projection.
4. SQLite authority + FTS + one Parquet analytical projection queried by
   DuckDB.
5. Replacement peak: retained authority + FTS + old projection + new
   projection + bounded spill/scratch.

Wide SQLite, native DuckDB, and Parquet are alternatives in that accounting,
not three files a product would keep together. The research runner creates all
three only to compare them from the same source.

## Architecture and proportionality checkpoint

Data flows from one production-shaped SQLite EAV `current_cells` slice into a
wide typed analytical relation. The projection carries `row_id`, `record_id`,
`category`, nullable typed `amount`, explicit `amount_quality`, `published_at`,
and `status`. It omits long body text and titles because none of the tested
analytical operations reads them; copying those values would add disk without
helping this workload. Missing amount cells stay absent in SQLite and become
`amount=NULL, amount_quality='missing'`; invalid `"not-stated"` cells become
`amount=NULL, amount_quality='invalid'`. Valid values remain integers.

The public/runtime proof is exact answer equality for the following matched
direct-engine kernels: a middle page read, a 0.1% filter with exact total, top-50
ascending and descending numeric sorts, eight-category grouped count/value
count/missing/invalid/sum/mean/median, and the same aggregate filtered to the
10% `environment` group. Each engine gets one discarded warm-up and the same
sample count. Actual Frisket API timings remain in a separate table because
they include request parsing, scope governance, projection, and response work
that these SQL kernels do not.

The realistic failures are a stale analytical copy after an edit or crash,
missing/invalid collapse, numeric coercion or median differences, a torn
Parquet replacement, extra disk during rebuild, and DuckDB writer conflicts
with Frisket's multiprocess use. The probe therefore also measures source-to-
projection builds, old+new replacement bytes, a one-cell update, a 200-cell
update, a 1,000-cell update, and a full native replacement after edits. It does
not build a watermark service, queue, recovery protocol, or application
adapter. Those would be required production work only after the narrow result
shows a worthwhile benefit.

This is proportionate because the experiment is one script, three alternative
layouts, six query shapes, exact results, and explicit size/update costs. A
generic storage interface, custom planner, mutation matrix, or production
dual-write path would be more complex than the question being tested.

## Existing 500k application evidence

The authoritative closeout is
`/home/dev/research/frisket-storage-next-20261002/DECISION.md`; the archived
machine-readable result is from qualification commit
`c4a4c25903086ef29dcd726a7e7180a866224cf9`. Its corpus had seven columns,
500,000 rows, 400,000 valid amounts, 50,000 invalid amounts, and 50,000 missing
amounts. The closed project/search/manifest total was 9,352,970,653 bytes.

| Actual Frisket runtime operation | 500k warm-cache observation |
| --- | ---: |
| 0.1% filter, median / max | 4,107.52 / 4,581.10 ms |
| Amount ascending sort, median / max | 2,757.63 / 2,974.45 ms |
| Amount descending sort, median / max | 2,744.75 / 3,017.55 ms |
| Broad grouped analytics, median / max | 16,854.46 / 17,371.62 ms |
| 10% filtered analytics, median / max | 8,991.14 / 9,099.03 ms |
| Ordinary edit action, median / max | 5.90 / 1,064.59 ms |
| 1,000-cell batch action / FTS catch-up | 337.76 / 611.93 ms |

Those values are problem and product baselines. They must not be divided by
direct DuckDB timings and presented as an engine-only speedup. The matched
direct SQLite kernels from this probe are engine-layout baselines. They include
both the production-shaped `WITHOUT ROWID` relation and an ordinary rowid
relation with the same identity paths. The sibling query investigation's actual
application plans remain necessary before a recommendation is final.

## Guarded 10k result

The canonical result is
`/home/dev/research/frisket-storage-layout-20261002/results/diagnostic-10k/results.json`
(SHA-256 `b73a7e64ea7f05e08fce2f4ab8696acfa610798948abecc99909ee3f3394afd2`).
It used Python 3.12.3, SQLite 3.45.1, DuckDB 1.5.6, and PyArrow 24.0.0.
All four layouts returned exactly equal answers before edits, after cumulative
1/200/1,000-cell edits, and after full replacement.

The successful process had outer PID 2861836 and completed in 6.80 seconds wall
time. A recursive host-side monitor summed the two `bwrap` processes and Python
grandchild every 50 ms; sampled peak RSS was 157,808 KiB against a 1,048,576 KiB
limit. The outer timeout was 300 seconds. Owned output was 6.1 MB against the
1 GiB guard. DuckDB used one thread and a 256 MB memory limit. The sandbox had no
network or credential mounts. An earlier launch used a 1 GiB virtual-address
limit and failed immediately because Arrow/DuckDB reserve more address space
than their RSS; it produced no timing result and was replaced by the actual RSS
monitor.

Median hot-cache direct-kernel times were:

| Operation | SQLite rowid | SQLite `WITHOUT ROWID` | DuckDB native | DuckDB over Parquet |
| --- | ---: | ---: | ---: | ---: |
| Middle 50-row read | 33.55 ms | 23.24 ms | 2.90 ms | 2.07 ms |
| 0.1% filter + exact total | 9.33 ms | 3.15 ms | 0.92 ms | 0.80 ms |
| Amount ascending top 50 | 19.80 ms | 14.91 ms | 2.55 ms | 2.56 ms |
| Amount descending top 50 | 19.07 ms | 16.00 ms | 2.59 ms | 2.59 ms |
| Eight-group analytics | 67.00 ms | 58.35 ms | 2.79 ms | 1.68 ms |
| 10% filtered analytics | 45.20 ms | 36.22 ms | 1.79 ms | 1.48 ms |

Each cell is five samples after a discarded warm-up. At 10k rows Parquet has one
row group, the files are cached, and the workload is single-threaded. These are
feasibility timings, not 500k predictions and not Frisket API measurements.

The projection build and update observations were:

| Operation | Native DuckDB | Parquet |
| --- | ---: | ---: |
| Fresh SQLite-source build | 186.06 ms | 86.42 ms |
| Final artifact before edits | 798,720 B | 60,499 B |
| One-cell synchronization | 19.23 ms | 83.51 ms full rewrite |
| 200-cell synchronization | 195.12 ms | 89.82 ms full rewrite |
| 1,000-cell synchronization | 1,005.24 ms | 108.23 ms full rewrite |
| Final full replacement | 115.94 ms | already rewritten above |
| Final replacement old + new bytes | 1,859,584 B | 126,499 B maximum observed |

Native updates use a temporary delta table and `UPDATE FROM`; the small Parquet
case rebuilds from SQLite through an in-memory DuckDB table. The surprising
10k result that a full rewrite beats 1,000 incremental native updates is
dominated by small-run setup and tuple insertion. It does not establish the same
crossover at 500k.

The analytical SQLite fixture contains 49,000 narrow current cells and excludes
body/title text. Its ordinary rowid database finished at 3,137,536 B and its
`WITHOUT ROWID` database at 2,035,712 B. This does not conflict with the physical
lane's 10k actual long-text result, where ordinary rowid saved 22.08% by reducing
overflow slack. It establishes that rowid is not intrinsically smaller; the
value-width distribution controls the result. Neither isolated file size may be
scaled into the actual 500k total.

## Official guidance applied

DuckDB's Parquet reader performs projection and filter pushdown and can use
row-group statistics to skip irrelevant data. Its guidance recommends studying
compression and row-group sizes, and notes that row groups control scan
parallelism. The probe uses Zstandard and a 122,880-row group, DuckDB's documented
default, while recording that a 10k diagnostic has only one group and cannot
demonstrate parallel row-group scanning:

- <https://duckdb.org/docs/stable/data/parquet/overview>
- <https://duckdb.org/docs/stable/data/parquet/tips>
- <https://duckdb.org/docs/current/guides/performance/file_formats>

The same performance guide says repeated or join-heavy work can favor a native
DuckDB table over querying Parquet and reports Parquet as slower in its own TPC-H
microbenchmark. That is why the experiment measures both physical choices
instead of assuming Parquet is the faster DuckDB option.

DuckDB recommends bulk ingestion and warns against row-at-a-time inserts above
100k rows. The runner therefore pages SQLite values into Arrow batches and
inserts those batches; only the deliberately small edit delta uses row tuples:

- <https://duckdb.org/docs/current/guides/performance/import>

DuckDB supports concurrent writers inside one process, but automatic
multi-process writing is not a primary supported mode, and many small
transactions are not its target. Parquet files are immutable for this purpose,
so edits require replacement. These constraints support a rebuildable read
projection, not replacing Frisket's application authority:

- <https://duckdb.org/docs/current/connect/concurrency>
- <https://duckdb.org/docs/current/sql/statements/update>
- <https://duckdb.org/docs/current/sql/statements/transactions>

Partitioning is intentionally absent. DuckDB warns that many small partitions
are expensive and recommends roughly 100 MB or more per partition. Eight skewed
categories at this scale do not justify a directory/file lifecycle, and category
partitioning would make cross-category analytics and edit replacement more
complex:

- <https://duckdb.org/docs/stable/data/partitioning/partitioned_writes>

## Runner and environment

The runner is
`/home/dev/Development/frisket/actor-worktrees/storage-columnar-20261002/scripts/storage_columnar_probe.py`
on base `1b8cde5c843aedee3fb1702db413a91f77c9231a`. DuckDB 1.5.6 is installed in
the lane-owned target
`/home/dev/Development/frisket/actor-artifacts/storage-columnar-20261002/python`
(59 MB). It uses canonical PyArrow 24 read-only. The unused Python 3.13 target
was verified to have no open process references and removed. Candidate execution
must use `bwrap` with network and credential paths absent and a cleared
environment.

The canonical fixture path is:

`/home/dev/research/frisket-storage-layout-20261002/results/diagnostic-10k/current-slice.sqlite3`

The canonical run used a five-minute timeout, a recursive 1 GiB RSS monitor,
and 1 GiB owned-output checks. DuckDB was limited to one thread and 256 MB. The
output-size check ran after fixture creation, fresh projection creation, every
edit rewrite, and the final replacement.

## Actual retained-Project mode and 200-row smoke

The runner now has a separate `--project` mode for the closed, retained bundle
created by the qualification workflow. It discovers the sheet and column IDs
from the actual database, derives the filter threshold, category distribution,
and missing/invalid counts from the final current values after the qualification
workflow's edit/undo/redo sequence, and refuses a nonempty WAL before opening
SQLite as immutable read-only. It neither opens the production `Project`
constructor nor writes a watermark. A small read-only facade invokes the real
optimized grid and analytics code so those public timings remain separate from
matched direct SQLite, native DuckDB, and Parquet kernels.
The prepared probe revision is
`e2ca7fbd15d47bf739b2e58852aefa8e7c1c0bd6`.

The actual-mode run also builds one wide SQLite control with the same eight
typed columns. It uses an ordinary rowid table with a unique position index,
`record_id,position,row_id`, category, and partial valid-amount ordering
indexes. The same direct SQLite query builder and answer checks run against
both EAV `current_cells` and this wide table. This distinguishes the benefit of
removing EAV joins from the benefit of changing engines. It measures only read,
build, and storage cost; SQLite-projection synchronization is not qualified.

A 200-row diagnostic using qualification harness
`36f92b8f58741fce03ed8d2ba5834e97e9412182` against production composition
`b4e5906e97aeaa9a677a827b04a9605fad9052cc` passed. The retained Project had
200 rows, 1,381 current cells, 161 valid amounts, 20 invalid amounts, and 19
missing amounts in its final state. All six query shapes returned equal answers
through the Frisket public paths, direct SQLite, native DuckDB, and DuckDB over
Parquet. The probe also verified one-row and all-161-available-valid-row
projection catch-up and replacement without changing any retained source file.
The result is
`results/actual-smoke-200/columnar/results.json`, SHA-256
`c70b7edde7fac08088faa80c25a72fed49de84a7d26e6cddf7b404e46ce52809`.
It is a code-path diagnostic and supplies no 500k performance evidence.

The retained qualifier completed in 6.23 seconds with sampled recursive peak
RSS 166,068,224 bytes and peak owned bytes 10,851,712. The columnar diagnostic
completed in 3.93 seconds with sampled recursive peak RSS 235,696,128 bytes and
peak combined owned bytes 6,830,470. Both used a five-minute outer timeout and
1 GiB RSS/scratch guards. The candidate ran in `bwrap` with no network or
credential mounts, the Project mounted read-only, DuckDB limited to one thread
and 256 MB, and both dependency locations mounted read-only. An initial
qualifier attempt exposed a raw FTS cardinality check that selected from a
decoded external-content view without registering `frisket_zlib_decode`; the
harness now counts the raw `cell_fts_docsize` shadow table. An initial columnar
attempt also showed that Arrow extraction needed immutable read-only SQLite
when the entire retained bundle is mounted read-only. Neither failed attempt
produced benchmark evidence.

## Guarded actual 500k result

The canonical result is
`results/actual-500k/results.json`, SHA-256
`01a1bf5f33f78b571c6dc50b9b98dfd423e13304b85c82104f7779f3dfbe6c50`.
It used probe `e2ca7fbd15d47bf739b2e58852aefa8e7c1c0bd6`, production source
`b4e5906e97aeaa9a677a827b04a9605fad9052cc`, Python 3.12.3, SQLite
3.45.1, DuckDB 1.5.6, and PyArrow 24.0.0. The source had 500,000 rows and
3,450,007 current cells after the qualification workflow's final edits,
undo, and redo: 400,014 valid amounts, 49,993 invalid amounts, and 49,993
missing amounts. Source sizes and mtimes were identical before and after the
probe.

All six workloads returned exactly equal answers through the Frisket public
paths, direct SQLite EAV reconstruction, the wide SQLite control, native
DuckDB, and DuckDB over Parquet. Median hot-cache times over five samples after
one discarded warm-up were:

| Operation | Frisket public | SQLite EAV direct | Wide SQLite | DuckDB native | DuckDB + Parquet |
| --- | ---: | ---: | ---: | ---: | ---: |
| Middle 50-row read | 102.89 ms | 624.54 ms | 10.13 ms | 6.29 ms | 68.42 ms |
| 0.1% filter + exact total | 2,751.06 ms | 188.67 ms | 0.32 ms | 1.03 ms | 1.82 ms |
| Amount ascending top 50 | 1,282.21 ms | 1,474.25 ms | 0.06 ms | 9.84 ms | 45.95 ms |
| Amount descending top 50 | 1,194.94 ms | 1,421.93 ms | 0.10 ms | 8.02 ms | 36.17 ms |
| Eight-group analytics | 4,245.89 ms | 3,897.88 ms | 1,296.77 ms | 15.62 ms | 25.60 ms |
| 10% filtered analytics | 989.82 ms | 663.94 ms | 133.99 ms | 5.77 ms | 11.91 ms |

These columns answer different questions. The public timings include request
validation, governed scope resolution, value projection, and response shaping.
The direct SQLite EAV kernels reconstruct all eight projected columns and are
layout controls, not the public application path. Wide SQLite, native DuckDB,
and Parquet are matched direct kernels without product integration overhead.
Consequently, public-to-direct ratios are not application speedup claims.

Fresh projection cost and final artifact size were:

| Alternative projection | Fresh build | Final bytes | Share of retained bundle |
| --- | ---: | ---: | ---: |
| Wide SQLite with four index paths | 4.87 s | 59,691,008 | 0.947% |
| Native DuckDB | 4.95 s | 8,663,040 | 0.138% |
| Parquet queried by DuckDB | 4.25 s | 2,532,999 | 0.040% |

The retained authority, FTS files, and manifest totaled 6,300,201,373 bytes.
Keeping exactly one projection produced totals of 6,359,892,381 bytes for wide
SQLite, 6,308,864,413 bytes for native DuckDB, or 6,302,734,372 bytes for
Parquet. These are alternatives. The research output held all three only for
the comparison.

Simulated projection catch-up left the retained authority untouched:

| Changed amount cells | Native DuckDB incremental | Parquet full replacement |
| --- | ---: | ---: |
| 1 | 18.62 ms | 4.36 s |
| 200 | 16.82 ms | 4.25 s |
| 1,000 | 14.24 ms | 4.57 s |

A full native DuckDB replacement after the edits took 5.10 seconds. Its retained
bundle plus old-and-new projection peak was 6,317,527,453 bytes. The maximum
Parquet replacement peak was 6,305,268,737 bytes. Wide SQLite's initial build
peak was 6,359,892,381 bytes; synchronization and old-plus-new replacement for
that control were deliberately not measured.

The secure `bwrap` run had outer PID 2910069 and completed in 167.19 seconds.
The recursive monitor sampled peak RSS at 325,537,792 bytes and peak output at
91,638,703 bytes against 1 GiB limits; minimum host `MemAvailable` was
12,595,806,208 bytes. The ten-minute timeout did not fire. The sandbox had no
network, `/home`, `/etc`, or credential mounts; source and dependencies were
read-only, and DuckDB used one thread with a 256 MB limit. Monitor evidence is
`results/actual-500k/guard.json`, SHA-256
`b45abf51801cade3fafe15312e54bc53421f7eba6b20c1f2221b010f2a0272b2`.

The wide SQLite control changes the interpretation. Eliminating EAV joins plus
ordinary indexes is sufficient for very fast indexed reads, filters, and sorts,
and materially reduces filtered analytics. It still took 1.30 seconds for the
broad grouped aggregate, while native DuckDB took 15.62 ms and Parquet took
25.60 ms on the same values. The broad-aggregation gain is therefore not only a
layout effect. Conversely, DuckDB is not the best answer for every query:
wide SQLite won the indexed filter and sort kernels.

## Decision

Keep SQLite as the Project authority and keep the compressed FTS sidecar. The
experiment does not support a primary-store migration. It establishes two
narrower conclusions:

- For indexed reads, filters, and sorts, a wide SQLite layout is sufficient and
  often faster than either DuckDB representation. Before adding another engine
  for those paths, prefer improvements within the SQLite query/storage design.
  The control is not itself ready to adopt because its synchronization cost and
  recovery behavior were not measured.
- For broad grouped analytics with a sub-100 ms target at 500k rows, layout
  alone was insufficient. Native DuckDB completed the matched grouped kernel in
  15.62 ms versus 1.30 seconds for wide SQLite, while adding 8.66 MB. Parquet
  also met that direct-kernel target at 25.60 ms and added only 2.53 MB, but each
  tested change required a 4.25--4.57 second full rewrite.

Native DuckDB is therefore the feasible candidate only when a concrete product
latency target requires broad aggregation beyond optimized SQLite. The smallest
production shape would keep one native DuckDB projection, owned and written by
one process, with SQLite `row_id` identity and explicit missing/invalid quality.
The Project store remains the source of truth and supplies a revision boundary;
the projection owner alone performs catch-up or an atomic rebuild; the API uses
it only for the closed analytical operations it can answer exactly. If the copy
is stale or unavailable, the product must expose that state or fall back to
SQLite rather than silently serving mismatched results.

That production work is not part of this experiment. It would need a revision
watermark, crash/rebuild recovery, single-writer coordination, application-level
latency measurement, and parity tests through the public API. The direct native
update observations show feasibility, not a qualified synchronization protocol.
Do not add that machinery until a product owner sets the broad-analytics latency
and staleness requirements. If the current roughly 4.25-second public grouped
response is acceptable, keep the optimized SQLite system and archive or delete
this research runner.

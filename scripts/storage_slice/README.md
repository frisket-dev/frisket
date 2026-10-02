# Representative storage slice

## Actual Project qualification

`project_benchmark.py` is a separate, opt-in qualification of Frisket's actual
`Project` schema and store. It streams 300,000 rows through
`StreamingSheetWriter`, checks exact current values including missing and
invalid typed input, builds the real FTS sidecar, searches a unique token at
the end of a long document, runs bounded sort/filter/grouped analytics, and
uses typed `cell.edit`, undo, and redo actions before checkpoint and reopen.
It does not describe the app schema as the normalized SQLite prototype below.

Run the tiny correctness smoke before a timed tier:

```sh
PYTHONPATH=src:. python -m unittest scripts.storage_slice.test_project_benchmark

PYTHONPATH=src:. python -m scripts.storage_slice.project_benchmark \
  --rows 300000 \
  --work-dir /path/to/disposable-local-filesystem \
  --output /path/to/project-qualification-300k.json
```

The 500,000-row tier is conditional on the 300,000-row result and host
headroom. The limits apply to the whole runner process and its owned work
directory: 6/8 GiB soft/hard RSS, a 4 GiB `MemAvailable` floor, a 15 GiB host
free-space reserve, and at most 18 GiB scratch (soft stop 2 GiB below the
computed hard limit). SQLite temp files are directed into that owned scratch;
the sampler interrupts active sort and analytics statements when a sampled
limit trips. Sampling occurs every 250 ms and between import/index/readback
batches, so brief peaks can be missed. The JSON records logical input/body
bytes separately from physical bundle files. The temporary bundle is removed
after the JSON result is assembled. The runner accepts 200–500,000 rows.

Generated-result generations and canonical evidence links are deliberately a
small real-schema fixture gate rather than repeated for every corpus row. Run:

```sh
PYTHONPATH=src:. pytest -q \
  tests/engine/test_grounded_map_extract_executor.py::test_grounded_map_extract_writes_canonical_evidence_and_replays \
  tests/engine/test_result_generation_store.py::test_descriptor_replacement_publishes_values_and_descriptor_at_seal_and_undo
```

Those tests exercise actual generation/evidence history; they do not measure
large-corpus AI execution. The runner has no network or model call and makes no
claim about 30 million rows, 100 GB, recovery from power loss, multi-process
writers, or macOS/Windows packaging.

The shared generator, sampler, prototype store, and their tests were restored
explicitly with `git restore --source=e66f6299f678a9fb2fa646d0e9d64cd6736750c8`
onto base `aa5201b2fc2abe26a03f1547721ca715ba7baaf9`; no prior production tree was
merged. The actual-Project runner and its smoke test were added on that base.

After two review rounds on correctness and resource handling, the
proportionality checkpoint kept the same architecture: one streamed import,
one paged full readback, and one sampler that interrupts active SQL. The only
second-round repair adapts Frisket's existing `AnalyticsCancelled` exception
to the runner's structured resource-stop result; no additional monitor or
test framework was introduced.

An opt-in experiment comparing **typed SQLite and native DuckDB**, using the
same logical document, extraction, review, and citation operations. Nothing in
the application imports this package; existing Frisket projects are untouched.

The question is whether one database can keep project facts consistent and
interactive while handling bulk work. This is not an ORM, project-format
migration, production storage adapter, or result from the actual-Project
qualification above.

## What runs

1. Import six repository-owned fictional lawsuit PDFs and their known text.
2. Publish fixed numeric extraction results and quotes from those documents.
3. Review/edit a value; preserve its original source and mark old evidence stale.
4. Import synthetic document rows, then publish extraction results in batches.
5. Query paginated rows, category filters, sorted amounts, and grouped counts,
   sums, arithmetic means, and medians in SQL.
6. Run another import alongside aggregate queries and explicit review edits.
7. Export a consistent logical snapshot plus assets, restore it, and verify
   values, counts, aggregates, and old citation context.

The extraction results are deterministic fixtures, **not cached model responses**.
There is no OCR/model call, UI automation, or claim about extraction quality.
The small PDFs exercise asset references; synthetic load rows contain text but
no PDF/audio/video bytes.

## Boundaries

`fixtures.py` provides input documents/results. `store.py` owns the database
connections and transaction boundaries. `test_slice.py` exercises public
operations against actual database files. `benchmark.py` measures those same
operations, running each engine in a fresh process.

The prototype has one process owning each project and one serialized writer.
Readers use separate thread-local connections. Imports stage bounded batches
and publish with SQL; existing data stays visible during staging. DuckDB uses
bounded Arrow inserts; SQLite uses parameterized SQL batches. Expensive model
work would happen outside these transactions.

Typed document fields, immutable source text versions, generated result
versions, manual overrides, review decisions, citations, and operation receipts
have separate relations. Results, current pointers, citations, and the operation
receipt commit together. A receipt here describes **one slice operation**, not
an entire Frisket action run or background agent job.

Amounts use signed 64-bit integer cents; missing and invalid imported values
remain distinguishable. Manual edits and explicit clearing survive subsequent
generated results. Reviews refer to the version actually reviewed. Citations
refer to frozen text versions and character ranges, using Frisket's existing
quote alignment helper, including whitespace differences and repeated matches.

## Running

Use a disposable checkout with Frisket's normal development dependencies. The
experiment additionally needs `duckdb==1.5.6`; the recorded run used Frisket's
existing PyArrow dependency at `24.0.0`. DuckDB is intentionally not added to
Frisket's runtime dependencies or lockfile.
With those packages available to your interpreter, from the repository root:

```sh
PYTHONPATH=src:. python -m unittest scripts.storage_slice.test_slice

PYTHONPATH=src:. python -m scripts.storage_slice.benchmark \
  --rows 20000 --ui-operations 100 \
  --work-dir /path/to/disposable-storage-slice-data \
  --output /path/to/storage-slice-results.json

PYTHONPATH=src:. python -m scripts.storage_slice.benchmark \
  --profile stress --rows 100000 --ui-operations 100 \
  --work-dir /path/to/disposable-storage-slice-data \
  --output /path/to/storage-stress-results.json
```

Choose a real local filesystem for `--work-dir`, not a RAM-backed temporary
directory. The runner removes its generated databases and exports; it retains
the requested JSON report and one report per engine. It runs SQLite first,
then DuckDB, with a five-minute timeout per engine. The bounded runner accepts
1–100,000 synthetic rows and 1–1,000 UI operations. It is not a large-scale
load generator. Benchmarking requires Unix's `resource` module; reported RSS
units are qualified for Linux only.

The opt-in `stress` profile streams deterministic, skewed-category text with a
mixed length distribution, retains 1.25 extraction versions per imported row,
and times the existing aggregate plus a selective history join. It reuses the
same bounded UI/read/edit overlap probe, records per-phase RSS and project,
temporary, and checkpoint storage, and skips the large logical export/restore.
It accepts at most 500,000 rows, stops an engine at the 8 GiB soft scratch
budget (with 10 GiB as the sampled hard ceiling), and gives each engine 30
minutes. The intended sequence is one 100,000-row canary and, only with enough
resource headroom, one 500,000-row run per engine. The sampler records host load
but cannot isolate or attribute competing work on a shared machine.

The recorded run used existing dependencies mounted read-only, a cleared
environment, and denied network access. No API keys or external services are
needed. Do not install into or mutate another worker's shared environment.

## Correctness checks

The tests cover append preservation, idempotent retries, conflicting retry
payloads, concurrent identical imports, actual process exit during staged
import, transaction rollback, stale-edit rejection, competing edits, review
states, explicit null overrides, numeric validation, multiline/repeated quotes,
stale source versions, bounded sorted pages, cross-engine aggregate equality,
and cross-engine export/restore. Modified export data or assets are rejected.

The load runner records actual import overlap instead of assuming concurrent
threads necessarily overlap. It reports p95/p99 only with at least 100 samples.
Ten warm probes and smaller overlap subsets report median, mean, and maximum.

## Deliberate limits

- Fixed document schema and one numeric derived field; no dynamic schema,
  arbitrary plugin values, user-defined cross-sheet join workload, or full
  action/run integration. Internal queries do join the version/head relations.
- Text citation context with PDF asset preservation; no PDF-box/transcript-time
  rendering or search/index implementation.
- Primary keys plus operation-level validation, not the full production
  foreign-key/constraint graph.
- Single-process ownership, not multi-process writers, hosted failover, or an
  exclusive-owner routing implementation.
- Tests cover process interruption and rollback, not power-loss durability.
  Asset writes do not implement a full fsync/recovery protocol. Unreferenced
  staged assets are not garbage-collected.
- Snapshot/export format is `storage-slice-logical-v1`, not the Frisket project
  format. Export refuses unfinished staging. Restore validates streamed table
  and asset checksums before publishing a new target directory.
- Stress text has deterministic vocabulary and length diversity, not the
  semantic entropy or format mix of a real investigative corpus. Its retained
  history and selective join exercise a fixed shape rather than arbitrary
  user-defined analysis.
- The stress profile samples RSS and filesystem size every 250 ms, so short
  spikes can fall between samples. It has no separate index-build workload.
- Small, warm Linux measurements do not qualify 30 million rows, 100 GB,
  macOS/Windows packaging, or production recovery.

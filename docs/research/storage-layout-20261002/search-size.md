# Search-sidecar size investigation

Date: 2026-10-02
Production base: `1b8cde5c843aedee3fb1702db413a91f77c9231a`
Probe: `scripts/storage_size_probe.py` in worktree
`/home/dev/Development/frisket/actor-worktrees/storage-search-size-20261002`
Machine-readable result:
`/home/dev/Development/frisket/actor-artifacts/search-size-20261002/search-size-probe.json`

## Conclusion

The 3.02 GB search sidecar is not a 3.02 GB posting list. Frisket uses ordinary,
contentful FTS5. It stores one complete copy of every searchable current value,
the positional token index needed for phrases and native snippets, and a second
identity/hash table. Four columns are searchable in the qualification corpus:
`title`, `body`, `category`, and `status`. The 2.05 GB figure is only `body`.

The 10,000-row production-boundary fixture reproduces the observed ratio almost
exactly: 40.740 MB of bodies, 41.075 MB of total searchable text, and a 59.851 MB
search sidecar (1.469x body bytes). Its physical layout is:

| Object | Allocated bytes | Share of sidecar | Purpose |
| --- | ---: | ---: | --- |
| `cell_fts_content` | 45,195,264 | 75.5% | Full searchable values, including UNINDEXED metadata and b-tree overhead |
| `cell_fts_data` | 9,637,888 | 16.1% | Token/posting/position data |
| `search_cells` | 3,227,648 | 5.4% | Cell identity and 64-character SHA-256 text |
| two `search_cells` indexes | 1,167,360 | 2.0% | Uniqueness and sheet/column/row access |
| `cell_fts_docsize` | 557,056 | 0.9% | Per-document token counts used by BM25 |
| FTS index/config and state | 65,536 | 0.1% | Segment directory/configuration and revision state |

This explains the 500,000-row result without treating it as a mystery or as
pure index overhead. Applying the fixture shares to 3.02 GB is only an estimate,
but it suggests roughly 2.28 GB of content shadow table and roughly 0.49 GB of
token data. The exact 500,000-row sidecar was removed after qualification, so
these are not direct `dbstat` measurements of that file.

The implemented design uses compressed, sidecar-owned external content: it
keeps full-detail FTS5 postings, stores each rebuildable text value as a zlib
blob in the same sidecar, exposes it through a connection-registered decode
function and SQL view, and points FTS5 `content=` at that view. This keeps native
phrase, prefix, NEAR, BM25, `snippet()` and `highlight()` behavior and does not
add an authoritative value pointer to `project.db`.

Search needs access to the exact indexed text for native snippets and for
revision-consistent citations while indexing runs asynchronously. That is a
requirement for text access, not a requirement for a second uncompressed copy.
The compressed sidecar projection supplies that text. Contentless FTS is
smaller, but it removes native snippet/highlight content and would create a
cross-database snapshot problem for Ask citations.

## Could Frisket eliminate the stored text projection?

Yes, but not within the current asynchronous sidecar contract without replacing
some other behavior. The fundamental requirement is access to text that exactly
matches the indexed tokens when Frisket deletes or updates an entry and when it
produces a snippet or rerank excerpt. A second uncompressed copy is not a
fundamental requirement.

SQLite external content does not bridge Frisket's two databases: FTS5 requires
the named content table or view to be in the same SQLite database, and it reads
that object whenever column values are needed. SQLite also requires the old
indexed text to still be available when an external-content row is deleted or
updated; supplying different text can leave stale index entries. See the
official [external-content contract](https://www.sqlite.org/fts5.html#external_content_tables)
and [update/delete ordering](https://www.sqlite.org/fts5.html#updates_and_deletes_on_contentless_tables).
Frisket currently changes `project.db` first and reconciles the sidecar later,
so a sidecar view cannot point directly at mutable `current_cells`. By the time
the queued indexer runs, the old indexed value may already be gone.

The feasible choices are:

| Design | Removes search-text projection? | Main tradeoff |
| --- | --- | --- |
| Compressed external content in the sidecar | No, but removes the uncompressed copy | Preserves the asynchronous rebuild boundary, exact indexed snapshots, native snippets/highlights, and the current 64-token rerank excerpt with bounded decompression. This is the implemented choice. |
| Move FTS and its external-content view into `project.db` | Yes | To remain consistent without retaining old text, FTS maintenance must occur synchronously in the same source transaction as edits, imports, undo/redo, and deletes. This couples tokenization and index writes to foreground source mutations, grows and locks the authoritative database during rebuild/repair, and gives up the disposable independently rebuilt sidecar boundary. |
| Use contentless-delete FTS in the sidecar | Yes | Ranking and full-detail phrase/NEAR matching can remain, and modern SQLite can update/delete with tombstones, but reading indexed columns returns `NULL`. Native `snippet()` and `highlight()` therefore cannot supply excerpts. Frisket would need to fetch and hash-check current source text after ranking, recreate match-centered snippets/rerank excerpts with equivalent tokenizer semantics, and define what partial asynchronous results do when the current value no longer matches the indexed revision. |

SQLite defines `snippet()` as selecting text from the matched column and
`highlight()` as returning a marked copy of that column; a contentless table
returns `NULL` for indexed columns. See the official
[auxiliary-function documentation](https://www.sqlite.org/fts5.html#built_in_auxiliary_functions)
and [contentless-table behavior](https://www.sqlite.org/fts5.html#contentless_tables).
Frisket calls `snippet()` twice for each bounded project result—once for the UI
excerpt and once for the 64-token reranker input—and validates the stored source
hash against a pinned current-value snapshot before exposing an interactive
hit. The compressed projection is therefore an architectural convenience with
meaningful consistency and feature benefits, not proof that literal duplication
is inherently required.

## Architecture and proportionality checkpoint

The intended data flow remains:

1. `project.db` owns current values, history, revisions, and dirty scopes.
2. The queued indexer reads a pinned project snapshot in bounded pages.
3. One rebuildable sidecar transaction publishes cell identity, compressed
   rebuildable text, and full-detail FTS postings together.
4. Search pins the sidecar and validates its complete revision against a pinned
   project snapshot before exposing results.
5. FTS5 ranks and locates matches; its external-content view inflates only the
   bounded result rows for native snippets/highlights.

On a content-version mismatch, existing maintenance opens an immediate sidecar
transaction, resets only keyword objects, preserves `cell_vec`, and queues the
existing global dirty scope for a bounded rebuild. It deletes an old FTS row
before its external content and inserts external content before its FTS row.
After the rebuilt revision is complete, the queued worker reclaims freed pages
on a fresh connection. Busy or full reclamation leaves the completed index
readable and the marker retryable; no foreground search or watch drain runs
`VACUUM`.

Realistic failures are missing codec registration, corrupt compressed content,
an interrupted reset or rebuild, an edit arriving during a batch, incorrect
external-content operation order, and a busy or full reclaim. The sidecar is
disposable and the source database remains authoritative.

The simplest proofs are existing public/runtime search calls plus physical
`dbstat`/closed-file measurements. A custom tokenizer, parser, source-shape
test, or new checkpoint service would be disproportionate. Dropping positional
detail is also disproportionate because it achieves size by weakening user
query behavior. A queued one-time `VACUUM` is justified because the in-place
schema replacement alone does not return freed pages to the filesystem.

## Measured fixture

The retained fixture is
`/home/dev/Development/frisket/actor-artifacts/search-size-20261002/fixture-10000.frisket`.
It uses the archived qualification generator read-only and imports 10,000 rows
through `StreamingSheetWriter`, then drains production `index_batch`. It has
70,000 persisted/current cells, 40,000 searchable cells, mixed-length bodies,
and a 96–192 KiB body every 200th row. The run was network-isolated with the
canonical environment mounted read-only, a 1 GiB address-space limit, and a
500 MiB scratch limit. Retained scratch is 186,139,041 bytes.

The production boundary found a token at the end of row 199 through
`search_project_page`, `search_sheet`, and exact-cell `search_cells_scoped`, and
returned a bounded match-centered snippet. The candidate comparison also used a
quoted phrase, a prefix query, the late-document token, native `snippet()`,
native `highlight()`, and BM25 order.

### Candidate layouts

`dbstat` live-btree savings are more meaningful than candidate file length,
because candidate writes left 247 free pages. Existing SQLite files likewise do
not shrink merely because objects are dropped.

| Layout | File bytes | Live b-tree bytes | Live saving | Behavior observed |
| --- | ---: | ---: | ---: | --- |
| Production contentful | 59,850,752 | 59,850,752 | — | Production reference |
| One-column contentful + binary digest | 58,867,712 | 57,856,000 | 3.3% | Same rows, ranks, snippets, highlights for phrase/prefix/late-token probes |
| zlib external content + binary digest | 25,735,168 | 24,723,456 | 58.7% | Same rows, ranks, snippets, highlights; reopen worked after codec registration |
| Full-detail contentless-delete + binary digest | 14,270,464 | 13,258,752 | 77.8% | Same rows/ranks; native snippet and highlight content are `NULL` |

The compact contentful candidate removes the four UNINDEXED FTS columns and
stores the source digest as 32-byte BLOB instead of 64 hex characters. Its
small saving is real and low-risk, but changing every search query and sidecar
schema for about 3% is weak on its own.

The compressed fixture shrank 41,074,819 logical content bytes to 10,035,763
bytes. That 24.4% fraction is unusually favorable because the stress fixture
uses repeated vocabulary. Two sensitivity probes bound expectations rather
than pretending it generalizes:

| Per-value zlib level 6 sample | Logical bytes | Compressed bytes | Fraction retained |
| --- | ---: | ---: | ---: |
| Six repository lawsuit texts | 8,534 | 4,701 | 55.1% |
| Deterministic less-repetitive text | 983,040 | 803,952 | 81.8% |

If actual content lands between those small probes, the 3.02 GB sidecar might
save roughly 0.5–1.1 GB, not the synthetic fixture's 58.7%. That is a directional
range, not a qualification result. A representative real extracted-text sample
is required before promising a product number.

FTS `optimize` followed by `VACUUM` changed 59,850,752 bytes to 59,576,320 bytes,
only 0.46%. `optimize` alone temporarily grew the file to 64,028,672 bytes and
left 1,051 free pages. It is not a useful answer to the 3.02 GB sidecar.

### Bounded runtime diagnostic

A warm-cache, shared-host diagnostic compared the old contentful schema with
the production compressed schema on the same 10,000-row fixture. It used 15
samples after three warmups, disabled network access, capped address space at
1 GiB and scratch at 500 MiB, and checked exact result parity. These numbers
detect regressions; they are not product latency guarantees.

| Query shape | Old median | Compressed median | Decode calls | Exact parity |
| --- | ---: | ---: | ---: | --- |
| Project-wide common term, 50 hits with two snippets | 28.30 ms | 28.13 ms | 50 | Yes |
| Sheet common term, 200 ranked candidates | 26.88 ms | 26.36 ms | 0 | Yes |
| Exact row/column scope, one hit with snippet | 70.37 ms | 39.85 ms | 1 | Yes |

The first candidate query exposed a real problem: filtering exact scope through
UNINDEXED FTS columns inflated 20,000 values and took 228.28 ms. The final SQL
joins scope predicates to `search_cells`, while the external-content view is
used only for bounded native snippets. The rerun reduced that shape to one
decode. Migration took 3.75 seconds; peak RSS was 93,036 KiB and peak scratch
was 186,268,459 bytes. Before queued reclamation, the migrated file remained
59,854,848 bytes although live b-trees occupied 26,484,736 bytes, demonstrating
why the one-time physical reclaim is part of migration.

## SQLite constraints

SQLite documents that normal FTS5 stores a private content copy and that an
external-content table or view must be in the same database. External content
retains all FTS functionality, but the application must keep it consistent with
the index. On update/delete, the old FTS entry must be removed while the old
content is still available. See the official
[FTS5 external-content documentation](https://www.sqlite.org/fts5.html#external_content_tables).

The same documentation says `detail=column` and `detail=none` remove phrase and
NEAR support. Frisket exposes raw FTS query semantics and depends on
match-centered snippets, so those modes are rejected for this change. Removing
`columnsize` saves less than 1% here and makes BM25 obtain token counts by reading
and tokenizing content on demand; it is also rejected. See
[FTS5 detail](https://www.sqlite.org/fts5.html#the_detail_option) and
[columnsize](https://www.sqlite.org/fts5.html#the_columnsize_option).

SQLite also states that `optimize` merges all FTS b-trees into their minimum
index form but may take a long time. The probe confirms that merging segments is
not where Frisket's bulk space goes. See
[FTS5 optimize](https://www.sqlite.org/fts5.html#the_optimize_command).

The measurements use SQLite's `dbstat`, which accounts for b-tree and overflow
pages but omits freelist, pointer-map, and lock pages. See the official
[dbstat documentation](https://www.sqlite.org/dbstat.html).

## Implementation proofs

- **Native query behavior:** migration tests compare row order, rank, and native
  snippets for quoted phrases, prefixes, and NEAR; the focused existing search
  suite covers malformed-query retry, sheet de-duplication, exact cell scopes,
  source anchors, limits, long values, and revision checks. Full FTS detail and
  `columnsize` remain enabled.
- **Connection and write order:** every sidecar reader/writer registers the
  deterministic decoder. Each edit deletes FTS before old external content,
  then inserts external content before FTS in the same transaction. Independent
  review also ran FTS5 `integrity-check` after edit/reindex.
- **Migration and cancellation:** a populated version-3 sidecar resets keyword
  objects in place, preserves `cell_vec`, and rebuilds through the existing
  bounded dirty-scope indexer. Cancellation during reset rolls back the keyword
  schema change; subsequent maintenance resumes successfully.
- **Physical reclaim:** the rebuilt index is searchable while `reclaim_pending`
  remains. Reclaim checks the freelist, runs `VACUUM` on a fresh queued-worker
  connection only when needed, truncates the WAL, and then clears the marker.
  A live writer makes reclaim defer without hiding search; the real worker
  lifecycle requeues the same job and a later attempt clears the marker. The
  migration regression asserts the closed file is smaller after reclaim.
- **Measured read cost:** common project and sheet searches match the old schema
  without measurable median regression in the bounded diagnostic. Metadata
  joins prevent unbounded decompression for scope filtering; only result rows
  requiring snippets are inflated.
- **Validation:** 63 focused search, migration, job, and Ask/source tests passed
  in a network-isolated sandbox using the canonical read-only environment.
  Independent review passed another 47 focused tests, lint, formatting, diff
  checks, FTS integrity, query-plan inspection, and live-reader reclaim retry.

The retained 10,000-row fixture proves proportional storage and runtime behavior.
A full 500,000-row composition remains the final qualification for the aggregate
bundle-size claim; the implementation does not promise that extrapolated size.

## Recommendation

Merge the focused compressed-external-content implementation after the final
500,000-row composition check. Keep full FTS detail and `columnsize`, the
existing source hash representation, and `search_cells` metadata joins. Treat
compressed content as a rebuildable projection, never as `project.db`
authority. A binary source digest would add migration surface for about a 3%
fixture saving and is intentionally outside this change.

Do not pursue contentless FTS in the same change. It saves more bytes but would
require a new snippet/highlight implementation or cross-database reads after
ranking. That expands the failure surface precisely around long-document Ask
citations and revision consistency. Do not ship `detail=column`/`none`, routine
FTS `optimize`, or `columnsize=0` as storage fixes.

# Physical storage layout audit

Audited source: Frisket `1b8cde5c843aedee3fb1702db413a91f77c9231a`. This is a layout account, not a proposal to replace the direct-value model or the FTS sidecar.

## Observed 500k qualification footprint

The final actual-Project qualification persisted 500,000 rows and 3,450,000 base cells (seven columns; 50,000 missing `amount` values have no base-cell row). It reported 2,052,990,612 UTF-8 body bytes and 2,142,115,192 canonical NDJSON bytes. Closed steady files were:

| File | Bytes | Relation to body bytes |
| --- | ---: | ---: |
| `project.db` | 6,330,556,416 | 3.08x |
| `project.search.db` | 3,022,413,824 | 1.47x |
| Total persisted databases | 9,352,970,240 | 4.56x |

The source bundle was deliberately removed after reporting. Therefore these file totals cannot themselves attribute exact bytes to individual SQLite objects. The bounded, read-only fixture below supplies object-level accounting for the same production cell schema and mixed text shape. Its fresh cell-only variants are not complete or byte-identical copies of the full 500k project database, and their figures are not a linear extrapolation to 500k.

## Main-store copy account

| Object/layout | Qualification cardinality | Stores | Purpose and physical meaning |
| --- | ---: | --- | --- |
| `cells` `WITHOUT ROWID`, PK `(row_id,column_id)` | 3,450,000 | Base JSON value plus base-write producer | The authoritative imported/base value. `idx_cells_column(column_id,row_id)` is a narrow alternate traversal for column refresh/read work; it does not repeat `value`. |
| `current_cells` `WITHOUT ROWID`, PK `(column_id,row_id)` | 3,450,000 | Current JSON value, origin kind/op/run or producer, validity | Rebuildable current-value projection used by the normal grid/value reader. On this source-only baseline every populated base value is copied a second time, with per-cell provenance and validity. This is the main structural payload amplification in `project.db`; it is not extra history. |
| `idx_current_cells_row(row_id)` | key-only secondary b-tree | Row-first lookup path | Measure before considering removal; the final report supplies no per-index bytes or plans. |
| `idx_current_cells_column_row(column_id,row_id)` | key-only secondary b-tree | Column-first identity only | The search-index scan explicitly forces it to discover IDs without pulling large primary-record values/overflow pages. Though its key order matches the `WITHOUT ROWID` primary key, it is intentionally narrower. Do not count it as a third cell-value copy or drop it as cosmetic duplication. |
| `ops`, `edits`, `runs`, `results`, `cell_result_heads` | sparse for this workload | Operation journal, edit overlays, immutable generated deltas, current generated heads | Undo/redo and generated-result history. The import uses one operation and compact producer record, not a base-value history copy. Edited values grow with changes; `current_cells` reflects the current winner. |
| `project.search.db`: `cell_fts`, `search_cells`, FTS internals | 2,000,000 indexed searchable cells | Searchable current text plus inverted-index and identity/state structures | A distinct rebuildable keyword-search sidecar. Its 3.022 GB is separate from the main store and needs FTS-specific accounting. |

No table bytes are summed with their indexes here. In SQLite `dbstat`, those are distinct allocations, while overflow and payload pages already belong to their owning object. Long inline JSON values use overflow pages within that one object.

## Data flow and tradeoff

Base `cells` is row-first for authored/imported records. The visible projection is column-first so the ordinary value reader can join requested rows to `current_cells` by a direct composite key. It does not reconstruct a page by scanning edit or run history. Projection refresh resolves source, published result head, and applied edit in the writer transaction; the normal read then reads the stored current value and provenance.

The base/current duplication is therefore a deliberate trade: one authoritative base representation plus one fully materialized, rebuildable current representation. It is not pointer indirection. The final qualification exercised reopening, full persisted readback, 200 edits, a 1,000-cell edit/undo/redo cycle, and search catch-up against this design.

## Narrow options

1. **Evaluate an ordinary rowid physical representation before redesigning the data model.** The isolated probe below shows a material space reduction while retaining every tested value/reference field, uniqueness invariant, and direct-read plan. It is an experiment, not a migration decision: it needs the real 500k import, projection/search, reopen, edit/undo/redo and migration proofs.

2. **If disk remains a product priority after that, evaluate a sparse override projection.** Keep base values in `cells`; store current values only for generated-result heads and manual edits, with a single reader selecting an override or base value. This can remove the full base-value duplicate for source-only sheets without text pointers: a selected value remains directly stored in one relation. It changes current reads, refresh, search feeding, migrations, and provenance consumers, so it requires an end-to-end 500k correctness/performance comparison before adoption.

3. **Do not pivot to per-sheet wide rows or a new columnar engine on current evidence.** The existing keys already provide row-first and column-first locality. Wide rows add user-column DDL/migration and still need current/history semantics; a columnar engine may improve scans but does not remove required direct current values. Neither is a narrow response to the measured duplicate.

The first option is now the proportionate next experiment. It isolates a physical SQLite representation change from the deliberate current-value duplicate; it should be measured before any more invasive projection redesign.

## Bounded fixture accounting

The shared 10,000-row actual streaming-import fixture has 40,739,791 body bytes and 40,000 searchable cells. Its main database is 126,287,872 B (4 KiB pages, 30,832 pages, 15 freelist pages); the search database is 59,850,752 B (14,612 pages, no freelist). `dbstat` allocations are distinct by object, so table and index bytes below are not double-counted.

| Database/object | Allocation | Payload | What it establishes |
| --- | ---: | ---: | --- |
| Main `current_cells` | 62,304,256 B | 43,614,033 B | 49.33% of `project.db`; a full current-value materialization. |
| Main `cells` | 60,313,600 B | 42,232,033 B | 47.76% of `project.db`; the authoritative base values. |
| Both value tables | 122,617,856 B | 85,846,066 B | 97.09% of `project.db`. The projection costs 1,990,656 allocated B more than the base copy (3.30%), due to current provenance/validity and layout; it is not a cheap key-only cache. |
| `idx_cells_column` | 696,320 B | 403,116 B | Narrow identity traversal, 0.55% of `project.db`. |
| `idx_current_cells_column_row` | 688,128 B | 403,116 B | Narrow column scan used by the search feeder, 0.54%. |
| `idx_current_cells_row` | 675,840 B | 403,116 B | Narrow row-axis traversal, 0.54%. |
| All three cell/current secondary indexes | 2,060,288 B | 1,209,348 B | 1.63% of `project.db`; none approaches the duplicated value-table cost. |
| Search `cell_fts_content` | 45,195,264 B | 41,714,811 B | 75.51% of `project.search.db`; FTS retains searchable content. |
| Search `cell_fts_data` | 9,637,888 B | 9,456,029 B | 16.10%; FTS term/index structures. |
| Search `search_cells` | 3,227,648 B | 2,959,488 B | 5.39%; identity and source-hash relation. |

The fixture shows the main-store conclusion without conflating index metadata with data: `cells` and `current_cells` consume almost all main-file space and are near equal in size. Its FTS figures separately show that most sidecar space is retained content, followed by the inverted data structure. This report records that fact but does not recommend an FTS change.

## `WITHOUT ROWID` page-packing probe

SQLite's official guidance says `WITHOUT ROWID` favors composite keys that do not carry large strings or BLOBs, and that ordinary rowid tables tend to work faster for large rows because their content stays in leaf pages. The same documentation gives a rough 4 KiB-page guideline of less than about 200 bytes per row. Frisket's mixed corpus includes long inline JSON text, so this is a realistic caveat rather than a theoretical concern. See SQLite's [WITHOUT ROWID guidance](https://www.sqlite.org/withoutrowid.html) and [file-format description](https://sqlite.org/fileformat.html).

The retained shared fixture has 10,000 rows and seven logical columns, or 70,000 logical row/column positions. Its 1,000 missing `amount` positions have no populated cell row, so the actual source and each generated variant contain 69,000 `cells` rows and 69,000 `current_cells` rows. The probe copied only those two relations into four fresh, cell-only databases; it did not reproduce the full project schema or claim byte identity with the shared fixture. Each variant retained the exact source digest for every copied value and provenance/reference column, and passed `PRAGMA integrity_check`; its source was opened `mode=ro&immutable=1`. The rowid representation retained `NOT NULL` and unique `(row_id,column_id)` / `(column_id,row_id)` constraints, and retained named direct-scan indexes. To keep one-row results in column order, its row-axis index is `(row_id,column_id)`, matching the primary-key suffix SQLite includes in the existing `WITHOUT ROWID` secondary index.

| Isolated layout | File bytes | `cells` + `current_cells` | Required secondary/unique indexes | Change from 4 KiB `WITHOUT ROWID` |
| --- | ---: | ---: | ---: | ---: |
| `WITHOUT ROWID`, 4 KiB pages | 123,916,288 | 122,064,896 | 1,847,296 | baseline |
| Ordinary rowid, 4 KiB pages | 96,550,912 | 93,106,176 | 3,440,640 | -27,365,376 B (-22.08%) |
| `WITHOUT ROWID`, 8 KiB pages | 106,905,600 | 105,029,632 | 1,867,776 | -17,010,688 B (-13.73%) |
| Ordinary rowid, 8 KiB pages | 100,810,752 | 97,329,152 | 3,473,408 | -23,105,536 B (-18.65%) |

The 4 KiB result identifies packing, not a discarded index, as the principal effect. The `WITHOUT ROWID` tables allocated 105,340,928 B of overflow pages, with 34,547,430 B unused inside those pages; their total table unused space was 35,579,006 B. The corresponding rowid tables used 55,771,136 B of overflow pages with 183,014 B unused, and 6,219,180 B total table unused space. Rowid needs 1,593,344 B more identity-index allocation because `cells` must add a unique `(row_id,column_id)` b-tree, but that cost is much smaller than the overflow/page-slack reduction.

The representative reads retained their intended plans: exact current lookup used the primary key (`WITHOUT ROWID`) or named unique index (rowid); the 500-row column scan used the same named covering `idx_current_cells_column_row`; row reads used `idx_current_cells_row` with no temporary sort. Forty hot-cache repetitions were sub-millisecond and unsuitable as a product performance claim; the only useful conclusion is no plan regression in these direct-read shapes. The rowid 4 KiB column scan median was 0.163 ms versus 0.150 ms for the `WITHOUT ROWID` baseline, while exact and row reads were lower in this tiny warm sample.

This is a concrete physical-layout candidate. It neither removes the intentional base/current logical duplicate nor establishes a 500k saving ratio. A production proposal should compare the two layouts under the already-qualified 500k workload, including write amplification and all migration/reopen behavior, before changing schema.

## Candidate implementation checkpoint

The measured narrow candidate is implemented in the separate production commit `ce9ad0661128cbca01e761b57cf8c537827309fa` after research commit `21fd6e7276a55bf3af2661cbd8f86af7dd3449ad`. It changes only the `cells` and `current_cells` DDL plus their atomic physical-repack migration in `src/frisket/engine/store/schema.py` and `src/frisket/engine/store/bundle_open.py`. Both become ordinary rowid tables; `cells` retains explicit `UNIQUE(row_id,column_id)`, and `current_cells` retains named unique `idx_current_cells_column_row(column_id,row_id)` and `idx_current_cells_row(row_id,column_id)`. The migration explicitly copies every value and provenance/reference field, swaps both tables, recreates indexes, and stamps the digest in one transaction. It uses individual statements because Python's `sqlite3.Connection.executescript()` commits a pending transaction before running its script.

Focused, network-isolated tests passed: direct rowid migration/rollback, current-cell migration, search-work/index-hygiene, and pinned a62/a64 known migration chains (35 passed). This is not the 500k qualification result: full fresh and migrated fixture correctness, disk, peak-space, reopen, and latency gates remain required before landing. `src/frisket/search_index.py` deliberately forces `idx_current_cells_column_row`, and the migration preserves that direct covering scan contract without adding a scalar value index or a new projection.
